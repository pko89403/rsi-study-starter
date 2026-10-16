"""후보 patch를 몇 개 어떻게 만들지. 기본: LLM 1회 호출, patch 1개.
8주차: patch마다 kernel의 guard(AST)를 미리 돌려 ALLOW/DENY를 ledger에 남긴다 (methods/safety_ledger.py).
patch 자체는 그대로 넘긴다 (실제 거부는 loop의 precheck가 한다). 재요청된 시도도 전부 한 줄씩 남는다."""
from __future__ import annotations
from loop.context import RoundContext
from loop import patch as P
from loop.patch import Patch, parse_patch
from methods import safety_ledger as L


def propose(ctx: RoundContext, messages: list[dict]) -> list[Patch]:
    p = parse_patch(ctx.llm(messages, role="improver", max_tokens=ctx.config.get("improver_max_tokens", 4096)))
    why = "no parsable patch"
    if p:
        why = P.guard(P.normalize(p, ctx.blocks or {ctx.block_name: ctx.block}), ctx.config["editable_blocks"])
    L.append({"kind": "attempt", "round": ctx.round, "week": ctx.week, "target": ctx.block_name, "block": p.block if p else None,
              "decision": "DENY" if why else "ALLOW", "reason": why, "violation": L.is_violation(why)})
    return [p] if p else []
