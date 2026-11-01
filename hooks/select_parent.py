"""어느 commit에서 이번 round를 시작할지. 3주차: DGM open-ended exploration (score_child_prop, DGM 부록 C.2).
점수가 있는 버전 전체(거부된 버전 포함)에서 sigmoid(λ(정답률 - 기준점)) × 1/(1 + 자식 수) 비율로 뽑는다."""
from __future__ import annotations
import json, math, random
from loop.archive import Archive

LAMBDA = 10.0       # 점수 차이를 얼마나 크게 반영할지 (DGM 값)
CENTER_TOP = 3      # 기준점 = 상위 3개 정답률 평균 (Hyperagents 부록 A.2). 0이면 DGM 원래 값 0.5
EPSILON = 0.0       # 이 확률로 그냥 champion을 고름
N_PROBLEMS = 100    # validation 문제 수 (archive 점수 = 맞힌 문제 수)


def select_parent(archive: Archive) -> str:
    scores = archive.state["scores"]
    log = archive.state_file.with_name("rounds.jsonl")   # config.yaml log 기본값과 같은 곳
    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
    rng = random.Random(len(entries))   # 같은 기록 → 같은 선택 (재현용)
    if not scores or rng.random() < EPSILON:
        return archive.best or archive.head()
    # parent와 100문제 SQL이 모두 같은 자식(kernel_accept의 changed == 0)은 parent의 복제본이라 뺀다
    clones = {e["commit"] for e in entries if e.get("commit") and e.get("changed") == 0}
    cands = [s for s in scores if s not in clones]
    top = sorted((scores[s] for s in cands), reverse=True)[:CENTER_TOP]
    center = sum(top) / len(top) / N_PROBLEMS if CENTER_TOP else 0.5
    kids = dict.fromkeys(cands, 0)   # 점수가 있는 자식만 센다 (DGM: archive에 들어간 자식만)
    for c, p in archive.state["parents"].items():
        if p in kids and c in kids:
            kids[p] += 1
    w = [1 / (1 + math.exp(-LAMBDA * (scores[s] / N_PROBLEMS - center))) / (1 + kids[s]) for s in cands]
    return rng.choices(cands, weights=w)[0]


if __name__ == "__main__":   # python -m hooks.select_parent : 가짜 archive로 확인 (LLM·git 없음)
    import tempfile
    from collections import Counter
    from pathlib import Path

    class _A:
        def __init__(self, d: Path, scores: dict, parents: dict) -> None:
            self.state_file = d / "archive.json"; self.state = {"scores": scores, "parents": parents}; self.best = "R5"
        def head(self) -> str: return "HEAD"

    d = Path(tempfile.mkdtemp())
    assert select_parent(_A(d, {}, {})) == "R5"                                       # 빈 archive → champion
    sc = {"v0": 70, "r1": 68, "R5": 74, "c1": 74, "c2": 72}
    pa = {"r1": "v0", "R5": "v0", "c1": "R5", "c2": "R5"}
    (d / "rounds.jsonl").write_text("\n".join(json.dumps({"commit": c, "changed": n}) for c, n in [("r1", 5), ("R5", 3), ("c1", 0), ("c2", 4)]))
    a = _A(d, sc, pa)
    assert select_parent(a) == select_parent(a)                                       # 같은 기록 → 같은 선택
    seen = Counter()
    for i in range(2000):
        (d / "rounds.jsonl").write_text("{}\n" * i + json.dumps({"commit": "c1", "changed": 0}))   # 줄 수가 seed
        seen[select_parent(a)] += 1
    assert "c1" not in seen and set(seen) == {"v0", "r1", "R5", "c2"}, seen             # 복제본 제외, 나머지는 모두 뽑힘
    assert seen["v0"] < seen["R5"], seen                                              # 자식 2개인 v0 < 자식 1개인 R5
    print("select_parent ok", dict(seen))
