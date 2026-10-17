"""SQL agent (LangGraph). 사용법: python -m sql_agent.run --problems p.json --out o.jsonl
입력 문제에는 gold_sql이 없다. 출력은 한 줄에 {"id","sql"}.
문제 하나 = graph 한 번 실행. 배선(select_schema → generate → execute → fix)은 고정이고, 노드 본문은 `code` 블록이 정한다."""
from __future__ import annotations
import argparse, json, os, re, sqlite3, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
import llm
from sql_agent.schema import schema_text

DATA_DIR = Path(os.environ.get("STARTER_DATA_DIR", "data"))
MAX_STEPS = 25   # graph가 END에 못 가고 맴돌 때 자르는 단계 수 (fix 5회 = 3 + 2*5 = 13 노드 + 여유)


class AgentState(TypedDict, total=False):
    question: str
    schema: str
    db_id: str
    fewshot: list[dict]
    sql: str          # 답. END에서 이 값을 낸다
    result: dict      # 마지막 run_sql 결과 {"ok","rows","error"}
    attempts: int     # 블록 안 노드가 올리는 값(참고용). 상한은 executions로 건다
    executions: int   # execute 노드가 올린다. 블록이 바꿀 수 없음


SQL_TIMEOUT = float(os.environ.get("SQL_TIMEOUT_SEC", "5"))   # try_sql 한 번의 상한(초). evaluator와 같은 방식


def try_sql(db_id: str, sql: str, limit: int = 5, timeout: float = SQL_TIMEOUT) -> dict:
    """SQL을 읽기 전용으로 실행해 본다. execute 노드가 쓰는 도구.
    반환: {"ok": bool, "rows": [...최대 limit행], "error": str | None}. 정답은 모른다.
    timeout초를 넘기면 progress handler가 중단시키고 ok=False, error="timeout"."""
    db = DATA_DIR / "database" / db_id / f"{db_id}.sqlite"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=timeout)
        try:
            con.execute(f"PRAGMA busy_timeout={int(timeout*1000)}")
            deadline = time.monotonic() + timeout
            con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
            rows = con.execute(sql).fetchmany(limit)
        finally:
            con.close()
        return {"ok": True, "rows": [tuple(r) for r in rows], "error": None}
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).lower():
            return {"ok": False, "rows": [], "error": f"timeout: query exceeded {timeout:g}s"}
        return {"ok": False, "rows": [], "error": f"{type(e).__name__}: {e}"[:200]}
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


def ask(messages: list[dict]) -> str:
    """SQL agent 역할의 LLM 호출. 블록 안 노드가 쓴다. EVOLVE-BLOCK 밖."""
    return llm.chat(messages, role="agent")


# === EVOLVE-BLOCK-START: code ===
# 노드 본문. 배선(select_schema → generate → execute → route → fix/END)은 아래 make_graph가 고정한다.
# 노드는 s(AgentState) -> 바뀐 key만 담은 dict. ask(messages) -> str 는 LLM 호출. s["result"]는 직전 execute의 {"ok","rows","error"}.
MAX_ATTEMPTS = 1   # route가 fix로 보낼 수 있는 generate+fix 총 횟수. 1이면 재시도 없음(v0)


def select_schema(s: AgentState) -> dict:
    """질문과 관련된 테이블만 남겨 s["schema"]를 줄인다. v0: 전체 스키마 그대로(변경 없음)."""
    return {}


def generate(s: AgentState) -> dict:
    return {"sql": extract_sql(ask(build_messages(s["question"], s["schema"], s["fewshot"]))), "attempts": s.get("attempts", 0) + 1}


def fix(s: AgentState) -> dict:
    """execute가 실패했을 때 오류를 보여 주고 SQL을 다시 받는다. v0에서는 route가 여기로 보내지 않는다."""
    msgs = build_messages(s["question"], s["schema"], s["fewshot"])
    msgs.append({"role": "assistant", "content": s["sql"]})
    msgs.append({"role": "user", "content": f"That SQL failed on SQLite: {s['result']['error']}\nReply with the corrected SQL only."})
    return {"sql": extract_sql(ask(msgs)), "attempts": s["attempts"] + 1}


def route(s: AgentState) -> str:
    """execute 뒤 어디로 갈지: "fix" 또는 END. v0: 항상 END."""
    return END
# === EVOLVE-BLOCK-END ===


MAX_FIX = 5   # route가 뭐라 하든 fix는 문제당 이 횟수를 넘지 않는다 (비용 상한). 블록 밖. 블록 안 attempts와 무관하게 execute 횟수로 센다.


def execute(s: AgentState) -> dict:
    return {"result": try_sql(s["db_id"], s["sql"]), "executions": s.get("executions", 0) + 1}


def _route(s: AgentState) -> str:
    r = ROUTE(s)
    fixes_so_far = s.get("executions", 1) - 1   # 첫 execute는 generate 뒤. 그 다음부터가 fix
    return "fix" if r == "fix" and fixes_so_far < MAX_FIX else END


def make_graph():
    """고정 배선. 노드 본문은 code 블록이 정한다. 블록이 이름을 빼먹으면 여기서 NameError."""
    global ROUTE
    ROUTE = route
    for name, fn in (("select_schema", select_schema), ("generate", generate), ("fix", fix), ("route", route)):
        if not callable(fn):
            raise TypeError(f"code block: {name} must be a function")
    int(MAX_ATTEMPTS)
    g = StateGraph(AgentState)
    g.add_node("select_schema", select_schema)
    g.add_node("generate", generate)
    g.add_node("execute", execute)
    g.add_node("fix", fix)
    g.add_edge(START, "select_schema")
    g.add_edge("select_schema", "generate")
    g.add_edge("generate", "execute")
    g.add_edge("fix", "execute")
    g.add_conditional_edges("execute", _route, {"fix": "fix", END: END})
    return g.compile()


GRAPH = make_graph()   # import 시점에 배선. 블록이 route/fix 등을 빼먹으면 여기서 NameError → loop guard의 compile 검사가 잡는다


def answer(graph, question: str, schema: str, db_id: str, fewshot: list[dict]) -> str:
    out = graph.invoke({"question": question, "schema": schema, "db_id": db_id, "fewshot": fewshot},
                       {"recursion_limit": MAX_STEPS})
    return out.get("sql", "")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--problems", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    tables = str(DATA_DIR / "tables.json")
    pool = {p["id"]: p for p in json.loads((DATA_DIR / "fewshot_pool.json").read_text())}
    fewshot = []
    for fid in FEWSHOT_IDS:
        if fid in pool:
            ex = {"question": pool[fid]["question"], "sql": pool[fid]["gold_sql"], "schema": schema_text(tables, pool[fid]["db_id"]), "db_id": pool[fid]["db_id"]}
            fewshot.append(ex)
    problems = json.loads(Path(a.problems).read_text())
    graph = GRAPH

    def solve(p: dict) -> dict:
        try:
            fs = [ex for ex in fewshot if ex["db_id"] != p["db_id"]]   # 같은 DB 예시 제외 (validation과 pool이 같은 DB를 써서, 같은 DB 예시는 test로 이어지지 않는다)
            return {"id": p["id"], "sql": answer(graph, p["question"], schema_text(tables, p["db_id"]), p["db_id"], fs)}
        except Exception as e:  # 한 문제 실패가 전체를 멈추지 않게. 빈 sql은 틀린 것으로 채점됨.
            print(f"[sql_agent] {p['id']} failed: {type(e).__name__}: {e}", flush=True)
            return {"id": p["id"], "sql": ""}

    workers = int(os.environ.get("LLM_CONCURRENCY", "8"))  # 로컬 LM Studio면 1
    with ThreadPoolExecutor(max_workers=workers) as ex, open(a.out, "w") as f:
        for row in ex.map(solve, problems):
            f.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    main()
