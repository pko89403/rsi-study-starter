"""git을 archive로 쓴다. round마다 commit, 최고 점수 commit을 추적한다."""
from __future__ import annotations
import json
from pathlib import Path
import git


class Archive:
    def __init__(self, root: str | Path, state_file: str = "logs/archive.json") -> None:
        self.root = Path(root)
        self.repo = git.Repo.init(self.root) if not (self.root / ".git").exists() else git.Repo(self.root)
        self.state_file = self.root / state_file
        self.state: dict = json.loads(self.state_file.read_text()) if self.state_file.exists() else {"scores": {}, "parents": {}, "best": None}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=1))

    def commit(self, message: str, paths: list[str]) -> str:
        self.repo.index.add(paths)
        if not self.repo.is_dirty(index=True, working_tree=False) and self.repo.head.is_valid():
            return self.repo.head.commit.hexsha[:7]
        c = self.repo.index.commit(message)
        return c.hexsha[:7]

    def head(self) -> str:
        return self.repo.head.commit.hexsha[:7]

    def checkout(self, sha: str, paths: list[str]) -> None:
        self.repo.git.checkout(sha, "--", *paths)

    def record(self, sha: str, score: float | None, parent: str | None) -> None:
        if score is not None:
            self.state["scores"][sha] = score
        if parent:
            self.state["parents"][sha] = parent
        self._save()

    def set_best(self, sha: str) -> None:
        self.state["best"] = sha; self._save()

    @property
    def best(self) -> str | None:
        return self.state["best"]

    def score_of(self, sha: str) -> float | None:
        return self.state["scores"].get(sha)

    def children_count(self, sha: str) -> int:
        return sum(1 for p in self.state["parents"].values() if p == sha)
