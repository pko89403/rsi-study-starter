"""round loop (LangGraph). python -m loop.run --rounds N

한 round = 아래 graph를 한 바퀴. 번호는 hook 위치.
  begin(①select_parent ②on_round_start) → diagnose(③) → propose(④build_prompt ⑤propose, 사전 거부·재요청)
  → review(⑥) → apply(guard·commit) → screen(⑦after_screen) → full(⑧accept AND kernel_accept) → after_eval(⑨) → log
  → ⑩should_stop ? END : begin(다음 round)
판정은 전부 고정 채점 rules_v0. champion(best)은 내려가지 않는다. Improver 쪽 hook에는 정답 SQL을 넘기지 않는다.
"""
from __future__ import annotations
import argparse, copy, hashlib, json, os, subprocess, sys, tempfile, time
from pathlib import Path
from typing import TypedDict
import yaml
from langgraph.graph import StateGraph, START, END
from evaluator.evaluate import evaluate
from loop.env import require_llm
from loop import kernel as K
from loop import patch as P
from loop.archive import Archive
from loop.context import RoundContext
from sql_agent import llm
from hooks.select_parent import select_parent
from hooks.on_round_start import on_round_start
from hooks.diagnose import diagnose
from hooks.build_prompt import build_prompt
from hooks.propose import propose
from hooks.review_patch import review_patch
from hooks.after_screen import after_screen
from hooks.accept import accept
from hooks.after_eval import after_eval
from hooks.should_stop import should_stop

ROOT = Path(__file__).resolve().parent.parent
LAST_AGENT_WARN: list[str] = []
HOOKS = [select_parent, on_round_start, diagnose, build_prompt, propose, review_patch, after_screen, accept, after_eval, should_stop]
MAX_REASK = 3   # 평가 전에 거부된 patch를 이유와 함께 다시 요청하는 횟수


def hook_modules() -> list:
    return [sys.modules[f.__module__] for f in HOOKS]


def run_agent(problems: list[dict], out: Path, timeout: int) -> str | None:
    """gold를 뺀 임시 파일로 SQL agent를 subprocess 실행. 실패 사유 문자열 또는 None."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, dir=ROOT / "logs") as f:
        json.dump([{k: p[k] for k in ("id", "db_id", "question")} for p in problems], f)
        tmp = f.name
    try:
        r = subprocess.run([sys.executable, "-m", "sql_agent.run", "--problems", tmp, "--out", str(out)],
                           cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "agent timeout"
    finally:
        os.unlink(tmp)
    if r.returncode != 0:
        return "agent crashed: " + (r.stderr.strip().splitlines() or ["?"])[-1][:200]
    fails = [l for l in r.stdout.splitlines() if "failed:" in l]
    if fails:  # 문제별 예외 (빈 sql로 채점됨). 첫 줄만 기록
        LAST_AGENT_WARN.append(f"{len(fails)} problems failed, e.g. {fails[0][:200]}")
    return None


def dump_patch(rnd: int, pt, why: str) -> None:
    """거부된 patch 원문을 남긴다 (디버그용)."""
    d = ROOT / "logs" / "rejected"; d.mkdir(parents=True, exist_ok=True)
    (d / f"round_{rnd}.txt").write_text(f"# rejected: {why}\n# block: {pt.block}\n# reason: {pt.reason}\n\n{pt.content}\n")


def eval_on(problems: list[dict], cfg: dict, tag: str) -> tuple[dict | None, str | None, dict]:
    """SQL agent를 돌리고 고정 채점(rules_v0)으로 잰다. 반환: (evaluate 결과, 오류, {id: sql})."""
    out = ROOT / "logs" / f"_pred_{tag}.jsonl"
    err = run_agent(problems, out, cfg["agent_timeout_sec"])
    if err:
        return None, err, {}
    sub = ROOT / "logs" / f"_problems_{tag}.json"
    sub.write_text(json.dumps(problems))
    res = evaluate(str(out), str(sub), cfg["db_dir"], cfg["sql_timeout_sec"])   # rules=None → rules_v0
    preds = {}
    for line in out.read_text().splitlines():
        try:
            o = json.loads(line); preds[o["id"]] = o.get("sql", "")
        except Exception:
            pass
    return res, None, preds


def patch_hash(pt: P.Patch) -> str:
    return hashlib.sha1(f"{pt.block}\n{pt.content.strip()}".encode()).hexdigest()[:12]


def precheck(ctx: RoundContext, pt: P.Patch | None, editable: list[str], archive: Archive) -> tuple[P.Patch | None, str | None]:
    """평가 전에 거를 것: 형식, 대상 블록, guard, parent와 같음(no-op), 이미 시도한 patch. 통과하면 정규화한 patch."""
    if pt is None:
        return None, "no parsable patch (answer exactly: BLOCK / REASON / <<<CONTENT ... CONTENT>>>)"
    pt = P.normalize(pt, ctx.blocks or {ctx.block_name: ctx.block})
    if pt.block != ctx.block_name:
        return None, f"this round's target block is '{ctx.block_name}', you wrote BLOCK: {pt.block}"
    why = P.guard(pt, editable)
    if why:
        return None, why
    if pt.content.strip() == (ctx.blocks or {}).get(pt.block, ctx.block).strip():
        return None, "identical to the current block (no change)"
    if archive.seen(patch_hash(pt)):
        return None, "this exact change was already tried in an earlier round"
    return pt, None


def propose_valid(ctx: RoundContext, editable: list[str], archive: Archive) -> tuple[P.Patch | None, list[str], int, int]:
    """build_prompt → propose. 평가 전 거부되면 이유를 붙여 최대 MAX_REASK번 다시 묻는다.
    round당 평가하는 patch는 1개 (후보가 여럿이면 처음 통과한 것). 반환: (patch, 거부 이유들, 재요청 수, LLM 호출 수)."""
    msgs, reasons, calls = build_prompt(ctx), [], 0
    for attempt in range(MAX_REASK + 1):
        patches = propose(ctx, msgs); calls += max(1, len(patches))
        now = []
        for p in patches or [None]:
            pt, why = precheck(ctx, p, editable, archive)
            if pt:
                return pt, reasons, attempt, calls
            now.append(why)
        reasons += now
        msgs = msgs + [{"role": "user", "content": f"Your answer was rejected before evaluation: {'; '.join(now)}. "
                        f"Patch block '{ctx.block_name}' with one real change. Same answer format."}]
    return None, reasons, MAX_REASK, calls


class RoundState(TypedDict, total=False):
    rnd: int
    t0: float
    calls: int
    parent: str
    before: dict
    bpreds: dict
    ctx: RoundContext
    entry: dict           # rounds.jsonl 한 줄. 노드들이 채운다
    pt: P.Patch
    sha: str
    halt: bool            # 이번 round의 patch 처리를 끝낼지 (거부 등)


def build_graph(cfg: dict, archive: Archive, last_round: int):
    problems = json.loads(Path(cfg["validation"]).read_text())
    view = K.strip_gold(problems)
    db_of = {p["id"]: p["db_id"] for p in problems}
    screen = problems[: cfg["screen_size"]]
    log_path = Path(cfg["log"])
    evolve_files = cfg["evolve_files"]
    editable = cfg["editable_blocks"]
    schema = K.collect_knobs(hook_modules())
    anchors0 = K.anchors_sha()
    models = {"model_agent": os.environ.get("LLM_MODEL"), "model_improver": os.environ.get("IMPROVER_LLM_MODEL") or os.environ.get("LLM_MODEL")}
    cache: dict[str, tuple[dict, dict]] = {}   # commit -> (evaluate 결과, 예측). 같은 commit은 다시 재지 않음

    def reject(s: RoundState, by: str, note: str = "", dump: str | None = None) -> dict:
        e = s["entry"]; e["rejected_by"] = by
        if note:
            e["reason"] = f"{e['reason']} | {note}"
        if dump:
            dump_patch(s["rnd"], s["pt"], dump)
        return {"halt": True}

    def begin(s: RoundState) -> dict:
        t0 = time.time(); LAST_AGENT_WARN.clear()
        if K.anchors_sha() != anchors0:
            print(f"[round {s['rnd']}] fixed files changed during the run (anchors {anchors0} -> {K.anchors_sha()}); stopping"); sys.exit(2)
        parent = select_parent(archive)
        archive.checkout(parent, evolve_files)
        if parent not in cache:
            res, err, preds = eval_on(problems, cfg, "full")
            if err:
                print(f"[round {s['rnd']}] parent {parent} failed to run: {err}"); sys.exit(1)
            cache[parent] = (res, preds)
        before, preds = cache[parent]
        archive.record(parent, before["correct"], None)
        if archive.best is None:
            archive.set_best(parent)
        blocks = {b: P.read_block(P.locate(evolve_files, b), b) for b in editable}
        # 이번 round에 고칠 블록: 열린 블록을 round마다 돌아가며. on_round_start hook에서 ctx.block_name을 바꿔 다른 전략을 쓸 수 있다
        block_name = editable[(s["rnd"] - 1) % len(editable)]
        improver, errs = K.validate(K.read_improver(), schema)
        if errs:
            print(f"[round {s['rnd']}] IMPROVER: {'; '.join(errs)} (기본값 사용)")
        ctx = RoundContext(round=s["rnd"], week=cfg["week"], config=copy.deepcopy(cfg), parent=parent, block_name=block_name,
                           block=blocks[block_name], blocks=blocks, before=before, problems=view, predictions=preds, llm=llm.chat,
                           recent_log=[json.loads(l) for l in log_path.read_text().splitlines()[-5:]] if log_path.exists() else [],
                           improver=improver, seed=s["rnd"])
        on_round_start(ctx)
        if ctx.block_name not in blocks:
            print(f"[round {s['rnd']}] on_round_start set block_name={ctx.block_name!r}, not open; using {block_name}")
            ctx.block_name = block_name
        ctx.block = blocks[ctx.block_name]
        return {"t0": t0, "calls": 0, "parent": parent, "before": before, "bpreds": preds, "ctx": ctx, "halt": False}

    def diagnose_node(s: RoundState) -> dict:
        s["ctx"].diagnosis = diagnose(s["ctx"])
        return {"calls": s["calls"] + 1}

    def propose_node(s: RoundState) -> dict:
        ctx, before = s["ctx"], s["before"]
        pt, reasons, resamples, n = propose_valid(ctx, editable, archive)
        calls = s["calls"] + n
        entry = {"round": s["rnd"], "week": cfg["week"], "parent": s["parent"], "commit": None, "hook_changed": cfg.get("hook_changed", "none"),
                 "diagnosis": ctx.diagnosis[:200], "block": ctx.block_name, "reason": None,
                 "screen": None, "full": {"n": before["n"], "before": before["correct"], "after": None},
                 "accepted": False, "rejected_by": None, "llm_calls": calls, "seconds": None,
                 "lever": ctx.block_name, "improver_sha": K.improver_sha(ctx.improver), "resamples": resamples,
                 "anchors_sha": anchors0, **models}
        if pt is None:
            entry["rejected_by"] = "error" if all(r.startswith("no parsable") for r in reasons) else "guard"
            entry["reason"] = "rejected before evaluation: " + " / ".join(reasons)[:400]
            return {"entry": entry, "calls": calls, "halt": True}
        return {"pt": pt, "entry": entry, "calls": calls, "halt": False}

    def review_node(s: RoundState) -> dict:
        pt = s["pt"]
        s["entry"]["reason"] = pt.reason; s["entry"]["block"] = pt.block
        pt = review_patch(s["ctx"], pt)
        if pt is None:
            return reject(s, "review", "rejected by review_patch")
        return {"pt": pt}

    def apply_node(s: RoundState) -> dict:
        pt, e = s["pt"], s["entry"]
        why = P.guard(pt, editable)   # review_patch가 내용을 바꿨을 수 있으니 다시 검사
        if why:
            e["reason"] = pt.reason; return reject(s, "guard", why, dump=why)
        archive.add_seen(patch_hash(pt))
        target = P.locate(evolve_files, pt.block)
        src_before = target.read_text()
        P.apply(target, pt.block, pt.content)
        cerr = P.compiles(f"{target.parent.name}.{target.stem}")
        if P.outside_changed(src_before, target.read_text(), pt.block) or cerr:
            target.write_text(src_before)
            why = "outside block changed" if not cerr else f"compile error: {cerr}"
            e["reason"] = pt.reason; return reject(s, "guard", why, dump=why)
        sha = archive.commit(f"round {s['rnd']}: {pt.reason}", evolve_files)
        e["commit"] = sha; archive.record(sha, None, s["parent"])
        if sha == s["parent"]:
            e["reason"] = pt.reason; return reject(s, "guard", "no change")
        if sha in cache:   # 같은 내용의 commit을 다시 만든 경우: 평가 생략
            e["full"]["after"] = cache[sha][0]["correct"]
            archive.checkout(s["parent"], evolve_files)
            return reject(s, "accept", "duplicate of evaluated commit")
        return {"sha": sha}

    def screen_node(s: RoundState) -> dict:
        e, before = s["entry"], s["before"]
        sres, serr, _ = eval_on(screen, cfg, "screen")
        calls = s["calls"] + len(screen)
        before_screen = sum(1 for p in before["per_problem"][: len(screen)] if p["correct"])
        s["ctx"].screen = sres
        if serr or not after_screen(s["ctx"], before_screen, sres["correct"]):
            e["screen"] = {"n": len(screen), "before": before_screen, "after": None if serr else sres["correct"]}
            if LAST_AGENT_WARN:
                e["agent_error"] = LAST_AGENT_WARN[-1]
            archive.checkout(s["parent"], evolve_files)
            return {"calls": calls, **reject(s, "error" if serr else "screen", serr or "", dump=e.get("agent_error") or "screen drop")}
        e["screen"] = {"n": len(screen), "before": before_screen, "after": sres["correct"]}
        return {"calls": calls}

    def full_node(s: RoundState) -> dict:
        ctx, e, sha = s["ctx"], s["entry"], s["sha"]
        fres, ferr, fpreds = eval_on(problems, cfg, "full")
        calls = s["calls"] + len(problems)
        if ferr:
            e["rejected_by"] = "error"; archive.checkout(s["parent"], evolve_files)
            return {"calls": calls, "halt": True}
        cache[sha] = (fres, fpreds); archive.record(sha, fres["correct"], s["parent"])
        e["full"]["after"] = fres["correct"]; ctx.after = fres; ctx.patch = s["pt"]
        hook_ok = bool(accept(ctx, s["before"]["correct"], fres["correct"], s["pt"]))
        k_ok, kinfo = K.kernel_accept(s["before"], fres, s["bpreds"], fpreds, db_of)
        ok = hook_ok and k_ok
        e.update(kinfo); e["hook_accept"] = hook_ok; e["kernel_accept"] = k_ok
        e["accepted"] = ok; ctx.accepted = ok
        if ok:
            e["champion"] = archive.set_best(sha)   # parent보다 나아도 현재 champion보다 높을 때만 champion
        else:
            e["rejected_by"] = "accept"; archive.checkout(s["parent"], evolve_files)
        return {"calls": calls, "halt": True}

    def after_eval_node(s: RoundState) -> dict:
        ctx, e = s["ctx"], s["entry"]
        after_eval(ctx)
        e["llm_calls"] = s["calls"]; e["seconds"] = round(time.time() - s["t0"], 1)
        with open(log_path, "a") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"[round {s['rnd']}] parent={s['parent']} block={e['block']} commit={e['commit']} full {e['full']['before']}->{e['full']['after']} "
              f"net={e.get('net')} accepted={e['accepted']} rejected_by={e['rejected_by']} resamples={e['resamples']} ({e['seconds']}s)")
        return {}

    def next_round(s: RoundState) -> str:
        if should_stop(s["ctx"]):
            print(f"[round {s['rnd']}] should_stop() -> True, stopping early"); return END
        return END if s["rnd"] >= last_round else "advance"

    halt_or = lambda nxt: (lambda s: "after_eval" if s["halt"] else nxt)

    g = StateGraph(RoundState)
    g.add_node("begin", begin)
    g.add_node("diagnose", diagnose_node)
    g.add_node("propose", propose_node)
    g.add_node("review", review_node)
    g.add_node("apply", apply_node)
    g.add_node("screen", screen_node)
    g.add_node("full", full_node)
    g.add_node("after_eval", after_eval_node)
    g.add_node("advance", lambda s: {"rnd": s["rnd"] + 1})
    g.add_edge(START, "begin")
    g.add_edge("begin", "diagnose")
    g.add_edge("diagnose", "propose")
    g.add_conditional_edges("propose", halt_or("review"))
    g.add_conditional_edges("review", halt_or("apply"))
    g.add_conditional_edges("apply", halt_or("screen"))
    g.add_conditional_edges("screen", halt_or("full"))
    g.add_edge("full", "after_eval")
    g.add_conditional_edges("after_eval", next_round)
    g.add_edge("advance", "begin")
    return g.compile()


def setup(config: str = "config.yaml") -> tuple[dict, Archive]:
    os.chdir(ROOT)
    require_llm()
    cfg = yaml.safe_load(Path(config).read_text())
    Path(cfg["log"]).parent.mkdir(exist_ok=True)
    os.environ.setdefault("LLM_CACHE", str(ROOT / "logs" / "llm_cache.sqlite"))   # temperature 0 응답 캐시. LLM_CACHE= 로 끈다. test_eval은 안 씀
    archive = Archive(ROOT)
    if not archive.repo.head.is_valid():
        archive.commit("v0", cfg["evolve_files"])
    return cfg, archive


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int); ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()
    cfg, archive = setup(a.config)
    rounds = a.rounds or cfg["rounds"]
    log_path = Path(cfg["log"])
    last_round = 0
    if log_path.exists():
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        if lines:
            last_round = json.loads(lines[-1])["round"]
    graph = build_graph(cfg, archive, last_round + rounds)
    graph.invoke({"rnd": last_round + 1}, {"recursion_limit": rounds * 20 + 10})
    archive.checkout(archive.best, cfg["evolve_files"])   # 끝: champion으로 맞춰 둔다
    print(f"champion: {archive.best} ({archive.score_of(archive.best)}/{len(json.loads(Path(cfg['validation']).read_text()))})")


if __name__ == "__main__":
    main()
