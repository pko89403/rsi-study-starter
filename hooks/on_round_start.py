"""round 시작 직후(before 점수가 나온 뒤, 진단 전)에 할 일.
기본: Improver agent가 이번 round에 고칠 블록과 겨냥할 오답 유형(target_error_class)을 자기 지난 round 결과로 고른다 (UCB1).
선택지 = config.yaml의 editable_blocks × ARMS. 4주차에 editable_blocks에 code를 더하면 선택지가 저절로 늘어난다 (운영자 작업 없음).
운영자는 선택지만 주고, 어느 값이 좋은지는 Improver가 자기 accept 기록으로 정한다. self_select=False면 IMPROVER 값을 그대로 쓴다.
3주차까지(prompt 블록만): prompt 지시문은 모든 질문에 영향을 줘서, 유형을 겨냥해도 바뀐 답이 많다 (실측 28~39개). 사실상 any가 남는다.
4주차부터(config.yaml editable_blocks: [prompt, code]): exec_error를 겨냥한 route·fix patch는 실행 오류 문제만 바꿔 좁다. 이때 이 선택이 효과를 낸다."""
from __future__ import annotations
import json, math
from pathlib import Path
from loop.context import RoundContext

ARMS = ["any", "exec_error", "mismatch"]   # 겨냥할 오답 유형. 선택지 하나 = "블록:유형" (예: "code:exec_error")
KNOBS = {"self_select": ("bool", True)}
KNOB_DOCS = {"self_select": "if true, each round picks which open block to patch and which wrong answers to target, from past round outcomes (UCB1 on accept rate); if false, blocks rotate and the fixed target_error_class value is used"}
CHOICES = Path("logs/self_select.jsonl")   # round마다 고른 값. rounds.jsonl과 round 번호로 이어 보상(accept 여부)을 센다


def choose(history: list[tuple[str, float]], arms: list[str], c: float = 0.5) -> str:
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
    ver, last = data_version(), {}
    for c in map(json.loads, filter(str.strip, choices.read_text().splitlines())):
        if c.get("data") == ver:
            last[c["round"]] = c["arm"]   # 중단 뒤 같은 round를 다시 돌리면 마지막 선택만 센다
    # 끝난 round만. 보상 = accept 1, 아니면 0
    return [(arm, 1.0 if rounds[n].get("accepted") else 0.0) for n, arm in last.items() if n in rounds]


def on_round_start(ctx: RoundContext) -> None:
    if not (ctx.improver or {}).get("self_select", KNOBS["self_select"][-1]):
        return None
    arms = [f"{b}:{t}" for b in ctx.config["editable_blocks"] for t in ARMS]
    arm = choose(history(Path(ctx.config["log"])), arms)
    block, tgt = arm.split(":")
    ctx.block_name = block   # loop가 이 블록의 현재 내용을 ctx.block에 다시 넣는다
    ctx.improver = {**(ctx.improver or {}), "target_error_class": tgt}
    CHOICES.parent.mkdir(exist_ok=True)
    with open(CHOICES, "a") as f:
        f.write(json.dumps({"round": ctx.round, "arm": arm, "data": data_version()}) + "\n")
    return None


if __name__ == "__main__":   # python -m hooks.on_round_start : 선택 규칙 확인 (LLM 없음)
    A = ["any", "exec_error", "mismatch"]
    assert choose([], A) == "any" and choose([("any", 0)], A) == "exec_error"            # 안 써 본 값부터
    h = [("any", 0)] * 4 + [("exec_error", 1)] * 3 + [("exec_error", 0)] + [("mismatch", 0)] * 4
    assert choose(h, A) == "exec_error"                                                # accept율 높은 값을 고른다
    assert choose([("any", 0)] * 30 + [("exec_error", 0)] * 30 + [("mismatch", 0)], A) == "mismatch"   # 덜 써 본 값도 다시 본다
    assert choose([("prompt:any", 0)], ["prompt:any", "code:any"]) == "code:any"        # 4주차에 열린 블록도 시도한다
    print("select ok")
