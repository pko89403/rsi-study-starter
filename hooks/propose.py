"""후보 patch를 몇 개 어떻게 만들지. 기본: LLM 1회 호출, patch 1개."""
from __future__ import annotations
from loop.context import RoundContext
from loop.patch import Patch, parse_patch


def propose(ctx: RoundContext, messages: list[dict]) -> list[Patch]:
    p = parse_patch(ctx.llm(messages, role="improver", max_tokens=ctx.config.get("improver_max_tokens", 4096)))
    return [p] if p else []
