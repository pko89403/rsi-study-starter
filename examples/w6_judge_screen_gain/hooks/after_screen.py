"""screen(20문제) 결과를 보고 full(100문제)로 갈지. 기본: screen_drop 이상 떨어지면 중단.
6주차 추가: screen_gain (0 = v0와 같음, 기본 1). 0보다 크면 screen에서 그만큼 올라야 full로 간다 (Goodhart 완화: 중간 단계에서 조기 중단)."""
from __future__ import annotations
from loop.context import RoundContext

KNOBS = {"screen_gain": ("int", 0, 5, 1)}   # 1: 20문제에서 한 문제도 안 올랐으면 full 100을 쓰지 않는다 (offline: 손실 patch 19개 중 11개를 23호출에서 거름, 승자 유지)
KNOB_DOCS = {"screen_gain": "minimum improvement on the 20-problem screen before spending the full 100-problem evaluation (0 = only reject on a drop)"}


def after_screen(ctx: RoundContext, screen_before: int, screen_after: int) -> bool:
    gain = (ctx.improver or {}).get("screen_gain", KNOBS["screen_gain"][-1])
    return screen_after >= screen_before - ctx.config["screen_drop"] and screen_after >= screen_before + gain
