"""patch를 guard·적용 전에 검토·수정·거부할지. 6주차: LLM-as-a-Judge (Zheng et al. 2023) 로 FILTER만 한다.
최종 판정은 kernel. 여기서는 평가(screen 20 + full 100 = 120 호출) 전에 거를 뿐이다.

- single:   reference-guided single-answer grading. 참고 자료 = 블록 계약 + 진단 + 틀린 문제 표본 + 지난 round의 (reason, net).
            SCORE 1-10. judge_min_score 미만이면 거부.
- pairwise: parent 블록 vs patch 블록을 A/B로 비교. position bias 보정을 위해 순서를 바꿔 2번 묻고,
            두 번 다 parent가 낫다고 할 때만 거부 (일관되지 않으면 TIE = 통과).
- 판정은 logs/judge.jsonl 에 남긴다 (kernel 판정과 나중에 맞춰 본다).
비용: single 1회, pairwise 2회 (round당)."""
from __future__ import annotations
import json, re
from pathlib import Path
from loop.context import RoundContext
from loop.patch import Patch
from hooks.build_prompt import CONTRACTS

KNOBS = {
    "judge_mode": ("enum", ["off", "single", "pairwise"], "single"),
    "judge_min_score": ("int", 1, 10, 7),      # single: 이 점수 미만이면 거부. offline: 정상 patch 21개는 전부 8-9, 검증 질문을 하드코딩한 patch는 6
    "judge_n_wrong": ("int", 0, 10, 5),        # 판정에 참고로 보여 줄 틀린 문제 수
}
KNOB_DOCS = {
    "judge_mode": "how the review judge works: off (pass everything), single (grade the patch 1-10 with references), pairwise (parent vs patch, both orders)",
    "judge_min_score": "single mode: minimum judge score (1-10) a patch needs to go on to evaluation",
    "judge_n_wrong": "how many wrong validation examples the judge sees as reference material",
}
JUDGE_LOG = Path("logs/judge.jsonl")

SINGLE = """You are a strict reviewer of patches to a Text-to-SQL agent. The patch rewrites one editable block of the agent's source.
The agent model is small (gpt-4.1-nano). Judge whether this patch is likely to make MORE validation questions correct than the current block, using the references below.
Penalize: cosmetic rewording that is equivalent to the current block or to already-tried patches (their outcomes are listed), changes that do not address the diagnosis,
prompts that hard-code specific validation questions or answers (that is overfitting), and anything that violates the block contract.
Think briefly (2-4 sentences), then end with a line exactly like: SCORE: <1-10>"""

PAIR = """You are comparing two versions of one editable block of a Text-to-SQL agent (model: gpt-4.1-nano). Decide which version will make more validation questions correct.
Do not prefer a version because it is longer. Think briefly (2-3 sentences), then end with a line exactly like: VERDICT: A or VERDICT: B or VERDICT: TIE"""


def knob(ctx: RoundContext, name: str):
    return (ctx.improver or {}).get(name, KNOBS[name][-1])


def _refs(ctx: RoundContext) -> str:
    wrong = [p for p in ctx.before["per_problem"] if not p["correct"]][: knob(ctx, "judge_n_wrong")]
    wrong_txt = "\n".join(f"- {ctx.problem(p['id'])['question']}\n  predicted: {ctx.prediction(p['id'])}\n  error: {p['error']}" for p in wrong)
    tried = "\n".join(f"- net {r.get('net')} ({r.get('rejected_by') or 'accepted'}): {r.get('reason')}" for r in ctx.recent_log if r.get("reason"))
    return (f"Validation score now: {ctx.before['correct']}/{ctx.before['n']}\nDiagnosis: {ctx.diagnosis}\n\n{CONTRACTS.get(ctx.block_name, '')}\n\n"
            f"Wrong examples:\n{wrong_txt or '(none)'}\n\nAlready tried in recent rounds (net = fixed - broken on validation):\n{tried or '(none)'}")


def _ask(ctx: RoundContext, system: str, user: str) -> str:
    return ctx.llm([{"role": "system", "content": system}, {"role": "user", "content": user}], role="improver", max_tokens=600)


def judge(ctx: RoundContext, patch: Patch) -> dict:
    """{'mode','keep', ...details}. ctx.llm 호출 수: single 1, pairwise 2."""
    mode = knob(ctx, "judge_mode")
    if mode == "off":
        return {"mode": mode, "keep": True}
    refs = _refs(ctx)
    if mode == "single":
        out = _ask(ctx, SINGLE, f"{refs}\n\nCurrent block:\n{ctx.block}\n\nPatch (reason: {patch.reason}):\n{patch.content}")
        m = re.search(r"SCORE:\s*(\d+)", out)
        score = int(m.group(1)) if m else None
        return {"mode": mode, "score": score, "keep": score is None or score >= knob(ctx, "judge_min_score"), "text": out[-300:]}
    votes = []
    for a, b in ((ctx.block, patch.content), (patch.content, ctx.block)):   # 순서 바꿔 2번: position bias 보정
        out = _ask(ctx, PAIR, f"{refs}\n\nVersion A:\n{a}\n\nVersion B:\n{b}")
        m = re.search(r"VERDICT:\s*(A|B|TIE)", out)
        votes.append(m.group(1) if m else "TIE")
    prefers_parent = votes[0] == "A" and votes[1] == "B"
    prefers_patch = votes[0] == "B" and votes[1] == "A"
    return {"mode": mode, "votes": votes, "consistent": prefers_parent or prefers_patch, "keep": not prefers_parent}


def review_patch(ctx: RoundContext, patch: Patch) -> Patch | None:
    v = judge(ctx, patch)
    JUDGE_LOG.parent.mkdir(exist_ok=True)
    with open(JUDGE_LOG, "a") as f:
        f.write(json.dumps({"round": ctx.round, "reason": patch.reason, **v}, ensure_ascii=False) + "\n")
    return patch if v["keep"] else None
