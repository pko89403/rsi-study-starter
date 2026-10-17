"""screen(20문제) 결과를 보고 full(100문제)로 갈지. 기본: screen_drop 이상 떨어지면 중단.
6주차(Self-evaluation) 후보: 20문제에서 오르지 않으면 100문제 평가를 생략 (examples/w6의 screen_gain, 실측 약 600 호출 절감)."""
from __future__ import annotations
from loop.context import RoundContext


def after_screen(ctx: RoundContext, screen_before: int, screen_after: int) -> bool:
    return screen_after >= screen_before - ctx.config["screen_drop"]
