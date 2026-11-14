"""어느 commit에서 이번 round를 시작할지. 3주차: DGM open-ended exploration (score_child_prop, DGM 부록 C.2).
점수가 있는 버전 전체(거부된 버전 포함)에서 sigmoid(λ(정답률 - 기준점)) × 1/(1 + 자식 수) 비율로 뽑는다."""
from __future__ import annotations
import json, math, random
from loop.archive import Archive

LAMBDA = 10.0       # 점수 차이를 얼마나 크게 반영할지 (DGM 값)
CENTER_TOP = 3      # 기준점 = 상위 3개 정답률 평균 (Hyperagents 부록 A.2). 0이면 DGM 원래 값 0.5
EPSILON = 0.0       # 이 확률로 그냥 champion을 고름
N_PROBLEMS = 100    # validation 문제 수 (archive 점수 = 맞힌 문제 수)
# 4주차 4b: prompt·SKILLS 교대는 "방금 accept된 자식 위에서 다른 부분을 고치는" 연쇄라 DGM 추첨이 끊는다.
# LINEAGE_ROOT가 있으면 DGM 대신, 그 commit과 hook_changed에 LINEAGE_TAG가 든 round에서 accept된 자손 중 최고점을 고른다. ""이면 DGM.
LINEAGE_ROOT = "c764ddb"   # 4주차 끝 champion (76)
LINEAGE_TAG = "4b"


def select_parent(archive: Archive) -> str:
    scores = archive.state["scores"]
    log = archive.state_file.with_name("rounds.jsonl")   # config.yaml log 기본값과 같은 곳
    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()] if log.exists() else []
    if LINEAGE_ROOT:
        line = [LINEAGE_ROOT]
        for e in entries:
            if e.get("accepted") and e.get("commit") and LINEAGE_TAG in (e.get("hook_changed") or "") and e.get("parent") in line:
                line.append(e["commit"])
        return max(line, key=lambda c: (scores.get(c, -1), line.index(c)))   # 같은 점수면 나중 것
    rng = random.Random(len(entries))   # 같은 기록 → 같은 선택 (재현용)
    if not scores or rng.random() < EPSILON:
        return archive.best or archive.head()
    # parent와 100문제 SQL이 모두 같은 자식(kernel_accept의 changed == 0)은 parent의 복제본이라 뺀다
    clones = {e["commit"] for e in entries if e.get("commit") and e.get("changed") == 0}
    # shortcut: promote가 만든 "champion <sha>" 복사 commit은 원본과 따로 후보가 된다, 참가자 commit 뒤 champion이 바뀌는 일이 잦으면 합칠 것
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
    root, LINEAGE_ROOT = LINEAGE_ROOT, ""   # 아래는 DGM 확인
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
    LINEAGE_ROOT = root   # 계보 확인: 4b round에서 accept된 root 자손 중 최고점, 다른 계보·다른 실험은 무시
    (d / "rounds.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"commit": "x1", "parent": root, "accepted": True, "hook_changed": "old"},      # 4b 아님
        {"commit": "k1", "parent": root, "accepted": True, "hook_changed": "4b: ..."},
        {"commit": "k2", "parent": "k1", "accepted": False, "hook_changed": "4b: ..."},  # 거부
        {"commit": "k3", "parent": "zz", "accepted": True, "hook_changed": "4b: ..."}])) # 다른 계보
    b = _A(d, {root: 76, "x1": 90, "k1": 77, "k2": 95, "k3": 99}, {})
    assert select_parent(b) == "k1", select_parent(b)
    (d / "rounds.jsonl").write_text("")
    assert select_parent(_A(d, {}, {})) == root                                          # 4b 기록이 없으면 root
    print("select_parent ok", dict(seen))
