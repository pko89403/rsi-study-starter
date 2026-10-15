"""SQL agent. 사용법: python -m sql_agent.run --problems p.json --out o.jsonl
입력 문제에는 gold_sql이 없다. 출력은 한 줄에 {"id","sql"}."""
from __future__ import annotations
import argparse, json, os, re, sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sql_agent import llm
from sql_agent.schema import schema_text

DATA_DIR = Path(os.environ.get("STARTER_DATA_DIR", "data"))


def try_sql(db_id: str, sql: str, limit: int = 5) -> dict:
    """SQL을 읽기 전용으로 실행해 본다. 블록 안 code가 '생성 → 실행 → 수정'에 쓰는 도구.
    반환: {"ok": bool, "rows": [...최대 limit행], "error": str | None}. 정답은 모른다."""
    db = DATA_DIR / "database" / db_id / f"{db_id}.sqlite"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            con.execute("PRAGMA busy_timeout=5000")
            rows = con.execute(sql).fetchmany(limit)
        finally:
            con.close()
        return {"ok": True, "rows": [tuple(r) for r in rows], "error": None}
    except sqlite3.Error as e:
        return {"ok": False, "rows": [], "error": f"{type(e).__name__}: {e}"[:200]}

# === EVOLVE-BLOCK-START: prompt ===
SYSTEM_PROMPT = "You are a Text-to-SQL assistant for SQLite. Answer with one SQL query only, no explanation."
FEWSHOT_IDS: list[str] = []   # data/fewshot_pool.json 의 id. 비어 있으면 few-shot 없음.


def build_messages(question: str, schema: str, fewshot: list[dict]) -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for ex in fewshot:
        msgs.append({"role": "user", "content": f"Schema:\n{ex['schema']}\nQuestion: {ex['question']}"})
        msgs.append({"role": "assistant", "content": ex["sql"]})
    msgs.append({"role": "user", "content": f"Schema:\n{schema}\nQuestion: {question}"})
    return msgs
# === EVOLVE-BLOCK-END ===


def extract_sql(text: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S)
    sql = (m.group(1) if m else text).strip()
    return sql.split(";")[0].strip()


# === EVOLVE-BLOCK-START: code ===
def generate(question: str, schema: str, db_id: str, fewshot: list[dict], ask: "callable", run_sql: "callable") -> str:
    """질문 하나 → SQL 하나. ask(messages) -> str 는 LLM 호출, run_sql(sql) -> {"ok","rows","error"} 는 이 문제의 DB에서 실행.
    v0: LLM 한 번. 4주차부터 '생성 → run_sql로 확인 → 오류면 고쳐서 다시' 같은 단계를 여기 짤 수 있다."""
    return extract_sql(ask(build_messages(question, schema, fewshot)))
# === EVOLVE-BLOCK-END ===


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--problems", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    tables = str(DATA_DIR / "tables.json")
    pool = {p["id"]: p for p in json.loads((DATA_DIR / "fewshot_pool.json").read_text())}
    fewshot = []
    for fid in FEWSHOT_IDS:
        if fid in pool:
            ex = {"question": pool[fid]["question"], "sql": pool[fid]["gold_sql"], "schema": schema_text(tables, pool[fid]["db_id"])}
            fewshot.append(ex)
    problems = json.loads(Path(a.problems).read_text())

    def solve(p: dict) -> dict:
        try:
            sql = generate(p["question"], schema_text(tables, p["db_id"]), p["db_id"], fewshot,
                           lambda msgs: llm.chat(msgs, role="agent"), lambda q: try_sql(p["db_id"], q))
            return {"id": p["id"], "sql": sql}
        except Exception as e:  # 한 문제 실패가 전체를 멈추지 않게. 빈 sql은 틀린 것으로 채점됨.
            print(f"[sql_agent] {p['id']} failed: {type(e).__name__}: {e}", flush=True)
            return {"id": p["id"], "sql": ""}

    workers = int(os.environ.get("LLM_CONCURRENCY", "8"))  # 로컬 LM Studio면 1
    with ThreadPoolExecutor(max_workers=workers) as ex, open(a.out, "w") as f:
        for row in ex.map(solve, problems):
            f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
