"""Improver 후보 모델이 4주차 `code` EVOLVE-BLOCK(노드 본문 4개 + MAX_ATTEMPTS) patch를 낼 수 있는지 잰다.

사용법:
  uv run python -m scripts.operator.probe_code_block --n 20                       # .env 의 IMPROVER_* (없으면 LLM_*) 설정으로
  uv run python -m scripts.operator.probe_code_block --n 20 --model qwen/qwen3-14b --base-url https://openrouter.ai/api/v1
  uv run python -m scripts.operator.probe_code_block --n 20 --model qwen3-14b --base-url http://localhost:1234/v1   # LM Studio

하는 일: 실제 build_prompt(계약 포함)로 patch 요청 → parse → guard → 블록을 exec → 고정 배선 make_graph()
→ compile → 가짜 ask/try_sql 로 1회 invoke(첫 SQL은 실패하게 해 재시도 경로를 밟게 함). 단계별 실패 수와 통과율을 표로 낸다. 점수는 재지 않는다(그건 loop가 한다).
SQL agent 파일은 건드리지 않는다. 결과는 logs/probe_<model>.jsonl 에 한 줄씩 남는다.
"""
from __future__ import annotations
import argparse, json, os, re, sys, time, traceback
from collections import Counter
from pathlib import Path

import yaml

from loop import patch as P
from loop.context import RoundContext
from loop.env import load_env
from hooks.build_prompt import build_prompt
import llm
from sql_agent.run import MAX_STEPS
from sql_agent.schema import schema_text

STAGES = ["parse", "guard", "exec", "build", "invoke", "ok"]


def fake_ctx(cfg: dict, diagnosis: str) -> RoundContext:
    """loop 없이 build_prompt를 부르기 위한 최소 RoundContext. before 결과는 validation 앞 10문제를 '틀림'으로 둔다."""
    problems = {p["id"]: p for p in json.loads(Path(cfg["validation"]).read_text())}
    ids = list(problems)[:10]
    before = {"n": 100, "correct": 65, "per_problem": [{"id": i, "correct": False, "error": "no such column"} for i in ids]}
    files = cfg["evolve_files"]
    blocks = {b: P.read_block(P.locate(files, b), b) for b in ("prompt", "code")}
    return RoundContext(round=1, week=4, config=cfg, parent="probe", block_name="code", block=blocks["code"],
                        before=before, problems=problems, predictions={i: "SELECT 1" for i in ids},
                        blocks=blocks, diagnosis=diagnosis, llm=llm.chat)


def run_one(ctx: RoundContext, src_run_py: str, model: str, max_tokens: int) -> dict:
    """patch 하나를 요청해 단계별로 검사한다. 반환: {"stage": 멈춘 단계 또는 "ok", "reason", "seconds", "chars", "block"}"""
    t0 = time.time()
    text = llm.chat(build_prompt(ctx), role="improver", max_tokens=max_tokens)
    sec = time.time() - t0
    rec = {"model": model, "seconds": round(sec, 1), "chars": len(text), "stage": None, "reason": None, "block": None}

    pt = P.parse_patch(text)
    if pt is None:
        rec.update(stage="parse", reason="format: BLOCK/REASON/<<<CONTENT 를 못 찾음", raw=text[:300]); return rec
    rec["block"] = pt.block
    if pt.block != "code":
        rec.update(stage="guard", reason=f"block={pt.block} (code 가 아님)"); return rec
    why = P.guard(pt, ["prompt", "code"])
    if why:
        rec.update(stage="guard", reason=why); return rec

    # 블록 밖 코드 + 새 블록을 한 모듈 소스로 합쳐 exec. sql_agent.run 의 이름공간을 그대로 재현한다.
    s, e = P.find_block(src_run_py, "code")
    module_src = src_run_py[:s] + pt.content.rstrip("\n") + "\n" + src_run_py[e:]
    module_src = module_src.split("\ndef main()")[0]  # main() 과 __main__ 은 제외
    ns: dict = {}
    try:
        exec(compile(module_src, "<probe>", "exec"), ns)
    except Exception as ex:
        rec.update(stage="exec", reason=f"{type(ex).__name__}: {ex}"); return rec
    missing = [n for n in ("MAX_ATTEMPTS", "select_schema", "generate", "fix", "route") if n not in ns]
    if missing:
        rec.update(stage="exec", reason=f"missing names: {missing}"); return rec

    calls = {"ask": 0, "run_sql": 0}

    def ask(msgs):  # 가짜 LLM: 첫 호출은 틀린 SQL, 이후는 맞는 SQL 을 낸다 (재시도 경로를 밟게)
        calls["ask"] += 1
        return "SELECT count(*) FROM nosuch" if calls["ask"] == 1 else "SELECT count(*) FROM singer"

    def run_sql(db_id, sql, limit=5):
        calls["run_sql"] += 1
        ok = "nosuch" not in sql
        return {"ok": ok, "rows": [(6,)] if ok else [], "error": None if ok else "OperationalError: no such table: nosuch"}

    ns["ask"] = ask; ns["try_sql"] = run_sql   # 블록 함수들은 모듈 전역 ask/try_sql 을 쓴다
    try:
        app = ns["make_graph"]()
    except Exception as ex:
        rec.update(stage="build", reason=f"{type(ex).__name__}: {ex}"); return rec
    try:
        out = app.invoke({"question": "How many singers?", "schema": "table singer(id, name)", "db_id": "concert_singer", "fewshot": []},
                         {"recursion_limit": MAX_STEPS})
    except Exception as ex:
        rec.update(stage="invoke", reason=f"{type(ex).__name__}: {str(ex)[:200]}"); return rec
    sql = out.get("sql", "")
    if not sql:
        rec.update(stage="invoke", reason="END 에서 sql 이 비어 있음"); return rec
    rec.update(stage="ok", reason=None, sql=sql, ask_calls=calls["ask"], attempts=out.get("attempts"),
               retries=calls["ask"] > 1, fixed=sql.strip().lower().endswith("from singer"), lines=pt.content.count("\n") + 1)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--max-tokens", type=int, help="config.yaml 의 improver_max_tokens 를 덮어씀")
    ap.add_argument("--model", help="IMPROVER_LLM_MODEL 을 덮어씀")
    ap.add_argument("--base-url", help="IMPROVER_LLM_BASE_URL 을 덮어씀")
    ap.add_argument("--provider", help="IMPROVER_LLM_PROVIDER 를 덮어씀 (OpenRouter provider 고정)")
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()
    load_env()
    if a.model: os.environ["IMPROVER_LLM_MODEL"] = a.model
    if a.base_url: os.environ["IMPROVER_LLM_BASE_URL"] = a.base_url
    if a.provider is not None: os.environ["IMPROVER_LLM_PROVIDER"] = a.provider
    model = os.environ.get("IMPROVER_LLM_MODEL") or os.environ.get("LLM_MODEL") or "?"

    cfg = yaml.safe_load(Path(a.config).read_text())
    cfg["editable_blocks"] = ["prompt", "code"]
    src = Path("sql_agent/run.py").read_text()
    ctx = fake_ctx(cfg, "The agent references tables/columns that do not exist; it never checks the SQL against the database.")

    out = Path("logs") / f"probe_{re.sub(r'[^A-Za-z0-9._-]', '_', model)}.jsonl"
    out.parent.mkdir(exist_ok=True)
    recs = []
    with out.open("a") as f:
        for i in range(a.n):
            try:
                r = run_one(ctx, src, model, a.max_tokens or cfg.get("improver_max_tokens", 4096))
            except Exception as ex:  # LLM 호출 실패 등. 한 번의 실패가 측정을 멈추지 않게 기록만 한다.
                r = {"model": model, "stage": "error", "reason": f"{type(ex).__name__}: {str(ex)[:200]}"}
                traceback.print_exc(limit=1, file=sys.stderr)
            r["i"] = i
            recs.append(r); f.write(json.dumps(r, ensure_ascii=False) + "\n"); f.flush()
            print(f"[{i+1}/{a.n}] {r['stage']:8s} {r.get('reason') or ''} ({r.get('seconds','?')}s)", flush=True)

    c = Counter(r["stage"] for r in recs)
    ok = [r for r in recs if r["stage"] == "ok"]
    print(f"\nmodel: {model}   n={len(recs)}   pass={len(ok)} ({100*len(ok)/max(1,len(recs)):.0f}%)")
    print("stage     count   (멈춘 단계. ok = 끝까지 통과)")
    for st in STAGES + ["error"]:
        if c.get(st): print(f"{st:9s} {c[st]}")
    if ok:
        print(f"retries (route sent to fix): {sum(r['retries'] for r in ok)}/{len(ok)}   fixed the SQL: {sum(r['fixed'] for r in ok)}/{len(ok)}   "
              f"mean lines: {sum(r['lines'] for r in ok)/len(ok):.0f}   mean seconds: {sum(r['seconds'] for r in ok)/len(ok):.1f}")
    print(f"log: {out}")


if __name__ == "__main__":
    main()
