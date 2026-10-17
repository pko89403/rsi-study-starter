"""git을 archive로 쓴다. 평가한 patch마다 commit, 최고 점수 commit(champion)을 추적한다.
round commit은 branch를 움직이지 않고 refs/rsi/<sha>에 둔다. branch(HEAD)는 champion이 될 때만 그 commit으로 옮긴다.
그래서 HEAD와 작업 트리는 늘 champion(또는 그 뒤 참가자 commit)이고, 거부된 patch가 git status·pull·commit에 섞이지 않는다."""
from __future__ import annotations
import json, os
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
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1)); os.replace(tmp, self.state_file)   # 중단돼도 반쯤 쓴 파일이 남지 않게

    def _tree(self, paths: list[str]):
        """HEAD tree에 paths만 작업 트리 내용으로 바꾼 tree (참가자가 stage한 다른 파일은 섞이지 않는다)."""
        idx = git.IndexFile.from_tree(self.repo, self.repo.head.commit)
        idx.add(paths)
        return idx.write_tree()

    def commit(self, message: str, paths: list[str]) -> str:
        """paths의 지금 내용으로 commit을 만든다. branch는 그대로, commit은 refs/rsi/<sha>로 보관한다."""
        if not self.repo.head.is_valid():   # 빈 repo: 첫 commit은 branch에
            self.repo.index.add(paths)
            return self.repo.index.commit(message).hexsha[:7]
        head = self.repo.head.commit
        tree = self._tree(paths)
        if tree.hexsha == head.tree.hexsha:
            return head.hexsha[:7]
        c = git.Commit.create_from_tree(self.repo, tree, message, parent_commits=[head], head=False)
        self.repo.git.update_ref(f"refs/rsi/{c.hexsha[:7]}", c.hexsha)
        return c.hexsha[:7]

    def promote(self, sha: str, paths: list[str]) -> None:
        """champion이 된 commit으로 branch를 옮긴다 (그 commit의 parent가 지금 HEAD면 fast-forward)."""
        head, c = self.repo.head.commit, self.repo.commit(sha)
        if head.hexsha == c.hexsha:
            return
        if self.repo.is_ancestor(head, c):
            self.repo.head.reference.set_commit(c)
        else:   # HEAD가 그 사이에 움직였으면(참가자 commit 등) champion 내용을 HEAD 위에 새로 commit
            self.repo.git.checkout(sha, "--", *paths)
            n = git.Commit.create_from_tree(self.repo, self._tree(paths), f"champion {sha}", parent_commits=[head], head=True)
            self.state["scores"][n.hexsha[:7]] = self.state["scores"].get(sha); self.state["best"] = n.hexsha[:7]
        self.repo.git.reset("-q", "HEAD", "--", *paths)   # index를 새 HEAD에 맞춘다

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

    def set_best(self, sha: str) -> bool:
        """champion은 내려가지 않는다: 현재 best보다 점수가 엄격히 높을 때만 바꾼다."""
        cur = self.score_of(self.best) if self.best else None
        new = self.score_of(sha)
        if self.best is not None and (new is None or cur is None or new <= cur):
            return False
        self.state["best"] = sha; self._save()
        return True

    def seen(self, h: str) -> bool:
        return h in self.state.get("seen", [])

    def add_seen(self, h: str) -> None:
        self.state.setdefault("seen", []).append(h); self._save()

    @property
    def best(self) -> str | None:
        return self.state["best"]

    def score_of(self, sha: str) -> float | None:
        return self.state["scores"].get(sha)

    def children_count(self, sha: str) -> int:
        return sum(1 for p in self.state["parents"].values() if p == sha)
