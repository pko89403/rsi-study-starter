"""round 시작 직후(before 점수가 나온 뒤, 진단 전)에 할 일. 기본: 아무것도 안 함."""
from loop.context import RoundContext


def on_round_start(ctx: RoundContext) -> None:
    return None
