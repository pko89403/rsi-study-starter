"""이번 round가 끝난 뒤 loop를 일찍 멈출지. 기본: 안 멈춤 (--rounds 만큼 돈다)."""
from loop.context import RoundContext


def should_stop(ctx: RoundContext) -> bool:
    return False
