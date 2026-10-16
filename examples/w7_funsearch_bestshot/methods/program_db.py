"""FunSearch식 programs database 읽기 (7주차). archive(git commit + logs/archive.json 점수)를 program DB로 쓴다.

best_shots(): 평가된 commit들에서 대상 블록의 이전 버전을 뽑아 점수 오름차순으로 돌려준다 (FunSearch "best-shot prompt").
- cluster = 같은 점수 (FunSearch의 score signature). cluster는 softmax(score / T)로 뽑고, cluster 안에서는 가장 짧은 블록 (FunSearch: 짧은 프로그램 선호).
- parent(현재 블록)는 항상 들어간다. 나머지 k-1개가 표본.
LLM 호출 없음. git show만 읽는다.
"""
from __future__ import annotations
import json, math, random
from pathlib import Path
from loop.patch import find_block


def _block_at(repo, sha: str, file: str, name: str) -> str | None:
    try:
        src = repo.git.show(f"{sha}:{file}")
    except Exception:
        return None
    se = find_block(src, name)
    return src[se[0]:se[1]].rstrip("\n") if se else None


def best_shots(root: Path, parent: str, parent_block: str, block_name: str, files: list[str],
               k: int, temp: float, seed: int) -> list[tuple[str, int, str]]:
    """반환: [(sha, score, block)] 점수 오름차순, parent 포함. archive가 없으면 parent만."""
    state_file = root / "logs" / "archive.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"scores": {}}
    scores: dict = state.get("scores", {})
    p_score = scores.get(parent, -1)
    out = [(parent, p_score, parent_block)]
    if k <= 1 or len(scores) < 2:
        return out
    import git
    repo = git.Repo(root)
    file = next((f for f in files if find_block((root / f).read_text(), block_name)), None)
    clusters: dict[int, list[tuple[str, str]]] = {}
    seen = {parent_block.strip()}
    for sha, sc in scores.items():
        if sha == parent:
            continue
        b = _block_at(repo, sha, file, block_name)
        if b is None or b.strip() in seen:   # 같은 내용(다른 블록만 고친 commit 등)은 한 번만
            continue
        seen.add(b.strip()); clusters.setdefault(int(sc), []).append((sha, b))
    rng = random.Random(seed)
    keys = list(clusters)
    for _ in range(min(k - 1, len(keys))):
        top = max(keys)
        w = [math.exp((s - top) / temp) for s in keys]
        s = rng.choices(keys, weights=w)[0]
        keys.remove(s)
        sha, b = min(clusters[s], key=lambda x: len(x[1]))   # ponytail: 길이 softmax 대신 최단 하나. 다양성이 필요하면 FunSearch식 softmax(-len)
        out.append((sha, s, b))
    out.sort(key=lambda x: x[1])
    return out


def render(shots: list[tuple[str, int, str]], parent: str, block_name: str, n: int) -> str:
    rows = []
    for i, (sha, sc, b) in enumerate(shots):
        tag = f" (current, commit {sha})" if sha == parent else f" (commit {sha})"
        rows.append(f"<<<VERSION v{i} of block '{block_name}' | validation score {sc}/{n}{tag}>>>\n{b}\n<<<END_VERSION>>>")
    head = (f"Previous versions of block '{block_name}' with their validation scores, lowest to highest. "
            f"Learn from the difference between low and high scores. Write version v{len(shots)}: it must score higher than all of them. "
            f"Make a targeted edit of the current version, not a rewrite: a change that alters many answers is accepted only with a much larger gain.")
    return head + "\n\n" + "\n\n".join(rows)


if __name__ == "__main__":   # python -m methods.program_db : 표본 뽑기 확인 (LLM·git 없음)
    import tempfile, subprocess
    d = Path(tempfile.mkdtemp()); (d / "logs").mkdir(); (d / "sql_agent").mkdir()
    g = lambda *a: subprocess.run(["git", *a], cwd=d, capture_output=True, text=True, check=True)
    g("init", "-q")
    shas = []
    for i, body in enumerate(["X = 0", "X = 1", "X = 1", "X = 2", "X = 3"]):
        (d / "sql_agent/run.py").write_text(f"# === EVOLVE-BLOCK-START: prompt ===\n{body}\n# === EVOLVE-BLOCK-END ===\n")
        g("add", "-A"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", str(i), "--allow-empty")
        shas.append(subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip())
    (d / "logs/archive.json").write_text(json.dumps({"scores": dict(zip(shas, [72, 69, 69, 70, 76])), "parents": {}, "best": shas[-1]}))
    s = best_shots(d, shas[-1], "X = 3", "prompt", ["sql_agent/run.py"], k=3, temp=2.0, seed=1)
    assert [x[1] for x in s] == sorted(x[1] for x in s) and s[-1][0] == shas[-1] and len(s) == 3, s
    assert len({x[2] for x in s}) == 3                                      # 내용 중복 없음
    assert best_shots(d, shas[-1], "X = 3", "prompt", ["sql_agent/run.py"], k=1, temp=2.0, seed=1) == [(shas[-1], 76, "X = 3")]
    assert best_shots(d, shas[-1], "X = 3", "prompt", ["sql_agent/run.py"], k=3, temp=2.0, seed=1) == s   # 같은 seed → 같은 표본
    hi = sum(best_shots(d, shas[-1], "X = 3", "prompt", ["sql_agent/run.py"], k=2, temp=0.5, seed=i)[0][1] == 72 for i in range(50))
    assert hi >= 40, hi                                                     # 낮은 T → 높은 점수 cluster 위주
    txt = render(s, shas[-1], "prompt", 100)
    assert "v2" in txt and "(current" in txt and "Write version v3" in txt
    print("program_db ok")
