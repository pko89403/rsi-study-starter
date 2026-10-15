"""평가 뒤 무엇을 기록·축적할지. 기본: 아무것도 안 함."""
from loop.context import RoundContext


def after_eval(ctx: RoundContext) -> None:
    return None
