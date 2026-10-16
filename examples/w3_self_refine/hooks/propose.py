"""후보 patch를 어떻게 만들지. 3주차: Self-Refine (Madaan et al. 2023, arXiv 2303.17651).
초안 patch → 같은 LLM이 구체적·실행 가능한 피드백 → 피드백과 이력을 붙여 다시 쓴다 (refine_iters회, VERDICT: OK면 조기 종료).
반환 순서: 최신 수정본이 앞, 초안이 뒤. loop는 사전 검사를 통과한 첫 patch 하나만 평가하므로 수정본이 걸리면 초안이 쓰인다.
손잡이(refine_iters, critic_focus)는 hooks/build_prompt.py의 KNOBS에 선언돼 있다 (meta-eval의 NULL 판정이 build_prompt 출력으로 결정되기 때문).
비용: round당 LLM 호출 +2 × 반복 수 (피드백 1 + 수정 1)."""
from __future__ import annotations
from loop.context import RoundContext
from loop.patch import Patch, parse_patch
from hooks.build_prompt import KNOBS, knob

FEEDBACK = """You are reviewing a DRAFT patch that rewrites one block of a Text-to-SQL agent (a small model answers the SQL).
You get the task the patch writer saw (score, diagnosis, wrong examples, block contract, current block) and the draft.
Write feedback that is ACTIONABLE (say what to change) and SPECIFIC (point at the exact line/wording). 2-4 bullet points.
Check: {focus}
Also check the draft keeps every required top-level name and stays valid Python.
Also check the draft does not copy these review instructions into the block (e.g. "keep within 3 sentences", "rules from the wrong examples"): the SQL model never sees them, so they are noise.
If the draft is already good, write only: VERDICT: OK
Otherwise end with the line: VERDICT: REVISE"""


def propose(ctx: RoundContext, messages: list[dict]) -> list[Patch]:
    mx = ctx.config.get("improver_max_tokens", 4096)
    draft = ctx.llm(messages, role="improver", max_tokens=mx)
    p = parse_patch(draft)
    out = [p] if p else []
    hist = messages + [{"role": "assistant", "content": draft}]
    task = "\n".join(m["content"] for m in messages if m["role"] == "user")
    for _ in range(knob(ctx, "refine_iters") if p else 0):
        fb = ctx.llm([{"role": "system", "content": FEEDBACK.format(focus=knob(ctx, "critic_focus"))},
                      {"role": "user", "content": f"TASK:\n{task}\n\nDRAFT PATCH (block {p.block}):\n{p.content}\n\nREASON: {p.reason}"}],
                     role="improver", max_tokens=1024).strip()
        if "VERDICT: OK" in fb:   # VERDICT 줄이 없으면 수정으로 본다 (실측: 9회 중 3회 VERDICT 생략, OK로 보면 초안이 그대로 나갔다)
            break
        hist.append({"role": "user", "content": f"Reviewer feedback on your patch:\n{fb}\n\nRewrite the patch addressing every point. Same answer format, entire block."})
        text = ctx.llm(hist, role="improver", max_tokens=mx)
        hist.append({"role": "assistant", "content": text})
        p = parse_patch(text)
        if not p:
            break
        out.insert(0, p)
    return out


if __name__ == "__main__":   # python -m hooks.propose : LLM 없이 흐름 확인
    from types import SimpleNamespace
    pt = lambda n: f"BLOCK: prompt\nREASON: r{n}\n<<<CONTENT\nX = {n}\nCONTENT>>>"
    log = []
    def fake(messages, **kw):
        log.append(messages)
        if messages[0]["content"].startswith("You are reviewing"):
            return "- fix X" if len(log) < 4 else "VERDICT: OK"   # VERDICT 없음 = 수정
        return pt(len(log))
    ctx = SimpleNamespace(config={}, improver={"refine_iters": 3}, llm=fake)
    res = propose(ctx, [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
    assert [p.reason for p in res] == ["r3", "r1"] and len(log) == 4, ([p.reason for p in res], len(log))   # draft, fb(REVISE), refine, fb(OK)
    assert log[2][3]["role"] == "user" and "Reviewer feedback" in log[2][3]["content"] and log[2][1]["content"] == "u"   # 수정 요청 = 원 prompt + 초안 + 피드백
    log.clear(); ctx.improver = {"refine_iters": 0}
    assert len(propose(ctx, [{"role": "user", "content": "u"}])) == 1 and len(log) == 1   # 끄면 v0와 같음
    print("propose ok")
