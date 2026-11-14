"""patch를 guard·적용 전에 검토·수정·거부할지. 기본: 그대로 통과. None을 돌려주면 그 patch는 버린다 (rejected_by=review).
6주차(Self-evaluation) method는 여기나 after_screen.py에 넣는다. 최종 판정은 고정 kernel이라 여기서는 평가 전에 거르기만 한다.
참고: examples/w6_judge_screen_gain/ — LLM judge는 모든 patch에 8~9점을 줘 거르지 못했다. 결과 열이 질문과 맞는지처럼 실행으로 확인되는 검사가 낫다.
4주차 4b: prompt 블록 안에서 SKILLS와 나머지를 round마다 번갈아 받는다 (build_prompt.aspect).
- skills round: patch에서 SKILLS 목록만 받는다. SYSTEM_PROMPT·FEWSHOT_IDS·build_messages는 parent 그대로.
- prompt round: patch에서 SKILLS 밖만 받는다. parent의 SKILLS는 그대로 다시 넣는다.
어느 쪽이든 SKILLS가 있으면 블록 끝에 적용 wrapper(APPLY)를 하나 붙인다."""
from __future__ import annotations
import ast
from loop.context import RoundContext
from loop.patch import Patch
from hooks.build_prompt import aspect, split_block

# prompt 블록의 build_messages 결과의 system message에 발동한 rule을 붙인다
APPLY = '''
_base_build_messages = build_messages


def build_messages(question: str, schema: str, fewshot: list[dict]) -> list[dict]:
    msgs = _base_build_messages(question, schema, fewshot)
    rules = [k["rule"] for k in SKILLS if any(w in question.lower() for w in k["when"])]
    if rules:
        msgs[0] = {**msgs[0], "content": msgs[0]["content"] + "\\n" + "\\n".join(rules)}
    return msgs'''


def _last_skills(src: str) -> object:
    """src의 마지막 SKILLS 리터럴. 없으면 None. 읽을 수 없으면 ValueError·SyntaxError·TypeError."""
    found = None
    for n in ast.parse(src).body:
        target = n.targets[0] if isinstance(n, ast.Assign) else n.target if isinstance(n, ast.AnnAssign) else None
        if isinstance(target, ast.Name) and target.id == "SKILLS" and n.value is not None:
            found = ast.literal_eval(n.value)
    return found


def _valid(skills: object) -> bool:
    return isinstance(skills, list) and all(
        isinstance(k, dict) and isinstance(k.get("rule"), str) and k["rule"].strip()
        and isinstance(k.get("when"), list) and k["when"] and all(isinstance(w, str) and w.strip() for w in k["when"])
        for k in skills)


def _join(core: str, skills_line: str | None) -> str:
    return core if not skills_line else f"{core}\n\n{skills_line}\n{APPLY}"


def review_patch(ctx: RoundContext, patch: Patch) -> Patch | None:
    if patch.block != "prompt":
        return patch
    try:
        core_old, skills_old = split_block(ctx.block)
        if aspect(ctx) == "skills":
            skills = _last_skills(patch.content)
            if not _valid(skills):
                return None   # SKILLS가 없거나 형식이 틀린 patch는 평가하지 않는다
            # when은 question.lower()에서 찾으므로 소문자로 맞춘다
            skills = [{"when": [w.strip().lower() for w in k["when"]], "rule": k["rule"].strip()} for k in skills]
            body = _join(core_old, f"SKILLS = {skills!r}" if skills else None)
        else:
            core_new, _ = split_block(patch.content)
            body = _join(core_new, skills_old)
    except (SyntaxError, ValueError, TypeError):
        return None
    if body.strip() == ctx.block.strip():
        return None   # 이번 round가 고칠 부분이 parent와 같다
    return Patch(patch.block, body, patch.reason)


if __name__ == "__main__":   # python -m hooks.review_patch : 가짜 ctx로 확인 (LLM 없음)
    from types import SimpleNamespace
    base = '''SYSTEM_PROMPT = "S"
FEWSHOT_IDS: list[str] = []


def build_messages(question: str, schema: str, fewshot: list[dict]) -> list[dict]:
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]'''
    ctx = lambda block, rnd: SimpleNamespace(block=block, round=rnd, block_name="prompt")
    run = lambda src: (exec(src, g := {}), g)[1]
    sk = 'SKILLS = [{"when": ["How many"], "rule": "Use COUNT(*)."}]'
    # skills round (홀수): SKILLS만 받는다
    out = review_patch(ctx(base, 1), Patch("prompt", 'SYSTEM_PROMPT = "changed"\n' + sk, "r"))
    g = run(out.content)
    assert g["SYSTEM_PROMPT"] == "S"                                                        # SKILLS 밖 변경은 버림
    assert g["build_messages"]("how many cars?", "", [])[0]["content"] == "S\nUse COUNT(*)."   # 발동
    assert g["build_messages"]("list cars", "", [])[0]["content"] == "S"                   # 미발동은 그대로
    assert review_patch(ctx(out.content, 3), Patch("prompt", out.content, "r")) is None       # 같은 SKILLS → 거부
    assert review_patch(ctx(base, 1), Patch("prompt", 'SYSTEM_PROMPT = "x"', "r")) is None     # SKILLS 없음 → 거부
    assert review_patch(ctx(base, 1), Patch("prompt", 'SKILLS = [{"when": [], "rule": "x"}]', "r")) is None   # 형식 틀림
    out3 = review_patch(ctx(out.content, 3), Patch("prompt", 'SKILLS = [{"when": ["average"], "rule": "Use AVG."}]', "r"))
    assert out3.content.count("_base_build_messages = build_messages") == 1 and "COUNT" not in out3.content   # 교체, wrapper 하나
    # prompt round (짝수): SKILLS 밖만 받고 parent SKILLS·wrapper를 다시 넣는다
    p = review_patch(ctx(out.content, 2), Patch("prompt", 'SYSTEM_PROMPT = "T"\n' + base.split("\n", 1)[1] + '\nSKILLS = []', "r"))
    g = run(p.content)
    assert g["SYSTEM_PROMPT"] == "T" and g["SKILLS"] == [{"when": ["how many"], "rule": "Use COUNT(*)."}]   # patch의 SKILLS는 무시
    assert g["build_messages"]("how many?", "", [])[0]["content"] == "T\nUse COUNT(*)." and p.content.count("_base_build_messages =") == 1
    # loop의 normalize가 새 build_messages를 wrapper 자리에 끼운 경우: 마지막 정의가 살아남고 wrapper는 새로 붙는다
    merged = out.content + '\n\n\ndef build_messages(question: str, schema: str, fewshot: list[dict]) -> list[dict]:\n    return [{"role": "system", "content": "N"}]'
    g = run(review_patch(ctx(out.content, 2), Patch("prompt", merged, "r")).content)
    assert g["build_messages"]("how many?", "", [])[0]["content"] == "N\nUse COUNT(*)."
    assert review_patch(ctx(out.content, 2), Patch("prompt", out.content, "r")) is None       # prompt 부분이 같음 → 거부
    assert review_patch(ctx(base, 2), Patch("prompt", "SYSTEM_PROMPT = (", "r")) is None        # 문법 오류 → 거부
    assert review_patch(ctx(base, 2), Patch("code", "x = 1", "r")).content == "x = 1"          # 다른 블록은 통과
    print("review_patch ok")
