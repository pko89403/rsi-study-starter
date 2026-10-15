"""틀린 문제를 보고 원인 한 줄을 어떻게 만들지. 기본: 틀린 15개를 Improver LLM에 주고 diagnose.md 로 한 줄."""
from __future__ import annotations
from loop.context import RoundContext


def diagnose(ctx: RoundContext) -> str:
    wrong = [p for p in ctx.before["per_problem"] if not p["correct"]][:15]
    if not wrong:
        return "no wrong answers"
    user = "\n".join(f"Q: {ctx.problem(p['id'])['question']}\nSQL: {ctx.prediction(p['id'])}\nerror: {p['error']}" for p in wrong)
    return ctx.llm([{"role": "system", "content": ctx.prompt_text("diagnose.md")},
                    {"role": "user", "content": user}], role="improver", max_tokens=ctx.config.get("improver_max_tokens", 4096)).strip()
