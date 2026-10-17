"""Spider 원본(train_spider.json·dev.json 또는 HF parquet를 json으로 바꾼 것) → 후보 카탈로그 catalog.jsonl.
data/database에 있는 DB의 문제만 쓴다. gold가 안 돌거나 결과가 비면 뺀다. v0(현재 sql_agent)를 돌려 틀린 방식을 붙인다.
운영자가 한 번 실행한다 (LLM 호출 약 1,400회, gpt-4.1-nano 기준 몇 분·몇십 원).

  uv run python -m scripts.operator.make_catalog --train train.json --dev dev.json --out catalog.jsonl
  (HF: uv run --with pyarrow python -c "import pyarrow.parquet as q,json;json.dump(q.read_table('train-00000-of-00001.parquet').to_pylist(),open('train.json','w'))")
"""
from __future__ import annotations
import argparse, json, os, re, sqlite3, subprocess, sys, time
from collections import Counter
from pathlib import Path

DB = Path(os.environ.get("STARTER_DATA_DIR", "data")) / "database"
TAGS = [("join", r" JOIN "), ("group", r"GROUP BY"), ("having", r"HAVING"), ("order", r"ORDER BY"), ("limit", r" LIMIT "),
        ("setop", r" (INTERSECT|UNION|EXCEPT) "), ("agg", r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\("), ("distinct", r" DISTINCT "),
        ("like", r" LIKE "), ("multi_cond", r"\b(AND|OR)\b")]


def run(db_id: str, sql: str, timeout: float = 5.0) -> list[tuple]:
    """evaluator._run과 같은 방식. timeout(초) 넘는 쿼리는 예외 (교차 join 예측이 멈추지 않게)."""
    con = sqlite3.connect(f"file:{DB / db_id / (db_id + '.sqlite')}?mode=ro", uri=True, timeout=timeout)
    try:
        deadline = time.monotonic() + timeout
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        return [tuple(round(v, 6) if isinstance(v, float) else v for v in r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def tags(sql: str) -> list[str]:
    s = " " + re.sub(r"\s+", " ", sql.upper()) + " "
    t = [name for name, pat in TAGS if re.search(pat, s)]
    if s.count("SELECT") > 1: t.append("nested")
    if re.search(r"'[^']+'", sql): t.append("value")
    return t


def hardness(sql: str) -> str:
    s = sql.upper()
    k = sum(w in s for w in (" JOIN ", "GROUP BY", "ORDER BY", "HAVING", "INTERSECT", "UNION", "EXCEPT")) + s.count("SELECT") - 1
    return ["easy", "medium", "hard", "extra"][min(k, 3)]


def classify(db_id: str, gold: str, pred: str) -> str:
    """v0 결과 유형: ok / exec_error / empty / col_order / extra_cols / missing_cols / row_mismatch"""
    if not pred:
        return "exec_error"
    g = run(db_id, gold)
    try:
        p = run(db_id, pred)
    except Exception:
        return "exec_error"
    ordered = re.search(r"\border\s+by\b", gold, re.I) is not None
    if (p == g) if ordered else (Counter(p) == Counter(g)):
        return "ok"
    if not p:
        return "empty"
    gw, pw = len(g[0]), len(p[0])
    if pw < gw:
        return "missing_cols"
    # ponytail: 열 단위 값 multiset 매칭 (행 정렬은 안 봄). 열 순열 전수 비교는 SELECT * 에서 10! 번 돌아 멈춘다
    used: set[int] = set()
    for j in range(gw):
        gc = Counter(r[j] for r in g)
        k = next((i for i in range(pw) if i not in used and Counter(r[i] for r in p) == gc), None)
        if k is None:
            return "row_mismatch"
        used.add(k)
    return "col_order" if pw == gw else "extra_cols"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True); ap.add_argument("--dev", required=True); ap.add_argument("--out", default="catalog.jsonl")
    a = ap.parse_args()
    local = {p.name for p in DB.iterdir() if p.is_dir()}
    cands = []
    for side, path in (("train", a.train), ("dev", a.dev)):
        for i, r in enumerate(json.loads(Path(path).read_text())):
            if r["db_id"] not in local:
                continue
            try:
                if not run(r["db_id"], r["query"]):
                    continue
            except Exception:
                continue
            cands.append({"id": f"{side}_{i:04d}", "side": side, "db_id": r["db_id"], "question": r["question"], "gold_sql": r["query"],
                          "hardness": hardness(r["query"]), "tags": tags(r["query"]),
                          "twin": r["db_id"] + "|" + " ".join(r["query_toks_no_value"]).lower()})
    print(f"후보 {len(cands)} {dict(Counter(c['side'] for c in cands))}", flush=True)
    out = Path(a.out); prob = out.with_suffix(".problems.json"); pred = out.with_suffix(".pred.jsonl")
    prob.write_text(json.dumps([{k: c[k] for k in ("id", "db_id", "question", "gold_sql")} for c in cands]))
    if not pred.exists():
        subprocess.run([sys.executable, "-m", "sql_agent.run", "--problems", str(prob), "--out", str(pred)], check=True)
    preds = {o["id"]: o["sql"] for o in map(json.loads, pred.read_text().splitlines()) if o}
    empty = sum(not preds.get(c["id"]) for c in cands)   # 빈 SQL = 호출 실패. 실행 오류로 세면 카탈로그가 통째로 틀어진다
    if empty > len(cands) // 50:
        sys.exit(f"빈 예측 {empty}/{len(cands)}: LLM 호출 실패로 보인다. .env(LLM_MODEL={os.environ.get('LLM_MODEL')}) 확인 후 {pred} 를 지우고 다시 실행")
    twins = Counter(c["twin"] for c in cands)
    with open(out, "w") as f:
        for c in cands:
            c["v0"] = classify(c["db_id"], c["gold_sql"], preds.get(c["id"], "")); c["n_twins"] = twins[c["twin"]]
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    for side in ("train", "dev"):
        cs = [c for c in cands if c["side"] == side]
        print(f"[{side}] n={len(cs)} v0 ok={sum(c['v0']=='ok' for c in cs)} v0: {dict(Counter(c['v0'] for c in cs).most_common())}")
        print(f"  hardness: {dict(Counter(c['hardness'] for c in cs))}  쌍둥이 그룹 {len({c['twin'] for c in cs})}")


def selfcheck() -> None:
    db, g = "concert_singer", "SELECT name, age FROM singer"
    for pred, want in [(g, "ok"), ("SELECT age, name FROM singer", "col_order"), ("SELECT name, age, country FROM singer", "extra_cols"),
                       ("SELECT name FROM singer", "missing_cols"), (g + " WHERE 1 = 0", "empty"), ("SELECT nope FROM singer", "exec_error"),
                       ("", "exec_error"), (g + " LIMIT 1", "row_mismatch")]:
        assert classify(db, g, pred) == want, (pred, classify(db, g, pred))
    assert tags("SELECT count(*) FROM a JOIN b ON a.x = b.x WHERE y IN (SELECT y FROM c)") == ["join", "agg", "nested"]
    print("catalog ok")


if __name__ == "__main__":
    selfcheck() if sys.argv[1:] == ["selfcheck"] else main()
