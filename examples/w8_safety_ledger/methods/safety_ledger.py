"""8주차 method: safety kernel ledger (arXiv 2606.26057 'The unfireable safety kernel').
Improver의 모든 patch 시도에 대한 ALLOW/DENY 판정과 round 증거를 append-only hash-chain 로그로 남긴다.
logs/kernel_ledger.jsonl 한 줄 = {...body, seq, prev, hash}. hash = sha256(body+seq+prev). LLM 호출 0.
ponytail: 서명 없는 해시 체인(변조 탐지만). 운영자 키 서명이 필요하면 .env 키로 HMAC으로 바꾼다."""
from __future__ import annotations
import hashlib, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "logs" / "kernel_ledger.jsonl"
VIOLATION_MARKS = ("forbidden", "import", "not open")   # loop/patch.guard 의 거부 사유 중 규칙 위반으로 세는 것. syntax error는 아님


def _hash(row: dict) -> str:
    return hashlib.sha256(json.dumps({k: v for k, v in row.items() if k != "hash"}, sort_keys=True).encode()).hexdigest()[:16]


def read(path: Path = LEDGER) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def append(body: dict, path: Path = LEDGER) -> dict:
    rows = read(path)
    row = {**body, "seq": len(rows) + 1, "prev": rows[-1]["hash"] if rows else "0" * 16}
    row["hash"] = _hash(row)
    path.parent.mkdir(exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def verify(path: Path = LEDGER) -> str | None:
    """체인이 깨진 곳. 정상이면 None."""
    prev = "0" * 16
    for i, row in enumerate(read(path), 1):
        if row.get("seq") != i or row.get("prev") != prev or row.get("hash") != _hash(row):
            return f"ledger chain broken at seq {i}"
        prev = row["hash"]
    return None


def is_violation(reason: str | None) -> bool:
    return bool(reason) and any(m in reason for m in VIOLATION_MARKS)


if __name__ == "__main__":  # python -m methods.safety_ledger : 체인·변조 탐지 확인
    import tempfile
    p = Path(tempfile.mkdtemp()) / "l.jsonl"
    for i in range(3):
        append({"kind": "attempt", "decision": "ALLOW" if i else "DENY", "reason": None if i else "forbidden name: os"}, p)
    assert verify(p) is None and len(read(p)) == 3
    assert is_violation("forbidden name: os") and is_violation("block 'code' not open") and not is_violation("syntax error: x")
    lines = p.read_text().splitlines()
    lines[1] = lines[1].replace('"ALLOW"', '"DENY"')
    p.write_text("\n".join(lines) + "\n")
    assert verify(p) == "ledger chain broken at seq 2", verify(p)
    print("safety_ledger ok")
