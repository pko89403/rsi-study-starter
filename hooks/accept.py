"""patch를 새 최고로 삼을지. 기본: noise 여유(문제 수) 이상 올랐을 때."""
from __future__ import annotations
from loop.context import RoundContext
from loop.patch import Patch


def accept(ctx: RoundContext, before_correct: int, after_correct: int, patch: Patch) -> bool:
    return after_correct - before_correct >= ctx.config["noise_margin"]
