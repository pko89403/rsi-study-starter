"""Improver LLM에 무엇을 줄지. 기본: 규칙 + 블록 계약 + few-shot pool 예시 + 현재 블록 + 진단 + 틀린 문제 10개 + 지난 log 5줄."""
from __future__ import annotations
import json
from pathlib import Path
from loop.context import RoundContext

CONTRACTS = {
    "prompt": """Block 'prompt' (sql_agent/run.py). How it is used by code outside the block, which you cannot change:
- FEWSHOT_IDS: list of ids from data/fewshot_pool.json. The outside code loads them and passes them to
  build_messages(question, schema, fewshot) as `fewshot`: a list of dicts with keys "question", "schema", "sql".
  You do NOT need to read any file. Never use open(), os, subprocess, or imports inside the block.
- build_messages must return a list of chat messages (dicts with "role" and "content").
- The model answering is small (7B); keep the prompt short and concrete.""",
    "code": """Block 'code' (sql_agent/run.py): the bodies of the SQL agent's graph nodes. The wiring is FIXED outside the block:
  START -> select_schema -> generate -> execute -> route -> ("fix" -> execute again | END)
- Keep these top-level names: MAX_ATTEMPTS (int), select_schema(s), generate(s), fix(s), route(s).
- s is the agent state dict with keys: question, schema, db_id, fewshot, sql, result, attempts.
  A node returns a dict with only the keys it changes. route(s) returns the string "fix" or END.
- execute (outside the block) runs s["sql"] on this problem's database and sets s["result"] = {"ok": bool, "rows": [...], "error": str|None}.
  It does not know the gold answer.
- ask(messages) -> str calls the LLM. Names available without import: END, ask, build_messages, extract_sql.
- v0: select_schema returns {} (no change) and route always returns END (no retry). Keep the block under ~40 lines.""",
    "evaluator": """Block 'evaluator' (evaluator/rules.py): select_problems(problems) -> list and compare(pred_rows, gold_rows, gold_sql) -> bool
- These define how validation is scored. Changing them changes the score itself; the held-out test uses a frozen copy.""",
}


def _pool_samples(ctx: RoundContext, n: int = 12) -> str:
    pool = json.loads(Path(ctx.config["fewshot_pool"]).read_text())
    rows = []
    for ex in pool[:n]:
        rows.append(f"- {ex['id']} ({ex['db_id']}, {ex.get('hardness','')}): {ex['question']}")
    return f"few-shot pool: {len(pool)} examples. First {n} ids:\n" + "\n".join(rows)


def build_prompt(ctx: RoundContext) -> list[dict]:
    wrong = [p for p in ctx.before["per_problem"] if not p["correct"]][:10]
    wrong_txt = "\n".join(f"- {ctx.problem(p['id'])['question']}\n  predicted: {ctx.prediction(p['id'])}\n  error: {p['error']}" for p in wrong)
    log_txt = "\n".join(json.dumps({k: r.get(k) for k in ("round", "reason", "accepted", "rejected_by")}) for r in ctx.recent_log)
    blocks = ctx.blocks or {ctx.block_name: ctx.block}
    contracts = "\n\n".join(CONTRACTS.get(b, f"Block '{b}'") for b in blocks)
    blocks_txt = "\n\n".join(f"Editable block '{b}':\n<<<CURRENT_BLOCK {b}>>>\n{src}\n<<<END_BLOCK>>>" for b, src in blocks.items())
    user = (f"Validation score: {ctx.before['correct']}/{ctx.before['n']}\nDiagnosis: {ctx.diagnosis}\n\n"
            f"Open blocks: {', '.join(blocks)}. This round, patch block '{ctx.block_name}' (BLOCK: {ctx.block_name}).\n\n{contracts}\n\n{_pool_samples(ctx)}\n\n"
            f"Wrong examples:\n{wrong_txt}\n\nRecent rounds:\n{log_txt or '(none)'}\n\n{blocks_txt}")
    return [{"role": "system", "content": ctx.prompt_text("patch.md")}, {"role": "user", "content": user}]
