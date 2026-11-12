"""patch를 guard·적용 전에 검토·수정·거부할지. 기본: 그대로 통과. None을 돌려주면 그 patch는 버린다 (rejected_by=review).
6주차(Self-evaluation) method는 여기나 after_screen.py에 넣는다. 최종 판정은 고정 kernel이라 여기서는 평가 전에 거르기만 한다.
참고: examples/w6_judge_screen_gain/ — LLM judge는 모든 patch에 8~9점을 줘 거르지 못했다. 결과 열이 질문과 맞는지처럼 실행으로 확인되는 검사가 낫다.
4주차(SkillAxe, skill만 editable): prompt patch에서 SKILLS 목록만 받아 parent 블록에 끼운다. SYSTEM_PROMPT·build_messages·FEWSHOT_IDS는 parent 그대로."""
from __future__ import annotations
import ast
from loop.context import RoundContext
from loop.patch import Patch

SKILL_ONLY = True
# parent의 build_messages가 무엇이든 그 결과의 system message에 발동한 rule을 붙인다
APPLY = '''
_base_build_messages = build_messages


def build_messages(question: str, schema: str, fewshot: list[dict]) -> list[dict]:
    msgs = _base_build_messages(question, schema, fewshot)
    rules = [k["rule"] for k in SKILLS if any(w in question.lower() for w in k["when"])]
    if rules:
        msgs[0] = {**msgs[0], "content": msgs[0]["content"] + "\\n" + "\\n".join(rules)}
    return msgs'''


def _skills_node(src: str) -> ast.Assign | ast.AnnAssign | None:
    for n in ast.parse(src).body:
        target = n.targets[0] if isinstance(n, ast.Assign) else n.target if isinstance(n, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == "SKILLS":
            return n
    return None


def _valid(skills: object) -> bool:
    return isinstance(skills, list) and all(
        isinstance(k, dict) and isinstance(k.get("rule"), str) and k["rule"].strip()
        and isinstance(k.get("when"), list) and k["when"] and all(isinstance(w, str) and w.strip() for w in k["when"])
        for k in skills)


def review_patch(ctx: RoundContext, patch: Patch) -> Patch | None:
    if not SKILL_ONLY or patch.block != "prompt":
        return patch
    try:
        node = _skills_node(patch.content)
        skills = ast.literal_eval(node.value) if node is not None and node.value is not None else None
        old = _skills_node(ctx.block)
    except (SyntaxError, ValueError, TypeError):
        return None
    if not _valid(skills):
        return None   # SKILLS가 없거나 형식이 틀린 patch는 평가하지 않는다
    # when은 question.lower()에서 찾으므로 소문자로 맞춘다
    skills = [{"when": [w.strip().lower() for w in k["when"]], "rule": k["rule"].strip()} for k in skills]
    lines = ctx.block.splitlines()
    if old is not None:   # parent의 SKILLS 줄을 지우고 새 목록을 같은 자리에 둔다
        lines[old.lineno - 1: old.end_lineno] = [f"SKILLS = {skills!r}"]
        body = "\n".join(lines)
    else:
        body = ctx.block.rstrip("\n") + f"\n\nSKILLS = {skills!r}"
    if "in SKILLS" not in body:   # 적용 코드가 없으면 붙인다 (r125처럼 parent가 이미 가진 경우는 건너뜀)
        body += "\n" + APPLY
    if body.strip() == ctx.block.strip():
        return None
    return Patch(patch.block, body, patch.reason)


if __name__ == "__main__":   # python -m hooks.review_patch : 가짜 ctx로 확인 (LLM 없음)
    from types import SimpleNamespace
    base = '''SYSTEM_PROMPT = "S"
FEWSHOT_IDS: list[str] = []


def build_messages(question: str, schema: str, fewshot: list[dict]) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]'''
    ctx = SimpleNamespace(block=base)
    sk = 'SKILLS = [{"when": ["How many"], "rule": "Use COUNT(*)."}]'
    out = review_patch(ctx, Patch("prompt", 'SYSTEM_PROMPT = "changed"\n' + sk, "r"))
    g: dict = {}; exec(out.content, g)
    assert g["SYSTEM_PROMPT"] == "S"                                                        # SKILLS 밖 변경은 버림
    assert g["build_messages"]("how many cars?", "", [])[0]["content"] == "S\nUse COUNT(*)."   # 발동
    assert g["build_messages"]("list cars", "", [])[0]["content"] == "S"                   # 미발동은 그대로
    ctx2 = SimpleNamespace(block=out.content)                                              # 다음 round: SKILLS 교체, wrapper 하나만
    out2 = review_patch(ctx2, Patch("prompt", 'SKILLS = [{"when": ["average"], "rule": "Use AVG."}]', "r"))
    assert out2.content.count("_base_build_messages = build_messages") == 1 and "COUNT" not in out2.content
    assert review_patch(ctx2, Patch("prompt", out.content, "r")) is None                     # 같은 SKILLS → 거부
    assert review_patch(ctx, Patch("prompt", 'SYSTEM_PROMPT = "x"', "r")) is None             # SKILLS 없음 → 거부
    assert review_patch(ctx, Patch("prompt", 'SKILLS = [{"when": [], "rule": "x"}]', "r")) is None   # 형식 틀림 → 거부
    assert review_patch(ctx, Patch("code", "x = 1", "r")).content == "x = 1"                 # 다른 블록은 통과
    print("review_patch ok")
