"""Improver LLM에 무엇을 줄지. 기본: 규칙 + 대상 블록 계약 + few-shot pool 예시 + 대상 블록 + 진단 + 틀린 문제 + 지난 log 5줄.

KNOBS: 이 hook에서 Improver가 조절해도 되는 파라미터(KNOBS) (이름: (타입, 범위, 기본값)). 값은 ctx.improver로 들어온다.
참가자가 method를 넣을 때 그 method의 파라미터를 여기에 더하면, Improver가 make meta-propose로 값을 다듬는다.
"""
from __future__ import annotations
import json, random
from pathlib import Path
from loop.context import RoundContext

KNOBS = {
    "n_wrong": ("int", 3, 20, 10),       # 보여 줄 틀린 문제 수
    "show_pool": ("int", 0, 20, 12),     # 보여 줄 few-shot pool 후보 수
    "guidance": ("str", 400, ""),        # patch 작성 지침 한두 문장
    # 3주차 Self-Refine (Madaan et al. 2023): 초안 patch → 피드백 → 수정. hooks/propose.py가 같은 파라미터를 읽는다.
    # meta-eval은 build_prompt 출력 hash로 NULL을 판정하므로 파라미터는 여기 선언하고 prompt에도 반영한다.
    "refine_iters": ("int", 0, 3, 1),    # 피드백→수정 반복 수. 0이면 Self-Refine 끔 (v0와 같음)
    # 이전 실행(w3_selfrefine)에서 수정본이 길고 일반적인 지시문으로 수렴해 validation만 올랐으므로 길이·구체성 조건을 기본값에 넣는다.
    "critic_focus": ("str", 400, "Does the patch add only concrete rules taken from the wrong examples "
                                 "(exact columns asked, column order, execution errors, wrong joins)? Reject generic 'be correct / "
                                 "verify schema' wording. The system prompt must stay within 3 sentences; cut anything longer."),
}
KNOB_DOCS = {   # Improver LLM이 읽는 파라미터 설명
    "n_wrong": "how many wrong validation examples (question, predicted SQL, error) the patch prompt shows",
    "show_pool": "how many few-shot pool candidates (id, db, question) the patch prompt lists for choosing FEWSHOT_IDS",
    "guidance": "an instruction sentence added to the patch prompt telling the patch writer how to write patches",
    "refine_iters": "how many Self-Refine rounds (critique the draft patch, then rewrite it) run before the patch is evaluated; 0 disables",
    "critic_focus": "the questions the critic asks about a draft patch; the patch writer is told these criteria in advance",
}


def knob(ctx: RoundContext, name: str):
    return (ctx.improver or {}).get(name, KNOBS[name][-1])

CONTRACTS = {
    "prompt": """Block 'prompt' (sql_agent/run.py). How it is used by code outside the block, which you cannot change:
- FEWSHOT_IDS: list of ids from data/fewshot_pool.json. The outside code loads them and passes them to
  build_messages(question, schema, fewshot) as `fewshot`: a list of dicts with keys "question", "schema", "sql".
  You do NOT need to read any file. Never use open(), os, subprocess, or imports inside the block.
- build_messages must return a list of chat messages (dicts with "role" and "content").
- The model answering is small; keep the prompt short and concrete.""",
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


def _pool_samples(ctx: RoundContext, n: int) -> str:
    if n == 0:
        return "few-shot pool: (not shown)"
    pool = json.loads(Path(ctx.config["fewshot_pool"]).read_text())
    rows = []
    for ex in pool[:n]:
        rows.append(f"- {ex['id']} ({ex['db_id']}, {ex.get('hardness','')}): {ex['question']}")
    return f"few-shot pool: {len(pool)} examples. First {n} ids:\n" + "\n".join(rows)


def build_prompt(ctx: RoundContext) -> list[dict]:
    wrong = [p for p in ctx.before["per_problem"] if not p["correct"]]
    random.Random(ctx.seed).shuffle(wrong)   # round마다 다른 오답 표본 (같은 입력 → 같은 patch 반복을 막는다)
    wrong = wrong[: knob(ctx, "n_wrong")]
    wrong_txt = "\n".join(f"- {ctx.problem(p['id'])['question']}\n  predicted: {ctx.prediction(p['id'])}\n  error: {p['error']}" for p in wrong)
    log_txt = "\n".join(json.dumps({k: r.get(k) for k in ("round", "block", "reason", "accepted", "rejected_by", "net")}) for r in ctx.recent_log)
    b = ctx.block_name
    guidance = knob(ctx, "guidance")
    user = (f"Validation score: {ctx.before['correct']}/{ctx.before['n']}\nDiagnosis: {ctx.diagnosis}\n\n"
            f"This round, patch block '{b}' (BLOCK: {b}).\n\n{CONTRACTS.get(b, f'Block {b!r}')}\n\n"
            + (f"Guidance: {guidance}\n\n" if guidance else "")
            + (f"A reviewer will critique your patch on: {knob(ctx, 'critic_focus')}\n\n" if knob(ctx, "refine_iters") > 0 else "")
            + f"{_pool_samples(ctx, knob(ctx, 'show_pool'))}\n\nWrong examples:\n{wrong_txt}\n\nRecent rounds:\n{log_txt or '(none)'}\n\n"
            f"Current block '{b}':\n<<<CURRENT_BLOCK {b}>>>\n{ctx.block}\n<<<END_BLOCK>>>")
    return [{"role": "system", "content": ctx.prompt_text("patch.md")}, {"role": "user", "content": user}]
