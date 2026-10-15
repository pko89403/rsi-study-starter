"""어느 commit에서 이번 round를 시작할지. 기본: 최고 점수 commit."""
from loop.archive import Archive


def select_parent(archive: Archive) -> str:
    return archive.best or archive.head()
