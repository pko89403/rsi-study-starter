"""틀린 문제를 보고 원인 한 줄을 어떻게 만들지. 기본: 틀린 문제 표본을 Improver LLM에 주고 diagnose.md 로 한 줄."""
from __future__ import annotations
import random
from loop.context import RoundContext

KNOBS = {
    "diagnose_n": ("int", 5, 25, 15),     # 진단에 보여 줄 틀린 문제 수
    "diagnose_focus": ("str", 300, ""),   # 진단할 때 무엇을 먼저 보라는 한 문장
}
KNOB_DOCS = {
    "diagnose_n": "how many wrong examples the diagnosis step reads before writing its one-sentence cause",
    "diagnose_focus": "an instruction sentence telling the diagnosis step which kind of failure to look at first",
}


def diagnose(ctx: RoundContext) -> str:
    k = lambda name: (ctx.improver or {}).get(name, KNOBS[name][-1])
    wrong = [p for p in ctx.before["per_problem"] if not p["correct"]]
    if not wrong:
        return "no wrong answers"
    random.Random(ctx.seed).shuffle(wrong)
    wrong = wrong[: k("diagnose_n")]
    user = "\n".join(f"Q: {ctx.problem(p['id'])['question']}\nSQL: {ctx.prediction(p['id'])}\nerror: {p['error']}" for p in wrong)
    system = ctx.prompt_text("diagnose.md") + (f"\nFocus: {k('diagnose_focus')}" if k("diagnose_focus") else "")
    return ctx.llm([{"role": "system", "content": system}, {"role": "user", "content": user}],
                   role="improver", max_tokens=ctx.config.get("improver_max_tokens", 4096)).strip()
