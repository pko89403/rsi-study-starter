"""Improver LLM에 무엇을 줄지. 기본: 규칙 + 대상 블록 계약 + few-shot pool 예시 + 대상 블록 + 진단 + 틀린 문제 + 지난 log 5줄.

KNOBS: 이 hook에서 Improver가 조절해도 되는 파라미터(KNOBS) (이름: (타입, 범위, 기본값)). 값은 ctx.improver로 들어온다.
참가자가 method를 넣을 때 그 method의 파라미터를 여기에 더하면, Improver가 make meta-propose로 값을 다듬는다.
"""
from __future__ import annotations
import ast, json, random
from pathlib import Path
from loop.context import RoundContext

KNOBS = {
    "n_wrong": ("int", 3, 20, 10),       # 보여 줄 틀린 문제 수
    "show_pool": ("int", 0, 20, 12),     # 보여 줄 few-shot pool 후보 수
    "guidance": ("str", 400, ""),        # patch 작성 지침 한두 문장
}
KNOB_DOCS = {   # Improver LLM이 읽는 파라미터 설명
    "n_wrong": "how many wrong validation examples (question, predicted SQL, error) the patch prompt shows",
    "show_pool": "how many few-shot pool candidates (id, db, question) the patch prompt lists for choosing FEWSHOT_IDS",
    "guidance": "an instruction sentence added to the patch prompt telling the patch writer how to write patches",
}


def knob(ctx: RoundContext, name: str):
    return (ctx.improver or {}).get(name, KNOBS[name][-1])


# 이번 round에 겨냥할 오답 유형: any / exec_error / mismatch / rotate(round마다 번갈아).
# 보통은 hooks/on_round_start.py가 round마다 골라 ctx.improver["target_error_class"]에 넣는다. 이 값은 SELF_SELECT=False일 때만 쓰인다.
# KNOBS가 아닌 이유: make meta-eval은 on_round_start를 돌리지 않아, 이 값을 바꿔도 실제 round와 다른 효과를 재게 된다.
TARGET_ERROR_CLASS = "any"


def target(ctx: RoundContext) -> str:
    t = (ctx.improver or {}).get("target_error_class", TARGET_ERROR_CLASS)
    return ["exec_error", "mismatch"][ctx.seed % 2] if t == "rotate" else t


def target_wrong(ctx: RoundContext) -> list[dict]:
    """겨냥한 유형의 오답 (없으면 전체 오답). exec_error = 실행 실패, mismatch = 실행됐지만 결과가 다름."""
    wrong = [p for p in ctx.before["per_problem"] if not p["correct"]]
    t = target(ctx)
    if t == "any":
        return wrong
    pick = [p for p in wrong if (p["error"] == "result mismatch") == (t == "mismatch")]
    return pick or wrong

CONTRACTS = {
    "prompt": """Block 'prompt' (sql_agent/run.py). How it is used by code outside the block, which you cannot change:
- FEWSHOT_IDS: list of ids from data/fewshot_pool.json. The outside code loads them and passes them to
  build_messages(question, schema, fewshot) as `fewshot`: a list of dicts with keys "question", "schema", "sql".
  You do NOT need to read any file. Never use open(), os, subprocess, or imports inside the block.
- build_messages must return a list of chat messages (dicts with "role" and "content").
- This round you change SYSTEM_PROMPT, FEWSHOT_IDS or build_messages. The skill list SKILLS and the code that applies it
  are kept by the loop as they are: do not write them.
- The model answering is small; keep the prompt short and concrete.""",
    "skills": """Block 'prompt' (sql_agent/run.py). This round you can change only the skill list SKILLS.
- Write your patch as one line, the full new list:
      SKILLS = [{"when": ["phrase", "other phrase"], "rule": "what the SQL agent must do for such questions"}]
  The loop keeps only this list. It ignores every other change (SYSTEM_PROMPT, build_messages, FEWSHOT_IDS stay as they are).
- A skill adds its rule to the system message only for questions whose lowercase text contains one of its "when" phrases,
  so other answers stay unchanged.
- Take the phrases from the wrong questions below, and choose words that mark a type of question (for example "average", "for each",
  "more than"). Do not use words that most questions contain, and do not copy a whole question.
- The rule must say what to do in SQL for that type of question. Do not repeat what SYSTEM_PROMPT already says.
- If the current block has SKILLS, write the full new list: keep the skills that help, change or remove the others, add new ones.
- The model answering is small; keep each rule short and concrete.""",
    "code": """Block 'code' (sql_agent/run.py): the bodies of the SQL agent's graph nodes. The wiring is FIXED outside the block:
  START -> select_schema -> generate -> execute -> route -> ("fix" -> execute again | END)
- Keep these top-level names: MAX_ATTEMPTS (int), select_schema(s), generate(s), fix(s), route(s).
- s is the agent state dict with keys: question, schema, db_id, fewshot, sql, result, attempts.
  A node returns a dict with only the keys it changes. route(s) returns the string "fix" or END.
- execute (outside the block) runs s["sql"] on this problem's database and sets s["result"] = {"ok": bool, "rows": [...], "error": str|None}.
  It does not know the gold answer.
- ask(messages) -> str calls the LLM. Names available without import: END, ask, build_messages, extract_sql.
- v0: select_schema returns {} (no change) and route always returns END (no retry). Keep the block under ~40 lines.""",
}


# 4주차 4b: prompt 블록 하나 안에서 SKILLS와 나머지(SYSTEM_PROMPT·FEWSHOT_IDS·build_messages)를 round마다 번갈아 고친다.
# sql_agent/run.py는 고정이라 블록을 새로 만들 수 없다. prompt round를 번갈아 SKILLS만 / 나머지만 고친다 (aspect). review_patch가 이 나눔을 강제한다.
ALTERNATE = True


def aspect(ctx: RoundContext) -> str:
    """prompt 블록에서 이번 round에 고칠 부분: "skills" 또는 "prompt". ALTERNATE=False면 늘 "prompt".
    loop는 열린 블록을 round마다 돌린다 (editable[(round-1) % n]). prompt round의 순번 (round-1)//n의 홀짝으로 나눠야
    블록이 2개일 때도 SKILLS와 나머지가 번갈아 온다 (round 홀짝으로 나누면 prompt round가 늘 같은 쪽만 받는다)."""
    n = len(((getattr(ctx, "config", None) or {}).get("editable_blocks")) or ["prompt"])
    return "skills" if ALTERNATE and ctx.block_name == "prompt" and ((ctx.round - 1) // n) % 2 == 0 else "prompt"


def split_block(src: str) -> tuple[str, str | None]:
    """prompt 블록을 (나머지, 마지막 SKILLS 정의 줄)로 나눈다. SKILLS 적용 wrapper(_base_build_messages)는 버리고,
    build_messages가 여러 번 정의돼 있으면 마지막 것만 남긴다. SyntaxError는 호출한 쪽이 처리한다."""
    lines, drop, skills, defs = src.splitlines(), set(), None, []
    for n in ast.parse(src).body:
        span = set(range(n.lineno - 1, n.end_lineno))
        target = n.targets[0] if isinstance(n, ast.Assign) else n.target if isinstance(n, ast.AnnAssign) else None
        name = target.id if isinstance(target, ast.Name) else None
        if name == "SKILLS":
            skills = "\n".join(lines[n.lineno - 1:n.end_lineno]); drop |= span
        elif name == "_base_build_messages":
            drop |= span
        elif isinstance(n, ast.FunctionDef) and n.name == "build_messages":
            if any(isinstance(x, ast.Name) and x.id == "_base_build_messages" for x in ast.walk(n)):
                drop |= span
            else:
                defs.append(span)
    for span in defs[:-1]:
        drop |= span
    core = "\n".join(l for i, l in enumerate(lines) if i not in drop)
    return core.strip("\n"), skills


def _pool_samples(ctx: RoundContext, n: int) -> str:
    if n == 0:
        return "few-shot pool: (not shown)"
    pool = json.loads(Path(ctx.config["fewshot_pool"]).read_text())
    rows = []
    for ex in pool[:n]:
        rows.append(f"- {ex['id']} ({ex['db_id']}, {ex.get('hardness','')}): {ex['question']}")
    return f"few-shot pool: {len(pool)} examples. First {n} ids:\n" + "\n".join(rows)


def build_prompt(ctx: RoundContext) -> list[dict]:
    wrong = target_wrong(ctx)
    random.Random(ctx.seed).shuffle(wrong)   # round마다 다른 오답 표본 (같은 입력 → 같은 patch 반복을 막는다)
    wrong = wrong[: knob(ctx, "n_wrong")]
    wrong_txt = "\n".join(f"- {ctx.problem(p['id'])['question']}\n  predicted: {ctx.prediction(p['id'])}\n  error: {p['error']}" for p in wrong)
    log_txt = "\n".join(json.dumps({k: r.get(k) for k in ("round", "block", "reason", "accepted", "rejected_by", "net")}) for r in ctx.recent_log)
    b = ctx.block_name
    a = aspect(ctx)
    contract = CONTRACTS["skills"] if a == "skills" else CONTRACTS.get(b, f"Block {b!r}")
    shown = ctx.block
    if b == "prompt" and ALTERNATE:
        try:
            core, skills = split_block(ctx.block)
            shown = (core if a == "prompt" else ctx.block) + (f"\n\n# kept by the loop this round (read-only):\n{skills}" if a == "prompt" and skills else "")
        except SyntaxError:
            pass
    guidance = knob(ctx, "guidance")
    user = (f"Validation score: {ctx.before['correct']}/{ctx.before['n']}\nDiagnosis: {ctx.diagnosis}\n\n"
            f"This round, patch block '{b}' (BLOCK: {b}).\n\n{contract}\n\n"
            + (f"Guidance: {guidance}\n\n" if guidance else "")
            + (f"Focus: fix only the {'SQL that fails to run' if target(ctx) == 'exec_error' else 'SQL that runs but returns the wrong result'} "
               "(examples below). Make the smallest change that fixes them; do not rewrite unrelated instructions, so correct answers stay correct.\n\n"
               if target(ctx) != "any" else "")
            + f"{_pool_samples(ctx, knob(ctx, 'show_pool'))}\n\nWrong examples:\n{wrong_txt}\n\nRecent rounds:\n{log_txt or '(none)'}\n\n"
            + "".join(f"Other block '{o}' (read-only context, do not patch it):\n{src}\n\n" for o, src in (ctx.blocks or {}).items() if o != b)
            + f"Current block '{b}':\n<<<CURRENT_BLOCK {b}>>>\n{shown}\n<<<END_BLOCK>>>")
    return [{"role": "system", "content": ctx.prompt_text("patch.md")}, {"role": "user", "content": user}]
