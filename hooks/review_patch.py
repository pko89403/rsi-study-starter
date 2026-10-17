"""patch를 guard·적용 전에 검토·수정·거부할지. 기본: 그대로 통과. None을 돌려주면 그 patch는 버린다 (rejected_by=review).
6주차(Self-evaluation) method는 여기나 after_screen.py에 넣는다. 최종 판정은 고정 kernel이라 여기서는 평가 전에 거르기만 한다.
참고: examples/w6_judge_screen_gain/ — LLM judge는 모든 patch에 8~9점을 줘 거르지 못했다. 결과 열이 질문과 맞는지처럼 실행으로 확인되는 검사가 낫다."""
from __future__ import annotations
from loop.context import RoundContext
from loop.patch import Patch


def review_patch(ctx: RoundContext, patch: Patch) -> Patch | None:
    return patch
