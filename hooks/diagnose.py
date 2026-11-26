"""틀린 문제를 보고 Improver에게 줄 진단을 어떻게 만들지.
6주차: BinEval(arXiv 2606.27226) 예/아니오 질문 진단. 4주차 SkillAxe의 Compliance(규칙 추출·0~1 판정)를 바꾼다.
- 질문은 prompt 규칙이 아니라 과제 설명에서 만든다 (§3.1). 첫 호출에 LLM 1회로 만들어 logs/bineval_questions.json에 두고 다시 쓴다.
- 틀린 SQL과 맞힌 SQL을 표시 없이 섞어 한 번에 묻는다 (§3.2, LLM 1회). 맞힌 SQL에 "아니오"가 많은 질문은 믿지 않는다 (논문에 없는 검증).
  평가자는 SQL과 그 실행 결과 앞 3행을 보고, SQL마다 질문 수만큼 Y/N 글자를 모두 적는다 (빈 목록이 기본 답이 되지 않게).
- 질문은 실행은 되지만 결과가 틀린 SQL을 겨냥한다. 생성 지시가 바뀌면(QUESTIONS_SHA) 질문 파일을 다시 만든다.
- 답 패턴이 거의 같은 질문은 한 묶음으로 센다 (6주차 세션 정리 질문 1).
- DIAGNOSE_MODE=holistic이면 대조군: 같은 오답과 같은 실행 결과를 "무엇이 틀렸나"로 통째로 한 번 묻는다 (세션 정리 질문 2).
  code round의 reachability 규칙(build_prompt·review_patch)은 두 조건에 똑같이 적용된다. 그래서 두 조건의 차이는 prompt round 진단에서만 생긴다.
prompt round: Binary(또는 Holistic) + Trigger + Quality / code round: 실행 오류 집계 + Quality (LLM 0회)."""
from __future__ import annotations
import ast, hashlib, json, os, random, re
from collections import Counter
from pathlib import Path
from loop.context import RoundContext
from hooks.build_prompt import route_always_end, target_wrong
from sql_agent.schema import schema_text

KNOBS = {
    "diagnose_n": ("int", 5, 25, 15),     # 진단에 보여 줄 틀린 문제 수
    "diagnose_focus": ("str", 300, ""),   # 진단할 때 무엇을 먼저 보라는 한 문장
}
KNOB_DOCS = {
    "diagnose_n": "how many wrong SQL queries the yes/no check reads (a few correct ones are mixed in)",
    "diagnose_focus": "an instruction sentence telling the yes/no check which kind of failure to look at first",
}

RECENT_ROUNDS = 20   # 거부된 patch가 깨뜨린 문제를 몇 round 뒤까지 볼지
TOP_BROKEN = 5       # brief에 적는 "반복해서 깨진 문제" 수
QUESTION_N = 8       # 과제에서 만들 예/아니오 질문 수
N_CORRECT = 5        # 섞어서 묻는 맞힌 SQL 수
DROP_AT = 2          # 맞힌 SQL에서 "아니오"가 이만큼 나온 질문은 진단에서 뺀다
SAME = 0.9           # 두 질문의 답이 이 비율 이상 같으면 한 묶음 (SQL 20개 중 18개)
TOP_Q = 3            # 진단에 적는 질문 묶음 수
MODE = os.environ.get("DIAGNOSE_MODE", "binary")   # binary | holistic (대조군)

ROWS = 3            # 평가자에게 보여 줄 실행 결과 행 수

TASK = "Given a natural-language question and a database schema, write one SQLite SQL query that returns the answer."
QUESTIONS_SYSTEM = f"""You design checks for a Text-to-SQL task. Task: {TASK}
Step 1: list the distinct requirements that a correct answer must meet.
Step 2: for each requirement, write one yes/no question about a predicted SQL query and its first result rows. "yes" means the requirement is met.
Target SQL that runs but returns the wrong result: wrong or extra columns, wrong or missing filters, wrong aggregation or counting,
wrong joins, wrong grouping or ordering, wrong limit. Do not ask whether the SQL runs or whether names exist in the schema:
execution already shows that.
Give each question one short violation example.
Return JSON only: {{"questions": [{{"id": "Q1", "text": "...", "violation": "..."}}]}}. Write {QUESTION_N} questions."""
QUESTIONS_SHA = hashlib.sha1(QUESTIONS_SYSTEM.encode()).hexdigest()[:12]   # 질문 생성 지시가 바뀌면 질문 파일을 다시 만든다
# 모든 (질문, SQL) 판정을 글자로 받는다. "아니오"만 적게 하면 빈 목록이 기본 답이 되어 진단이 비었다 (6주차 2차 실행 9/10)
ANSWER_SYSTEM = """You check predicted SQL queries for a Text-to-SQL task. You get yes/no questions, database schemas and numbered queries
(database, question, predicted SQL, and its first result rows or its error). You do not get the correct SQL.
Answer every question for every query independently. For each query write one letter per question, in question order: Y = yes, N = no.
Give a reason of at most 12 words for each N, with no quotes and no SQL in it.
Return JSON only: {"answers": {"S1": {"v": "EXAMPLE", "why": {"Q3": "reason"}}}}. Include every query."""
HOLISTIC_SYSTEM = """You review wrong answers of a Text-to-SQL agent. You get database schemas and wrong answers
(database, question, predicted SQL, and its first result rows or its error). Name the 3 most common failure types.
For each, say how many answers it covers and give one example. Plain text, at most 8 lines."""


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


def _schemas(ctx: RoundContext, items: list[dict]) -> str:
    return "\n".join(f"{db}:\n{schema_text(ctx.config['tables'], db)}" for db in dict.fromkeys(ctx.problem(p["id"])["db_id"] for p in items))


def _questions(ctx: RoundContext) -> list[dict]:
    """질문 묶음. 같은 지시(QUESTIONS_SHA)로 만든 파일이 있으면 읽고, 없으면 LLM 1회로 만들어 둔다
    (round마다 질문이 바뀌면 진단끼리 비교할 수 없다). 지시가 바뀌면 파일을 덮어쓴다."""
    path = Path(ctx.config.get("log", "logs/rounds.jsonl")).parent / "bineval_questions.json"
    if path.exists():
        saved = json.loads(path.read_text())
        if isinstance(saved, dict) and saved.get("prompt_sha") == QUESTIONS_SHA:
            return saved["questions"]
    qs = _ask_json(ctx, QUESTIONS_SYSTEM, TASK)["questions"]
    if not (isinstance(qs, list) and qs and all(isinstance(q, dict) and q.get("id") and q.get("text") for q in qs)
            and len({q["id"] for q in qs}) == len(qs)):
        raise ValueError(f"bad question list: {qs!r}"[:300])   # 형식이 틀린 질문은 파일에 남기지 않는다
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"prompt_sha": QUESTIONS_SHA, "questions": qs}, ensure_ascii=False, indent=1))
    return qs


def _run_sql(db_id: str, sql: str, limit: int, timeout: float) -> dict:
    from sql_agent.run import try_sql   # 블록 밖의 읽기 전용 실행 도구. 무거운 import라 진단할 때만 읽는다
    return try_sql(db_id, sql, limit=limit, timeout=timeout)


def _result(ctx: RoundContext, p: dict) -> str:
    """예측 SQL을 다시 실행한 앞 ROWS행 또는 오류. 정답 없이 실행만으로 보이는 값이다.
    BinEval 평가자는 원문과 출력을 함께 본다 (§3.2). SQL 문장만 보면 '개수를 물었는데 이름을 돌려준' 오답을 짚을 수 없다."""
    r = _run_sql(ctx.problem(p["id"])["db_id"], ctx.prediction(p["id"]), ROWS, float(ctx.config.get("sql_timeout_sec", 5)))
    if not r["ok"]:
        return f"error: {r['error']}"
    return ("rows: " + "; ".join(repr(row) for row in r["rows"]))[:300] if r["rows"] else "rows: (none)"


def _query_text(ctx: RoundContext, label: str, p: dict) -> str:
    return (f"{label}\nDB: {ctx.problem(p['id'])['db_id']}\nQ: {ctx.problem(p['id'])['question']}\n"
            f"SQL: {ctx.prediction(p['id'])}\nresult: {_result(ctx, p)}")


def binary(ctx: RoundContext, wrong: list[dict], correct: list[dict], focus: str) -> str:
    """틀린 SQL과 맞힌 SQL을 섞어 질문별 예/아니오를 받고, "아니오"가 많은 질문 묶음을 고른다 (질문 생성 첫 1회 + LLM 1회)."""
    rows = [(p, True) for p in wrong] + [(p, False) for p in correct]
    random.Random(ctx.seed).shuffle(rows)   # 틀린 SQL인지가 순서로 드러나지 않게 섞는다
    labels = [(f"S{n}", w) for n, (_, w) in enumerate(rows, 1)]
    try:
        qs = _questions(ctx)
        ids = [q["id"] for q in qs]
        text_of = {q["id"]: q["text"] for q in qs}
        example = ("YYN" + "Y" * len(ids))[: len(ids)]
        system = ANSWER_SYSTEM.replace("EXAMPLE", example) + (f"\nFocus: {focus}" if focus else "")
        # 정답과 비교해야 아는 값(result mismatch)은 넣지 않는다. 실행 결과는 맞힌 SQL에도 똑같이 보여 준다
        queries = "\n\n".join(_query_text(ctx, s, p) for (s, _), (p, _) in zip(labels, rows))
        user = (f"Questions:\n{json.dumps([{'id': q['id'], 'text': q['text'], 'violation': q.get('violation', '')} for q in qs])}\n\n"
                f"Schemas:\n{_schemas(ctx, [p for p, _ in rows])}\n\nQueries:\n{queries}")
        ans = _ask_json(ctx, system, user)["answers"]
        if not isinstance(ans, dict):
            raise TypeError(f"answers is {type(ans).__name__}, not an object")
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as e:   # 진단 없이 round는 진행한다. 실패 사실은 brief 맨 앞에 남는다
        return f"[Binary] unavailable: {e!r}"
    got: dict[str, dict[str, str]] = {}
    for s, _ in labels:
        a = ans.get(s)
        v = str(a.get("v", "")).strip().upper() if isinstance(a, dict) else ""
        if len(v) == len(ids) and set(v) <= {"Y", "N"}:   # 글자 수가 틀리거나 빠진 SQL은 세지 않는다
            why = a.get("why") if isinstance(a.get("why"), dict) else {}
            got[s] = {q: str(why.get(q, "")) for q, c in zip(ids, v) if c == "N"}
    seen = [(s, w) for s, w in labels if s in got]
    nw, nc = sum(w for _, w in seen), sum(not w for _, w in seen)
    if not nw:
        return f"[Binary] unavailable: no wrong query was answered in the format ({len(seen)}/{len(labels)} answered)"
    vec = {q: [q in got[s] for s, _ in seen] for q in ids}
    no_w = {q: sum(v and w for v, (_, w) in zip(vec[q], seen)) for q in ids}
    no_c = {q: sum(v and not w for v, (_, w) in zip(vec[q], seen)) for q in ids}
    # 첫 줄은 rounds.jsonl의 diagnosis 200자에 남는다. 평가자를 믿을 수 있는 정도를 여기에 둔다
    head = (f"[Binary] answered {len(seen)}/{len(labels)}; wrong SQL with a 'no': {sum(bool(got[s]) for s, w in seen if w)}/{nw}; "
            f"'no' on correct SQL: {sum(no_c.values())}/{nc * len(ids)}")
    groups: list[list[str]] = []
    for q in sorted((q for q in ids if no_c[q] < DROP_AT and no_w[q] > 0), key=lambda q: -no_w[q]):
        g = next((g for g in groups if sum(a == b for a, b in zip(vec[g[0]], vec[q])) >= SAME * len(seen)), None)
        if g:
            g.append(q)
        else:
            groups.append([q])
    lines = [head]
    for g in groups[:TOP_Q]:
        why = [got[s][g[0]] for s, w in seen if w and g[0] in got[s]][:2]
        lines.append(f"- \"{text_of[g[0]]}\": 'no' on {no_w[g[0]]} of {nw} wrong SQL"
                     + (f" (same pattern: {'; '.join(text_of[x] for x in g[1:])})" if g[1:] else "")
                     + "".join(f"\n  e.g. {r}" for r in why))
    if not groups:
        lines.append("No question failed on these wrong answers.")
    if dropped := sum(no_c[q] >= DROP_AT for q in ids):
        lines.append(f"({dropped} questions ignored: they also said 'no' on correct SQL)")
    return "\n".join(lines)


def holistic(ctx: RoundContext, wrong: list[dict], focus: str) -> str:
    """대조군: 같은 오답을 통째로 한 번 묻는다 (LLM 1회)."""
    examples = "\n\n".join(_query_text(ctx, f"W{n}", p) for n, p in enumerate(wrong, 1))   # binary와 같은 입력 (실행 결과 행)
    system = HOLISTIC_SYSTEM + (f"\nFocus: {focus}" if focus else "")
    text = ctx.llm([{"role": "system", "content": system}, {"role": "user", "content": f"Schemas:\n{_schemas(ctx, wrong)}\n\nWrong answers:\n{examples}"}],
                   role="improver", max_tokens=ctx.config.get("improver_max_tokens", 4096))
    return "[Holistic] " + text.strip()


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
    wrong = [p for p in target_wrong(ctx) if not str(p["error"]).startswith("gold failed")]   # 정답 SQL이 깨진 문제는 agent 실패가 아니다
    if not wrong:
        return "no wrong answers"
    random.Random(ctx.seed).shuffle(wrong)
    wrong = wrong[: k("diagnose_n")]
    if ctx.block_name != "prompt":
        # 실행 오류를 고칠 fix()가 실행되지 않는 상태면 그것부터 알린다 (LLM 없음)
        reach = ["[Reachability] route always returns END, so fix() never runs: make route send failed SQL to fix."] if route_always_end(ctx.block) else []
        return "\n".join(reach + [exec_errors(ctx), quality(ctx)])
    if MODE == "holistic":
        first = holistic(ctx, wrong, k("diagnose_focus"))
    else:
        correct = [p for p in ctx.before["per_problem"] if p["correct"]]
        random.Random(ctx.seed + 1).shuffle(correct)
        first = binary(ctx, wrong, correct[:N_CORRECT], k("diagnose_focus"))
    # rounds.jsonl은 diagnosis 앞 200자만 남긴다. 실패할 수 있는 축을 앞에 둔다
    return "\n".join([first, trigger(ctx), quality(ctx)])


if __name__ == "__main__":   # python -m hooks.diagnose : 가짜 ctx로 확인 (LLM·git 없음)
    import tempfile

    d = Path(tempfile.mkdtemp())
    (d / "r.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"commit": "P", "fixed_ids": ["a", "b"], "broken_ids": ["c"]},
        {"commit": "x", "rejected_by": "accept", "broken_ids": ["d"]},
        {"commit": "y", "rejected_by": "accept", "broken_ids": ["d", "e"]}]))
    per = [{"id": "a", "correct": False, "error": "result mismatch"}, {"id": "b", "correct": False, "error": "OperationalError: no such column: T1.x"},
           {"id": "d", "correct": True, "error": None}, {"id": "e", "correct": False, "error": "result mismatch"},
           {"id": "f", "correct": True, "error": None}, {"id": "g", "correct": True, "error": None},
           {"id": "h", "correct": False, "error": "gold failed: x"}]
    preds = {"a": "SELECT BAD a", "b": "SELECT BAD b", "e": "SELECT BAD e", "d": "SELECT ok", "f": "SELECT ok", "g": "SELECT ok", "h": "SELECT h"}
    QS = [{"id": "Q1", "text": "Right columns?"}, {"id": "Q2", "text": "Valid SQLite?"}, {"id": "Q3", "text": "Right tables?"}, {"id": "Q4", "text": "Never fails"}]
    calls: list[str] = []
    users: list[str] = []

    def fake(msgs: list[dict], **kw) -> str:
        calls.append(msgs[0]["content"]); users.append(msgs[1]["content"])
        if msgs[0]["content"] == QUESTIONS_SYSTEM:
            return "```json\n" + json.dumps({"questions": QS}) + "\n```"
        if msgs[0]["content"].startswith(HOLISTIC_SYSTEM):
            return " joins are wrong (3)\n"
        # Q1·Q3: 틀린 SQL에만 N (같은 패턴), Q2: 모든 SQL에 N (맞힌 SQL에도 → 뺀다), Q4: 늘 Y. "BAD e"는 답을 빠뜨리고, S99는 없는 번호
        out = {"S99": {"v": "NNNN"}}
        for s, sql in re.findall(r"^(S\d+)\n(?:.*\n){2}SQL: (.*)$", msgs[1]["content"], re.M):
            if "BAD e" not in sql:
                bad_ = "BAD" in sql
                out[s] = {"v": ("N" if bad_ else "Y") + "N" + ("N" if bad_ else "Y") + "Y",
                          "why": {"Q2": "r", **({"Q1": "cols " + sql, "Q3": "tabs"} if bad_ else {})}}
        return json.dumps({"answers": out})

    schema_text = lambda tables, db: f"schema of {db}"   # 전역을 바꿔 data 없이 확인
    _run_sql = lambda db, sql, limit, timeout: ({"ok": False, "rows": [], "error": "OperationalError: no such column: T1.x"} if "BAD b" in sql
                                                else {"ok": True, "rows": [(1, "x")], "error": None})
    block = 'SYSTEM_PROMPT = "x"\nSKILLS = [{"when": ["How many"], "rule": "r"}, {"when": ["zzz"], "rule": "q"}]\ndef build_messages(q):\n    return SKILLS'
    mk = lambda name, b, llm_=fake: RoundContext(round=1, week=6, config={"log": str(d / "r.jsonl"), "tables": "t"}, parent="P", block_name=name, block=b,
                                                 before={"per_problem": per, "correct": 3, "n": 7},
                                                 problems={i: {"question": "how many " + i, "db_id": "db1"} for i in "abdefgh"}, predictions=preds, llm=llm_)
    (d / "bineval_questions.json").write_text(json.dumps(QS))                       # 옛 형식(지시 sha 없음) 파일은 다시 만든다
    out = diagnose(mk("prompt", block))
    assert len(calls) == 2 and calls[0] == QUESTIONS_SYSTEM, calls                  # 첫 호출: 질문 만들기 + 답하기
    assert json.loads((d / "bineval_questions.json").read_text()) == {"prompt_sha": QUESTIONS_SHA, "questions": QS}
    assert "result mismatch" not in users[1] and "gold failed" not in users[1] and "SELECT h" not in users[1], users[1]   # 정답에서 온 정보는 숨긴다
    assert "result: error: OperationalError" in users[1] and "result: rows: (1, 'x')" in users[1], users[1]               # 실행 결과 행을 보여 준다
    assert users[1].count("schema of db1") == 1 and '"v": "YYNY"' in calls[1], calls[1]                                   # 예시 글자 수 = 질문 수
    first = out.splitlines()[0]
    assert first == "[Binary] answered 5/6; wrong SQL with a 'no': 2/2; 'no' on correct SQL: 3/12", out   # 빠진 SQL은 세지 않고 그 수를 적는다
    allyes = lambda m, **kw: json.dumps({"answers": {f"S{n}": {"v": "YYYY"} for n in range(1, 7)}})
    assert diagnose(mk("prompt", block, allyes)).startswith("[Binary] answered 6/6; wrong SQL with a 'no': 0/3; 'no' on correct SQL: 0/12\nNo question failed")
    short = lambda m, **kw: json.dumps({"answers": {f"S{n}": {"v": "YY"} for n in range(1, 7)}})
    assert "no wrong query was answered in the format (0/6 answered)" in diagnose(mk("prompt", block, short))   # 글자 수가 틀린 답은 버린다
    assert "not an object" in diagnose(mk("prompt", block, lambda m, **kw: '{"answers": []}'))
    assert '"Right columns?"' in out and "same pattern: Right tables?" in out, out   # Q1·Q3은 한 묶음
    assert "Valid SQLite?" not in out and "1 questions ignored" in out, out           # 맞힌 SQL에도 "no"인 Q2는 뺀다
    assert "Never fails" not in out and "e.g. cols SELECT BAD" in out, out
    assert "fixed 2 and broke 1" in out and "(broken 2x) how many d" in out and "how many e" not in out, out
    assert "fired 7, correct 3" in out and "never fires" in out, out
    calls.clear()
    diagnose(mk("prompt", block))
    assert len(calls) == 1, calls                                                    # 질문 파일이 있으면 다시 만들지 않는다
    calls.clear()
    out = diagnose(mk("code", block))
    assert calls == [] and "OperationalError: no such column (1 times)" in out and "[Binary]" not in out and "[Reachability]" not in out, out
    out = diagnose(mk("code", "def route(s):\n    return END"))
    assert out.startswith("[Reachability] route always returns END") and "OperationalError" in out, out
    (d / "bineval_questions.json").unlink()
    bad = diagnose(mk("prompt", block, lambda m, **kw: "not json"))
    assert bad.startswith("[Binary] unavailable") and "[Trigger]" in bad and not (d / "bineval_questions.json").exists(), bad   # 형식 오류: round는 진행, 파일은 안 남김
    bad = diagnose(mk("prompt", block, lambda m, **kw: json.dumps({"questions": [{"id": "Q1", "text": "a"}, {"id": "Q1", "text": "b"}]})))
    assert "bad question list" in bad, bad                                           # id가 겹치는 질문 목록은 거부
    MODE = "holistic"
    calls.clear(); users.clear()
    out = diagnose(mk("prompt", block))
    assert len(calls) == 1 and out.startswith("[Holistic] joins are wrong (3)") and "[Trigger]" in out, out
    assert "result: rows: (1, 'x')" in users[0] and "result mismatch" not in users[0], users[0]   # binary와 같은 입력
    assert "No SKILLS yet" in trigger(mk("prompt", 'SYSTEM_PROMPT = "x"'))
    assert "could not be read" in trigger(mk("prompt", "SKILLS = [make()]"))
    assert "unexpected shape" in trigger(mk("prompt", "SKILLS = [1]\nf(SKILLS)"))
    assert "could not be read" in trigger(mk("prompt", "SKILLS = {[]: 1}"))       # literal_eval의 TypeError
    assert "never used" in trigger(mk("prompt", 'SKILLS = [{"when": ["a"], "rule": "r"}]'))
    assert "No round record" in quality(RoundContext(round=1, week=6, config={"log": str(d / "none")}, parent="Z", block_name="code", block="", before={"per_problem": per, "correct": 3, "n": 7}, problems={}, predictions={}))
    print("diagnose ok")
