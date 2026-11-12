"""틀린 문제를 보고 Improver에게 줄 진단을 어떻게 만들지. 4주차: SkillAxe(arXiv 2606.10546) 진단 brief.
Quality Impact는 정답 채점 기록으로 대체, Trigger는 SKILLS의 실제 발동 수로 대체, Compliance는 일괄 판정, Solution-Path Coverage는 뺌.
prompt round: Quality + Compliance(LLM 2회) + Trigger / code round: Quality + 실행 오류 집계(LLM 0회)."""
from __future__ import annotations
import ast, json, random, re
from collections import Counter
from pathlib import Path
from loop.context import RoundContext
from hooks.build_prompt import target_wrong
from sql_agent.schema import schema_text

KNOBS = {
    "diagnose_n": ("int", 5, 25, 15),     # 진단에 보여 줄 틀린 문제 수
    "diagnose_focus": ("str", 300, ""),   # 진단할 때 무엇을 먼저 보라는 한 문장
}
KNOB_DOCS = {
    "diagnose_n": "how many wrong examples the rule-compliance judgment reads",
    "diagnose_focus": "an instruction sentence telling the rule-compliance judgment which kind of failure to look at first",
}

RECENT_ROUNDS = 20   # 거부된 patch가 깨뜨린 문제를 몇 round 뒤까지 볼지
TOP_BROKEN = 5       # brief에 적는 "반복해서 깨진 문제" 수
LOW = 0.5            # a·g·f를 낮다고 볼 기준

RULES_SYSTEM = """Extract the actionable rules from this Text-to-SQL prompt block (SYSTEM_PROMPT and SKILLS).
Return JSON only: {"rules": [{"id": "R1", "text": "<the rule>"}]}. Use at most 12 rules. If two rules conflict, keep both."""
JUDGE_SYSTEM = """You judge whether a Text-to-SQL agent's failures come from its rules or from the agent ignoring them.
You get the rules, the database schemas and wrong answers (database, question, predicted SQL, error). For each rule return:
relevant = how many of the wrong answers the rule applies to,
a = 0 to 1, how well the agent followed the rule on those answers,
g = 0 to 1, how correct and usable the rule itself is,
f = 0 to 1, how much the failures come from the rule (1) rather than from the agent ignoring a valid rule (0),
note = one short sentence.
Return JSON only: {"judgments": [{"id": "R1", "relevant": 0, "a": 1.0, "g": 1.0, "f": 0.0, "note": ""}]}."""


def _ask_json(ctx: RoundContext, system: str, user: str) -> dict:
    text = ctx.llm([{"role": "system", "content": system}, {"role": "user", "content": user}],
                   role="improver", max_tokens=ctx.config.get("improver_max_tokens", 4096))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in the reply")
    return json.loads(m.group(0))


def quality(ctx: RoundContext) -> str:
    """이 parent를 만든 patch의 결과와, 최근 거부된 patch가 반복해서 깨뜨린 문제. 정답 기록만 쓴다 (LLM 없음)."""
    log = Path(ctx.config.get("log", "logs/rounds.jsonl"))
    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
    own = next((e for e in reversed(entries) if e.get("commit") == ctx.parent and e.get("fixed_ids") is not None), None)
    head = (f"[Quality] The patch that made this version fixed {len(own['fixed_ids'])} and broke {len(own['broken_ids'])} questions."
            if own else "[Quality] No round record for this version.")   # v0와 promote가 만든 champion 복사 commit
    correct_now = {p["id"] for p in ctx.before["per_problem"] if p["correct"]}
    broken = Counter(i for e in entries[-RECENT_ROUNDS:] if e.get("rejected_by") == "accept"
                     for i in e.get("broken_ids") or [] if i in correct_now)
    lines = [f"- (broken {n}x) {ctx.problem(i)['question']}" for i, n in broken.most_common(TOP_BROKEN) if n >= 2]
    return "\n".join([head] + (["Recently rejected patches kept breaking these correct questions; keep them correct:"] + lines if lines else []))


def _skills(block: str) -> list[dict] | None:
    """prompt 블록의 SKILLS 리터럴. 없으면 None. 실행하지 않고 AST로만 읽는다."""
    for n in ast.parse(block).body:
        target = n.targets[0] if isinstance(n, ast.Assign) else n.target if isinstance(n, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == "SKILLS" and n.value is not None:
            return ast.literal_eval(n.value)
    return None


def trigger(ctx: RoundContext) -> str:
    """SKILLS별 발동 수와 정답률 (LLM 없음).
    shortcut: build_messages가 "when"을 소문자 포함 검사로 쓴다고 가정한다. 다르게 쓰는 patch가 나오면 실제 발동과 어긋나므로 그때 build_messages를 직접 호출하도록 바꾼다."""
    try:
        skills = _skills(ctx.block)
    except (SyntaxError, ValueError, TypeError) as e:
        return f"[Trigger] SKILLS could not be read: {e}"
    if not skills:
        return "[Trigger] No SKILLS yet."
    if not any(isinstance(n, ast.Name) and n.id == "SKILLS" and isinstance(n.ctx, ast.Load) for n in ast.walk(ast.parse(ctx.block))):
        return "[Trigger] SKILLS is defined but never used, so it has no effect: make build_messages use it."
    rows = [(ctx.problem(p["id"])["question"].lower(), p["correct"]) for p in ctx.before["per_problem"]]
    lines = ["[Trigger]"]
    try:
        for s in skills:
            words = [w.lower() for w in s["when"]]
            fired = [ok for q, ok in rows if any(w in q for w in words)]
            lines.append(f"- when={s['when']}: fired {len(fired)}, correct {sum(fired)}" + (" (never fires: fix or remove)" if not fired else ""))
    except (KeyError, TypeError, AttributeError) as e:
        return f"[Trigger] SKILLS has an unexpected shape: {e!r}"
    return "\n".join(lines)


def _verdict(a: float, g: float, f: float) -> str:
    if g < LOW:
        return "rule is wrong or unclear: revise or remove"
    if a >= LOW:
        return "followed: do not blame this rule"
    return "agent ignored a valid rule: keep it, state it more explicitly" if f < LOW else "the rule's guidance led to the failure: revise"


def compliance(ctx: RoundContext, wrong: list[dict], focus: str) -> str:
    """규칙을 한 번 뽑고 오답을 일괄 판정해, 규칙을 고칠지 유지할지 나눈다 (LLM 2회)."""
    examples = "\n".join(f"DB: {ctx.problem(p['id'])['db_id']}\nQ: {ctx.problem(p['id'])['question']}\nSQL: {ctx.prediction(p['id'])}\nerror: {p['error']}" for p in wrong)
    try:
        schemas = "\n".join(f"{db}:\n{schema_text(ctx.config['tables'], db)}" for db in dict.fromkeys(ctx.problem(p["id"])["db_id"] for p in wrong))
        rules = {r["id"]: r["text"] for r in _ask_json(ctx, RULES_SYSTEM, ctx.block)["rules"]}
        if not rules:
            return "[Compliance] No actionable rules found."
        system = JUDGE_SYSTEM + (f"\nFocus: {focus}" if focus else "")
        out = _ask_json(ctx, system, f"Rules:\n{json.dumps(rules)}\n\nSchemas:\n{schemas}\n\nWrong answers:\n{examples}")["judgments"]
        lines = ["[Compliance]"]
        for j in out:
            if j["id"] in rules and int(j["relevant"]) > 0:
                lines.append(f"- {j['id']} \"{rules[j['id']]}\" (applies to {int(j['relevant'])}): "
                             f"{_verdict(float(j['a']), float(j['g']), float(j['f']))}. {j.get('note', '')}")
    except (ValueError, KeyError, TypeError) as e:   # 진단 없이 round는 진행한다. 실패 사실은 brief 맨 앞에 남는다
        return f"[Compliance] unavailable: {e!r}"
    return "\n".join(lines) if len(lines) > 1 else "[Compliance] No rule applies to these wrong answers."


def exec_errors(ctx: RoundContext) -> str:
    """실행 오류를 유형별로 센다 (LLM 없음). 이름이 다른 같은 오류를 묶으려고 두 번째 ':' 뒤(컬럼 이름 등)는 버린다."""
    errors = [p["error"] for p in ctx.before["per_problem"]
              if not p["correct"] and p["error"] != "result mismatch" and not p["error"].startswith("gold failed")]
    if not errors:
        return "[Execution] No execution errors."
    kinds = Counter(":".join(e.split(":")[:2]) for e in errors)
    return "[Execution] " + "; ".join(f"{k} ({n} times)" for k, n in kinds.most_common())


def diagnose(ctx: RoundContext) -> str:
    k = lambda name: (ctx.improver or {}).get(name, KNOBS[name][-1])
    wrong = target_wrong(ctx)   # build_prompt의 target_error_class와 같은 유형을 진단한다
    if not wrong:
        return "no wrong answers"
    random.Random(ctx.seed).shuffle(wrong)
    # rounds.jsonl은 diagnosis 앞 200자만 남긴다. 실패할 수 있는 축을 앞에 둔다
    parts = [compliance(ctx, wrong[: k("diagnose_n")], k("diagnose_focus")), trigger(ctx)] if ctx.block_name == "prompt" else [exec_errors(ctx)]
    return "\n".join(parts + [quality(ctx)])


if __name__ == "__main__":   # python -m hooks.diagnose : 가짜 ctx로 확인 (LLM·git 없음)
    import tempfile

    d = Path(tempfile.mkdtemp())
    (d / "r.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"commit": "P", "fixed_ids": ["a", "b"], "broken_ids": ["c"]},
        {"commit": "x", "rejected_by": "accept", "broken_ids": ["d"]},
        {"commit": "y", "rejected_by": "accept", "broken_ids": ["d", "e"]}]))
    per = [{"id": "a", "correct": False, "error": "result mismatch"}, {"id": "b", "correct": False, "error": "OperationalError: no such column: T1.x"},
           {"id": "d", "correct": True, "error": None}, {"id": "e", "correct": False, "error": "result mismatch"}]
    calls: list[str] = []

    def fake(msgs: list[dict], **kw) -> str:
        calls.append(msgs[0]["content"]); users.append(msgs[1]["content"])
        if msgs[0]["content"] == RULES_SYSTEM:
            return '```json\n{"rules": [{"id": "R1", "text": "use COUNT"}, {"id": "R2", "text": "bad"}, {"id": "R3", "text": "ok"}]}\n```'
        return json.dumps({"judgments": [{"id": "R1", "relevant": 2, "a": 0.1, "g": 0.9, "f": 0.1, "note": "n"},
                                         {"id": "R2", "relevant": 1, "a": 0.9, "g": 0.1, "f": 0.9}, {"id": "R3", "relevant": 0, "a": 1, "g": 1, "f": 0},
                                         {"id": "R9", "relevant": 3, "a": 0, "g": 0, "f": 1}]})

    schema_text = lambda tables, db: f"schema of {db}"   # 전역을 바꿔 data 없이 확인
    users: list[str] = []
    block = 'SYSTEM_PROMPT = "x"\nSKILLS = [{"when": ["How many"], "rule": "r"}, {"when": ["zzz"], "rule": "q"}]\ndef build_messages(q):\n    return SKILLS'
    mk = lambda name, b, llm_=fake: RoundContext(round=1, week=4, config={"log": str(d / "r.jsonl"), "tables": "t"}, parent="P", block_name=name, block=b, before={"per_problem": per, "correct": 1, "n": 4},
                                      problems={i: {"question": "how many " + i, "db_id": "db1"} for i in "abde"}, predictions={}, llm=llm_)
    out = diagnose(mk("prompt", block))
    assert "fixed 2 and broke 1" in out and "(broken 2x) how many d" in out, out   # d는 지금 맞고 2번 깨짐
    assert "how many e" not in out, out                                           # e는 지금도 틀렸으므로 "유지"할 대상이 아님
    assert "keep it" in out and "revise or remove" in out and "R3" not in out and "R9" not in out, out
    assert "fired 4, correct 1" in out and "never fires" in out, out
    assert len(calls) == 2 and users[1].count("schema of db1") == 1, users    # 같은 DB 스키마는 한 번만
    assert out.startswith("[Compliance]"), out
    calls.clear()
    out = diagnose(mk("code", block))
    assert calls == [] and "OperationalError: no such column (1 times)" in out and "[Compliance]" not in out, out
    assert "No SKILLS yet" in trigger(mk("prompt", 'SYSTEM_PROMPT = "x"'))
    assert "could not be read" in trigger(mk("prompt", "SKILLS = [make()]"))
    assert "unexpected shape" in trigger(mk("prompt", "SKILLS = [1]\nf(SKILLS)"))
    assert "could not be read" in trigger(mk("prompt", "SKILLS = {[]: 1}"))       # literal_eval의 TypeError
    assert "never used" in trigger(mk("prompt", 'SKILLS = [{"when": ["a"], "rule": "r"}]'))
    bad = diagnose(mk("prompt", block, lambda m, **kw: "not json"))
    assert bad.startswith("[Compliance] unavailable") and "[Trigger]" in bad, bad          # LLM 형식 오류로 round가 멈추지 않고 실패가 드러남
    assert "No round record" in quality(RoundContext(round=1, week=4, config={"log": str(d / "none")}, parent="Z", block_name="code", block="", before={"per_problem": per, "correct": 1, "n": 4}, problems={}, predictions={}))
    print("diagnose ok")
