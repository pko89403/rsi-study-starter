"""LLM 없이 loop 동작을 확인하기 위한 가짜 모델. 점수는 의미 없다.
- agent 역할: 질문에서 테이블 이름을 찾아 SELECT * / SELECT count(*) 를 낸다.
- improver 역할: 진단은 고정 문장, patch는 system prompt에 한 줄을 덧붙인 블록을 돌려준다."""
from __future__ import annotations
import json, re


def chat(messages: list[dict], *, role: str) -> str:
    text = "\n".join(m["content"] for m in messages if m["role"] != "system")
    if role == "improver":
        if "DIAGNOSE" in messages[0]["content"]:
            return "mock diagnosis: 테이블 선택이 틀림"
        m = re.search(r"<<<CURRENT_BLOCK prompt>>>\n(.*?)\n<<<END_BLOCK>>>", text, re.S)
        block = m.group(1) if m else ""
        # attempt 횟수를 세어 매번 다른 patch를 만든다 (같은 commit 방지)
        n = block.count("# mock patch")
        new_block = block.replace('SYSTEM_PROMPT = "', f'SYSTEM_PROMPT = "[mock rule {n+1}] ', 1) + f"\n# mock patch {n+1}"
        return f"BLOCK: prompt\nREASON: mock patch {n+1}\n<<<CONTENT\n{new_block}\nCONTENT>>>"
    # agent: 스키마 텍스트에서 테이블 목록을 찾고 질문 단어와 맞는 테이블을 고른다
    tables = re.findall(r"^table (\w+)", text, re.M)
    q = re.search(r"Question: (.*)", text)
    qwords = set(re.findall(r"\w+", (q.group(1) if q else "").lower()))
    best = max(tables, key=lambda t: len(qwords & set(t.lower().split("_"))), default=tables[0] if tables else "x")
    ql = q.group(1).lower() if q else ""
    sysmsg = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    if "how many" in ql or ("[mock rule" in sysmsg and ("number of" in ql or "count" in ql)):
        return f"SELECT count(*) FROM {best}"
    return f"SELECT * FROM {best}"
