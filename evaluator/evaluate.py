"""execution accuracy evaluator. validation과 test가 같은 함수를 쓴다."""
from __future__ import annotations
import json, re, sqlite3
from collections import Counter
from pathlib import Path


def _run(db: Path, sql: str, timeout: float) -> list[tuple]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=timeout)
    try:
        con.execute(f"PRAGMA busy_timeout={int(timeout*1000)}")
        # 긴 쿼리 중단: progress handler로 timeout 흉내
        import time
        deadline = time.monotonic() + timeout
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    return [tuple(r) for r in rows]


def _norm(rows: list[tuple]) -> list[tuple]:
    out = []
    for r in rows:
        out.append(tuple(round(v, 6) if isinstance(v, float) else v for v in r))
    return out


def evaluate(pred_path: str, problems_path: str, db_dir: str, sql_timeout: float = 5.0, rules=None) -> dict:
    """rules: select_problems / compare 를 가진 모듈. None이면 고정본 rules_v0 (test 채점용).
    loop는 evaluator.rules 를 넘긴다 (6주차에 Improver가 고칠 수 있는 블록)."""
    if rules is None:
        from evaluator import rules_v0 as rules
    problems = {p["id"]: p for p in rules.select_problems(json.loads(Path(problems_path).read_text()))}
    preds: dict[str, str] = {}
    for line in Path(pred_path).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
            preds[o["id"]] = o.get("sql", "")
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    per = []
    for pid, prob in problems.items():
        db = Path(db_dir) / prob["db_id"] / f"{prob['db_id']}.sqlite"
        sql = preds.get(pid)
        if not sql:
            per.append({"id": pid, "correct": False, "error": "no prediction"})
            continue
        try:
            gold_rows = _run(db, prob["gold_sql"], sql_timeout)
        except Exception as e:  # gold가 안 돌면 데이터 문제. 틀린 것으로 세지 않고 표시만.
            per.append({"id": pid, "correct": False, "error": f"gold failed: {e}"})
            continue
        try:
            pred_rows = _run(db, sql, sql_timeout)
        except Exception as e:
            per.append({"id": pid, "correct": False, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        ok = bool(rules.compare(_norm(pred_rows), _norm(gold_rows), prob["gold_sql"]))
        per.append({"id": pid, "correct": ok, "error": None if ok else "result mismatch"})
    correct = sum(1 for x in per if x["correct"])
    n = len(per)
    wrong = [x for x in per if not x["correct"]]
    errors = Counter((x["error"] or "").split(":")[0] for x in wrong)
    feedback = f"{correct}/{n} correct. wrong by type: " + ", ".join(f"{k}={v}" for k, v in errors.most_common())
    return {"score": correct / n if n else 0.0, "n": n, "correct": correct, "per_problem": per, "feedback": feedback}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True); ap.add_argument("--problems", required=True); ap.add_argument("--db-dir", required=True)
    a = ap.parse_args()
    r = evaluate(a.pred, a.problems, a.db_dir)
    print(json.dumps({k: v for k, v in r.items() if k != "per_problem"}, ensure_ascii=False))
