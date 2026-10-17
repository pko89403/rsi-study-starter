"""round 시작 직후(before 점수가 나온 뒤, 진단 전)에 할 일.
기본: Improver agent가 이번 round에 겨냥할 오답 유형(target_error_class)을 자기 지난 round 결과로 고른다 (UCB1).
운영자는 선택지만 주고, 어느 값이 좋은지는 Improver가 자기 accept 기록으로 정한다. self_select=False면 IMPROVER 값을 그대로 쓴다."""
from __future__ import annotations
import json, math
from pathlib import Path
from loop.context import RoundContext

ARMS = ["any", "exec_error", "mismatch"]
KNOBS = {"self_select": ("bool", True)}
KNOB_DOCS = {"self_select": "if true, each round picks target_error_class itself from past round outcomes (UCB1 on accept rate); if false, uses the fixed target_error_class value"}
CHOICES = Path("logs/self_select.jsonl")   # round마다 고른 값. rounds.jsonl과 round 번호로 이어 보상(accept 여부)을 센다


def choose(history: list[tuple[str, float]], arms: list[str] = ARMS, c: float = 0.5) -> str:
    """history = [(arm, 보상)]. 안 써 본 값이 있으면 순서대로 먼저, 아니면 UCB1."""
    n = {a: 0 for a in arms}; s = {a: 0.0 for a in arms}
    for a, r in history:
        if a in n:
            n[a] += 1; s[a] += r
    for a in arms:
        if n[a] == 0:
            return a
    total = sum(n.values())
    return max(arms, key=lambda a: s[a] / n[a] + c * math.sqrt(math.log(total) / n[a]))


def data_version() -> str | None:
    """지금 점수를 잰 데이터의 버전 (loop가 logs/archive.json에 둔다). 데이터가 바뀌면 이전 선택 기록은 쓰지 않는다."""
    a = Path("logs/archive.json")
    return json.loads(a.read_text()).get("data_sha") if a.exists() else None


def history(log: Path, choices: Path = CHOICES) -> list[tuple[str, float]]:
    if not (log.exists() and choices.exists()):
        return []
    rounds = {r["round"]: r for r in map(json.loads, filter(str.strip, log.read_text().splitlines()))}
    out, ver = [], data_version()
    for c in map(json.loads, filter(str.strip, choices.read_text().splitlines())):
        r = rounds.get(c["round"]) if c.get("data") == ver else None
        if r:   # 끝난 round만. 보상 = accept 1, 아니면 0
            out.append((c["arm"], 1.0 if r.get("accepted") else 0.0))
    return out


def on_round_start(ctx: RoundContext) -> None:
    if not (ctx.improver or {}).get("self_select", KNOBS["self_select"][-1]):
        return None
    arm = choose(history(Path(ctx.config["log"])))
    ctx.improver = {**(ctx.improver or {}), "target_error_class": arm}
    CHOICES.parent.mkdir(exist_ok=True)
    with open(CHOICES, "a") as f:
        f.write(json.dumps({"round": ctx.round, "arm": arm, "data": data_version()}) + "\n")
    return None


if __name__ == "__main__":   # python -m hooks.on_round_start : 선택 규칙 확인 (LLM 없음)
    assert choose([]) == "any" and choose([("any", 0)]) == "exec_error"            # 안 써 본 값부터
    h = [("any", 0)] * 4 + [("exec_error", 1)] * 3 + [("exec_error", 0)] + [("mismatch", 0)] * 4
    assert choose(h) == "exec_error"                                              # accept율 높은 값을 고른다
    assert choose([("any", 0)] * 30 + [("exec_error", 0)] * 30 + [("mismatch", 0)]) == "mismatch"   # 덜 써 본 값도 다시 본다
    print("select ok")
