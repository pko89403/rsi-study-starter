"""screen(20문제) 결과를 보고 full(100문제)로 갈지. 기본: screen_drop 이상 떨어지면 중단."""
from __future__ import annotations
from loop.context import RoundContext


def after_screen(ctx: RoundContext, screen_before: int, screen_after: int) -> bool:
    return screen_after >= screen_before - ctx.config["screen_drop"]
