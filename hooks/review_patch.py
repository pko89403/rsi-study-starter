"""patch를 guard·적용 전에 검토·수정·거부할지. 기본: 그대로 통과. None을 돌려주면 그 patch는 버린다 (rejected_by=review)."""
from __future__ import annotations
from loop.context import RoundContext
from loop.patch import Patch


def review_patch(ctx: RoundContext, patch: Patch) -> Patch | None:
    return patch
