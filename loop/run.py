"""round loop (LangGraph). python -m loop.run --rounds N

한 round = 아래 graph를 한 바퀴. 번호는 hook 위치.
  begin(①select_parent ②on_round_start) → diagnose(③) → propose(④build_prompt ⑤propose)
  → review(⑥) → apply(guard·commit) → screen(⑦after_screen) → full(⑧accept) ─┐
     ↑ 다음 patch ────────────────────────────────────────────────────────────┘
  거부 / 마지막 patch → after_eval(⑨) → log → ⑩should_stop ? END : begin(다음 round)
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, tempfile, time
from pathlib import Path
from typing import TypedDict
import yaml
from langgraph.graph import StateGraph, START, END
from evaluator.evaluate import evaluate
from evaluator import rules as eval_rules
from loop.env import require_llm
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
    out = ROOT / "logs" / f"_pred_{tag}.jsonl"
    err = run_agent(problems, out, cfg["agent_timeout_sec"])
    if err:
        return None, err, {}
    sub = ROOT / "logs" / f"_problems_{tag}.json"
    sub.write_text(json.dumps(problems))
    res = evaluate(str(out), str(sub), cfg["db_dir"], cfg["sql_timeout_sec"], rules=eval_rules)
    preds = {}
    for line in out.read_text().splitlines():
        try:
            o = json.loads(line); preds[o["id"]] = o.get("sql", "")
        except Exception:
            pass
    return res, None, preds


class RoundState(TypedDict, total=False):
    rnd: int
    t0: float
    calls: int
    parent: str
    before: dict
    ctx: RoundContext
    entry: dict           # rounds.jsonl 한 줄. 노드들이 채운다
    patches: list
    i: int                # 지금 보는 patch 번호
    pt: P.Patch
    sha: str
    halt: bool            # 이번 round의 patch 처리를 끝낼지 (거부 등)


def build_graph(cfg: dict, archive: Archive, last_round: int):
    problems = json.loads(Path(cfg["validation"]).read_text())
    screen = problems[: cfg["screen_size"]]
    log_path = Path(cfg["log"])
    evolve_files = cfg["evolve_files"]
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
        blocks = {b: P.read_block(P.locate(evolve_files, b), b) for b in cfg["editable_blocks"]}
        # 이번 round에 고칠 블록: 열린 블록을 round마다 돌아가며. on_round_start hook에서 ctx.block_name을 바꿔 다른 전략을 쓸 수 있다
        block_name = cfg["editable_blocks"][(s["rnd"] - 1) % len(cfg["editable_blocks"])]
        ctx = RoundContext(round=s["rnd"], week=cfg["week"], config=cfg, parent=parent, block_name=block_name,
                           block=blocks[block_name], blocks=blocks, before=before,
                           problems={p["id"]: p for p in problems}, predictions=preds, llm=llm.chat,
                           recent_log=[json.loads(l) for l in log_path.read_text().splitlines()[-5:]] if log_path.exists() else [])
        on_round_start(ctx)
        return {"t0": t0, "calls": 0, "parent": parent, "before": before, "ctx": ctx, "halt": False}

    def diagnose_node(s: RoundState) -> dict:
        s["ctx"].diagnosis = diagnose(s["ctx"])
        return {"calls": s["calls"] + 1}

    def propose_node(s: RoundState) -> dict:
        ctx, before = s["ctx"], s["before"]
        patches = propose(ctx, build_prompt(ctx))
        calls = s["calls"] + max(1, len(patches))
        entry = {"round": s["rnd"], "week": cfg["week"], "parent": s["parent"], "commit": None, "hook_changed": cfg.get("hook_changed", "none"),
                 "diagnosis": ctx.diagnosis[:200], "block": ctx.block_name, "reason": None,
                 "screen": None, "full": {"n": before["n"], "before": before["correct"], "after": None},
                 "accepted": False, "rejected_by": None, "llm_calls": calls, "seconds": None}
        if not patches:
            entry["rejected_by"] = "error"; entry["reason"] = "no parsable patch"
        return {"patches": patches, "i": 0, "entry": entry, "calls": calls, "halt": not patches}

    def review_node(s: RoundState) -> dict:
        pt = s["patches"][s["i"]]
        s["entry"]["reason"] = pt.reason; s["entry"]["block"] = pt.block
        pt = review_patch(s["ctx"], pt)
        if pt is None:
            return reject(s, "review", "rejected by review_patch")
        return {"pt": pt}

    def apply_node(s: RoundState) -> dict:
        pt, e = s["pt"], s["entry"]
        why = P.guard(pt, cfg["editable_blocks"])
        if why:
            e["reason"] = pt.reason; return reject(s, "guard", why, dump=why)
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
        if sha in cache:   # 같은 내용의 patch를 다시 낸 경우: 평가 생략
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
        ok = accept(ctx, s["before"]["correct"], fres["correct"], s["pt"])
        e["accepted"] = ok; ctx.accepted = ok
        if ok:
            archive.set_best(sha)
        else:
            e["rejected_by"] = "accept"; archive.checkout(s["parent"], evolve_files)
        return {"calls": calls, "i": s["i"] + 1}

    def after_eval_node(s: RoundState) -> dict:
        ctx, e = s["ctx"], s["entry"]
        after_eval(ctx)
        e["llm_calls"] = s["calls"]; e["seconds"] = round(time.time() - s["t0"], 1)
        with open(log_path, "a") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"[round {s['rnd']}] parent={s['parent']} commit={e['commit']} full {e['full']['before']}->{e['full']['after']} "
              f"accepted={e['accepted']} rejected_by={e['rejected_by']} ({e['seconds']}s)")
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
    g.add_conditional_edges("full", lambda s: "after_eval" if s["halt"] or s["i"] >= len(s["patches"]) else "review")
    g.add_conditional_edges("after_eval", next_round)
    g.add_edge("advance", "begin")
    return g.compile()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int); ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()
    os.chdir(ROOT)
    require_llm()
    cfg = yaml.safe_load(Path(a.config).read_text())
    rounds = a.rounds or cfg["rounds"]
    log_path = Path(cfg["log"]); log_path.parent.mkdir(exist_ok=True)
    os.environ.setdefault("LLM_CACHE", str(ROOT / "logs" / "llm_cache.sqlite"))   # temperature 0 응답 캐시. LLM_CACHE= 로 끈다. test_eval은 안 씀
    archive = Archive(ROOT)
    if not archive.repo.head.is_valid():
        archive.commit("v0", cfg["evolve_files"])
    last_round = 0
    if log_path.exists():
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        if lines:
            last_round = json.loads(lines[-1])["round"]
    graph = build_graph(cfg, archive, last_round + rounds)
    # ponytail: round당 노드 ~10개 × patch 수. propose가 patch를 많이 내면 limit를 올린다
    graph.invoke({"rnd": last_round + 1}, {"recursion_limit": rounds * 50})
    archive.checkout(archive.best, cfg["evolve_files"])   # 끝: 최고 commit으로 맞춰 둔다


if __name__ == "__main__":
    main()
