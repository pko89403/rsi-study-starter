"""patch를 남길지. 최종 판정은 고정 kernel(loop/kernel.py kernel_accept)이 하고, 이 hook과 AND로 묶인다.
그래서 이 hook은 조건을 더 엄격하게만 만들 수 있다. 기본: kernel 판정을 그대로 따른다."""
from __future__ import annotations
from loop.context import RoundContext
from loop.patch import Patch


def accept(ctx: RoundContext, before_correct: int, after_correct: int, patch: Patch) -> bool:
    return True
