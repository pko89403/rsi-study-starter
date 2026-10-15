"""round loop. python -m loop.run --rounds N"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, tempfile, time
from pathlib import Path
import yaml
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int); ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()
    os.chdir(ROOT)
    require_llm()
    cfg = yaml.safe_load(Path(a.config).read_text())
    rounds = a.rounds or cfg["rounds"]
    problems = json.loads(Path(cfg["validation"]).read_text())
    screen = problems[: cfg["screen_size"]]
    log_path = Path(cfg["log"]); log_path.parent.mkdir(exist_ok=True)
    archive = Archive(ROOT)
    evolve_files = cfg["evolve_files"]
    if not archive.repo.head.is_valid():
        archive.commit("v0", evolve_files)
    cache: dict[str, tuple[dict, dict]] = {}
    last_round = 0
    if log_path.exists():
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        if lines:
            last_round = json.loads(lines[-1])["round"]

    for rnd in range(last_round + 1, last_round + rounds + 1):
        t0 = time.time(); calls = 0; LAST_AGENT_WARN.clear()
        parent = select_parent(archive)
        archive.checkout(parent, evolve_files)
        # 1. before (같은 commit은 다시 재지 않음)
        if parent not in cache:
            res, err, preds = eval_on(problems, cfg, "full")
            if err:
                print(f"[round {rnd}] parent {parent} failed to run: {err}"); sys.exit(1)
            cache[parent] = (res, preds)
        before, preds = cache[parent]
        archive.record(parent, before["correct"], None)
        if archive.best is None:
            archive.set_best(parent)
        block_name = cfg["editable_blocks"][0]
        blocks = {b: P.read_block(P.locate(evolve_files, b), b) for b in cfg["editable_blocks"]}
        ctx = RoundContext(round=rnd, week=cfg["week"], config=cfg, parent=parent, block_name=block_name,
                           block=blocks[block_name], blocks=blocks, before=before,
                           problems={p["id"]: p for p in problems}, predictions=preds, llm=llm.chat,
                           recent_log=[json.loads(l) for l in log_path.read_text().splitlines()[-5:]] if log_path.exists() else [])
        on_round_start(ctx)
        # 2. 진단 (hook)
        ctx.diagnosis = diagnose(ctx); calls += 1
        # 3-4. 제안
        patches = propose(ctx, build_prompt(ctx)); calls += max(1, len(patches))
        entry = {"round": rnd, "week": cfg["week"], "parent": parent, "commit": None, "hook_changed": cfg.get("hook_changed", "none"),
                 "diagnosis": ctx.diagnosis[:200], "block": block_name, "reason": None,
                 "screen": None, "full": {"n": before["n"], "before": before["correct"], "after": None},
                 "accepted": False, "rejected_by": None, "llm_calls": calls, "seconds": None}
        if not patches:
            entry["rejected_by"] = "error"; entry["reason"] = "no parsable patch"
        for pt in patches:
            entry["reason"] = pt.reason; entry["block"] = pt.block
            pt = review_patch(ctx, pt)
            if pt is None:
                entry["rejected_by"] = "review"; entry["reason"] += " | rejected by review_patch"; break
            why = P.guard(pt, cfg["editable_blocks"])
            if why:
                entry["rejected_by"] = "guard"; entry["reason"] = f"{pt.reason} | {why}"; dump_patch(rnd, pt, why); break
            target = P.locate(evolve_files, pt.block)
            src_before = target.read_text()
            P.apply(target, pt.block, pt.content)
            cerr = P.compiles(f"{target.parent.name}.{target.stem}")
            if P.outside_changed(src_before, target.read_text(), pt.block) or cerr:
                target.write_text(src_before)
                why = "outside block changed" if not cerr else f"compile error: {cerr}"
                entry["rejected_by"] = "guard"; entry["reason"] = f"{pt.reason} | {why}"
                dump_patch(rnd, pt, why); break
            sha = archive.commit(f"round {rnd}: {pt.reason}", evolve_files)
            entry["commit"] = sha; archive.record(sha, None, parent)
            if sha == parent:
                entry["rejected_by"] = "guard"; entry["reason"] = f"{pt.reason} | no change"; break
            if sha in cache:   # 같은 내용의 patch를 다시 낸 경우: 평가 생략
                fres, _ = cache[sha]
                entry["full"]["after"] = fres["correct"]; entry["rejected_by"] = "accept"; entry["reason"] += " | duplicate of evaluated commit"
                archive.checkout(parent, evolve_files); break
            # 5. screen
            sres, serr, _ = eval_on(screen, cfg, "screen"); calls += len(screen)
            before_screen = sum(1 for p in before["per_problem"][: len(screen)] if p["correct"])
            ctx.screen = sres
            if serr or not after_screen(ctx, before_screen, sres["correct"]):
                entry["screen"] = {"n": len(screen), "before": before_screen, "after": None if serr else sres["correct"]}
                entry["rejected_by"] = "screen" if not serr else "error"; entry["reason"] += f" | {serr}" if serr else ""
                if LAST_AGENT_WARN:
                    entry["agent_error"] = LAST_AGENT_WARN[-1]
                dump_patch(rnd, pt, entry.get("agent_error") or "screen drop")
                archive.checkout(parent, evolve_files); break
            entry["screen"] = {"n": len(screen), "before": before_screen, "after": sres["correct"]}
            # full
            fres, ferr, fpreds = eval_on(problems, cfg, "full"); calls += len(problems)
            if ferr:
                entry["rejected_by"] = "error"; archive.checkout(parent, evolve_files); break
            cache[sha] = (fres, fpreds); archive.record(sha, fres["correct"], parent)
            entry["full"]["after"] = fres["correct"]; ctx.after = fres; ctx.patch = pt
            ok = accept(ctx, before["correct"], fres["correct"], pt)
            entry["accepted"] = ok; ctx.accepted = ok
            if ok:
                archive.set_best(sha)
            else:
                entry["rejected_by"] = "accept"
            archive.checkout(parent, evolve_files) if not ok else None
        after_eval(ctx)
        entry["llm_calls"] = calls; entry["seconds"] = round(time.time() - t0, 1)
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"[round {rnd}] parent={parent} commit={entry['commit']} full {entry['full']['before']}->{entry['full']['after']} "
              f"accepted={entry['accepted']} rejected_by={entry['rejected_by']} ({entry['seconds']}s)")
        if should_stop(ctx):
            print(f"[round {rnd}] should_stop() -> True, stopping early"); break
    # 끝: 최고 commit으로 맞춰 둔다
    archive.checkout(archive.best, evolve_files)


if __name__ == "__main__":
    main()
