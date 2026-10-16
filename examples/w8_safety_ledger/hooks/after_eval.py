"""평가 뒤 무엇을 기록·축적할지.
8주차: round 증거 한 줄을 ledger에 남긴다: screen/full 이득, proxy 괴리(screen은 오르는데 full은 안 오름), 이번 round의 DENY·위반 수."""
from loop.context import RoundContext
from methods import safety_ledger as L

KNOBS = {
    "proxy_margin": ("int", 1, 5, 2),   # screen 이득이 이만큼인데 full 이득이 0 이하이면 proxy 괴리로 표시
}
KNOB_DOCS = {
    "proxy_margin": "screen-20 gain at or above which a round with no full-100 gain is flagged as proxy-decoupled in the ledger",
}


def after_eval(ctx: RoundContext) -> None:
    margin = (ctx.improver or {}).get("proxy_margin", KNOBS["proxy_margin"][-1])
    n = ctx.config["screen_size"]
    screen_gain = full_gain = None
    if ctx.screen:
        screen_gain = ctx.screen["correct"] - sum(1 for p in ctx.before["per_problem"][:n] if p["correct"])
    if ctx.after:
        full_gain = ctx.after["correct"] - ctx.before["correct"]
    attempts = [r for r in L.read() if r.get("kind") == "attempt" and r.get("round") == ctx.round]
    L.append({"kind": "round", "round": ctx.round, "week": ctx.week, "patched": ctx.patch is not None, "accepted": bool(ctx.accepted),
              "screen_gain": screen_gain, "full_gain": full_gain,
              "proxy_decoupled": screen_gain is not None and full_gain is not None and screen_gain >= margin and full_gain <= 0,
              "denies": sum(r["decision"] == "DENY" for r in attempts), "violations": sum(bool(r["violation"]) for r in attempts)})
