"""이번 round가 끝난 뒤 loop를 일찍 멈출지.
8주차: fail-closed kill switch. 멈추는 조건 (하나라도):
  - ledger 해시 체인이 깨짐 (증거 변조)
  - 이번 week의 llm_calls 합 >= CALL_BUDGET (환경 변수, 기본 2500)
  - 이번 week의 규칙 위반 시도 수 >= GUARD_CEILING (환경 변수, 기본 10)
  - stall_rounds 번 연속 accept 실패 (KNOB, 0=끔)
CALL_BUDGET·GUARD_CEILING 은 일부러 KNOB이 아니다: Improver가 자기 상한을 올릴 수 없어야 한다 (운영자가 .env로 정한다)."""
from __future__ import annotations
import json, os
from pathlib import Path
from loop.context import RoundContext
from methods import safety_ledger as L

KNOBS = {
    "stall_rounds": ("int", 0, 20, 0),   # 연속 비채택 round 수. 0이면 끔
}
KNOB_DOCS = {
    "stall_rounds": "stop the loop after this many consecutive rounds without an accepted patch (0 disables)",
}


def should_stop(ctx: RoundContext) -> bool:
    stall = (ctx.improver or {}).get("stall_rounds", KNOBS["stall_rounds"][-1])
    budget, ceiling = int(os.environ.get("CALL_BUDGET", 2500)), int(os.environ.get("GUARD_CEILING", 10))
    log = Path(ctx.config["log"])
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
    rows = [r for r in rows if r.get("week") == ctx.week]
    calls = sum(r.get("llm_calls") or 0 for r in rows)
    violations = sum(bool(r.get("violation")) for r in L.read() if r.get("kind") == "attempt" and r.get("week") == ctx.week)
    tail = 0
    for r in reversed(rows):
        if r.get("accepted"):
            break
        tail += 1
    why = L.verify() or (f"call budget: {calls} >= {budget}" if calls >= budget else None) \
        or (f"guard ceiling: {violations} violation attempts >= {ceiling}" if violations >= ceiling else None) \
        or (f"stall: {tail} consecutive rounds without accept >= {stall}" if stall and tail >= stall else None)
    if why:
        L.append({"kind": "stop", "round": ctx.round, "week": ctx.week, "reason": why})
        print(f"[round {ctx.round}] safety kernel: {why}")
    return bool(why)
