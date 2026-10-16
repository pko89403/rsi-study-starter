"""Improver 자기 방법(IMPROVER 손잡이 값) 개선. 참가자가 자기 starter에서 실행하고, 채택은 본인이 정한다.

  python -m loop.meta propose                       Improver LLM이 손잡이 1~2개를 바꾼 수정안을 improver/challenger.py에 쓴다
  python -m loop.meta eval [--file F] [--n N]       내 archive의 상황 N개에서 기존(incumbent) vs 수정안(challenger) 짝 비교 → 판정 카드
  python -m loop.meta adopt [--file F] [--force --reason R]   수정안을 incumbent로 채택 (git commit)

판정은 전부 고정 채점 rules_v0. 동료가 공유한 수정안은 --file 로 내 환경에서 다시 잰다 (합산하지 않는다).
"""
from __future__ import annotations
import argparse, copy, hashlib, json, math, random, re, sys, time
from pathlib import Path
from loop import kernel as K
from loop import patch as P
from loop import run as R
from loop.context import RoundContext
from sql_agent import llm

LEDGER = Path("logs/improver_ledger.jsonl")
CHALLENGER = Path("improver/challenger.py")
ALPHA, GATE_P, COSMETIC_FRAC = 0.05, 0.10, 0.8


def ledger() -> list[dict]:
    return [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()] if LEDGER.exists() else []


def log_ledger(rec: dict) -> None:
    LEDGER.parent.mkdir(exist_ok=True)
    with open(LEDGER, "a") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M"), **rec}, ensure_ascii=False) + "\n")


def load(path: Path, schema: dict) -> dict:
    values, errs = K.validate(K.read_improver(path), schema)
    if errs:
        print(f"{path}: {'; '.join(errs)} (기본값으로 채움)")
    return values


# ---------------- propose ----------------

def round_stats(cfg: dict, n: int = 20) -> str:
    p = Path(cfg["log"])
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()][-n:] if p.exists() else []
    if not rows:
        return "(no rounds yet)"
    by = {}
    for r in rows:
        by[r["rejected_by"] or "accepted"] = by.get(r["rejected_by"] or "accepted", 0) + 1
    nets = [r["net"] for r in rows if r.get("net") is not None]
    reasons = [r["reason"].split(" | ")[-1][:120] for r in rows if r.get("reason") and r["rejected_by"]][-5:]
    return (f"last {len(rows)} rounds: outcomes {by}; evaluated patches {len(nets)}, mean net "
            f"{(sum(nets) / len(nets)) if nets else 0:.2f}; mean re-asks {sum(r.get('resamples', 0) for r in rows) / len(rows):.1f}\n"
            "recent rejection reasons:\n" + "\n".join(f"- {x}" for x in reasons))


def focus_knob(schema: dict) -> str:
    """가장 오래 안 바꾼 손잡이 (temperature 0이라 입력을 바꿔야 매번 다른 수정안이 나온다)."""
    last = {}
    for i, r in enumerate(ledger()):
        for k in (r.get("changes") or {}):
            last[k] = i
    return min(schema, key=lambda k: (last.get(k, -1), k))


def propose(cfg: dict) -> None:
    schema = K.collect_knobs(R.hook_modules())
    if not schema:
        sys.exit("hook에 KNOBS가 없습니다. Improver가 조절할 손잡이를 hook에 선언하세요.")
    inc = load(Path(K.IMPROVER_FILE), schema)
    focus = focus_knob(schema)
    docs = K.collect_docs(R.hook_modules())
    rows = "\n".join(f"- {k}: {docs.get(k, '(no description)')}. Value: {K.describe(spec)}. Current: {inc[k]!r}" for k, spec in schema.items())
    past = "\n".join(f"- {r.get('changes')} -> {r.get('label', 'proposed')}" for r in ledger()[-8:] if r.get("changes")) or "(none)"
    msgs = [{"role": "system", "content":
             "You tune the improvement method of an Improver agent that patches a Text-to-SQL agent. Its method has knobs "
             "declared by its hooks; each knob says what it controls and which values are allowed. Propose ONE improvement: change 1 or 2 knob values "
             "within their specs, aimed at the failures in the stats. Answer with JSON only: "
             '{"changes": {"<knob>": <value>}, "hypothesis": "<one line: why this should yield more accepted patches>"}'},
            {"role": "user", "content": f"Knobs:\n{rows}\n\nLoop stats:\n{round_stats(cfg)}\n\nPast proposals:\n{past}\n\n"
                                        f"This time you must change the knob '{focus}' (you may also change one other)."}]
    for attempt in range(3):
        text = llm.chat(msgs, role="improver", max_tokens=cfg.get("improver_max_tokens", 4096))
        m = re.search(r"\{.*\}", text, re.S)
        why = None
        try:
            o = json.loads(m.group(0)) if m else None
            changes = o["changes"] if o and isinstance(o.get("changes"), dict) else None
        except (json.JSONDecodeError, KeyError, TypeError):
            changes = None
        if not changes:
            why = "answer was not the requested JSON"
        else:
            ch, errs = K.validate({**inc, **changes}, schema)
            if errs:
                why = "; ".join(errs)
            elif ch == inc:
                why = "no knob value changed"
        if why is None:
            break
        msgs += [{"role": "assistant", "content": text}, {"role": "user", "content": f"Rejected: {why}. Same JSON format."}]
    else:
        sys.exit(f"수정안을 받지 못했습니다: {why}")
    CHALLENGER.write_text(Path(K.IMPROVER_FILE).read_text())
    K.write_improver(ch, CHALLENGER)
    rec = {"type": "propose", "week": cfg["week"], "changes": {k: ch[k] for k in ch if ch[k] != inc[k]},
           "hypothesis": str(o.get("hypothesis", ""))[:300], "incumbent_sha": K.improver_sha(inc), "challenger_sha": K.improver_sha(ch)}
    log_ledger(rec)
    print(f"수정안 → {CHALLENGER}\n  바꾼 손잡이: {rec['changes']}\n  가설: {rec['hypothesis']}\n다음: make meta-eval")


# ---------------- eval ----------------

def sign_flip_p(d: list[float]) -> float:
    """단측 p: 평균 차이 > 0. 부호를 무작위로 뒤집은 분포에서 관측값 이상이 나올 확률."""
    nz = [x for x in d if x != 0]
    if not nz:
        return 1.0
    obs = sum(nz)
    if len(nz) <= 16:
        sums = [sum(x if (mask >> i) & 1 else -x for i, x in enumerate(nz)) for mask in range(1 << len(nz))]
        return sum(s >= obs - 1e-9 for s in sums) / len(sums)
    rng = random.Random(0)
    hits = sum(sum(x if rng.random() < 0.5 else -x for x in nz) >= obs - 1e-9 for _ in range(20000))
    return (hits + 1) / 20001


def binom_tail(k: int, n: int) -> float:
    """P(X >= k), X ~ Bin(n, 0.5)."""
    return sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n if n else 1.0


class _NoSeen:   # meta-eval에서는 이전에 시도한 patch도 그 arm의 정상 행동으로 센다
    def seen(self, h: str) -> bool:
        return False


def evaluate(cfg: dict, archive, file: Path, n: int, seed0: int, min_parents: int) -> None:
    schema = K.collect_knobs(R.hook_modules())
    inc, ch = load(Path(K.IMPROVER_FILE), schema), load(file, schema)
    ch_sha, inc_sha = K.improver_sha(ch), K.improver_sha(inc)
    base = {"type": "eval", "week": cfg["week"], "file": str(file), "incumbent_sha": inc_sha, "challenger_sha": ch_sha,
            "changes": {k: ch[k] for k in ch if ch[k] != inc[k]}}
    if ch == inc:
        print("판정: NULL (수정안과 기존 값이 같음)"); log_ledger({**base, "label": "NULL"}); return
    scores = archive.state["scores"]
    # 실제 patch의 출발점이었던 commit과 champion을 먼저, 모자라면 점수 높은 commit으로 채운다 (초기 주차는 parent가 1~2개뿐)
    real = [x for x in dict.fromkeys([archive.best, *archive.state["parents"].values()]) if x in scores]
    parents = (sorted(real, key=lambda x: -scores[x]) + [x for x in sorted(scores, key=lambda x: -scores[x]) if x not in real])[:6]
    if len(parents) < min_parents:
        print(f"판정: HOLD (서로 다른 parent {len(parents)}개 < {min_parents}. make round를 더 돌린 뒤 다시)")
        log_ledger({**base, "label": "HOLD", "parents": len(parents)}); return
    problems = json.loads(Path(cfg["validation"]).read_text())
    view, db_of = K.strip_gold(problems), {p["id"]: p["db_id"] for p in problems}
    editable, files = cfg["editable_blocks"], cfg["evolve_files"]
    before: dict = {}
    try:
        for pa in parents:
            archive.checkout(pa, files)
            res, err, preds = R.eval_on(problems, cfg, "meta")
            if err:
                sys.exit(f"parent {pa} failed to run: {err}")
            before[pa] = (res, preds, {b: P.read_block(P.locate(files, b), b) for b in editable})
        # 1) 상황 만들기 + 두 arm의 제안 (Improver 호출만)
        contexts, seen_in, calls = [], set(), 0
        for i in range(n):
            pa, seed = parents[i % len(parents)], seed0 + i
            res, preds, blocks = before[pa]
            bname = editable[seed % len(editable)]
            row = {"parent": pa, "seed": seed, "block": bname}
            for arm, vals in (("I", inc), ("C", ch)):
                ctx = RoundContext(round=0, week=cfg["week"], config=copy.deepcopy(cfg), parent=pa, block_name=bname, block=blocks[bname],
                                   blocks=blocks, before=res, problems=view, predictions=preds, llm=llm.chat, improver=vals, seed=seed)
                ctx.diagnosis = R.diagnose(ctx); calls += 1
                prompt_h = hashlib.sha1(json.dumps(R.build_prompt(ctx), sort_keys=True).encode()).hexdigest()
                pt, _, _, c = R.propose_valid(ctx, editable, _NoSeen()); calls += c
                row[arm] = {"prompt": prompt_h, "patch": pt, "hash": R.patch_hash(pt) if pt else None}
            if row["I"]["prompt"] in seen_in:
                continue   # 기존 버전 입력이 같은 상황은 하나로 센다
            seen_in.add(row["I"]["prompt"]); contexts.append(row)
        if all(r["I"]["prompt"] == r["C"]["prompt"] for r in contexts):
            print("판정: NULL (두 버전의 Improver 입력이 모든 상황에서 같음 → hook이 바뀐 손잡이를 읽지 않음)")
            log_ledger({**base, "label": "NULL", "n_distinct": len(contexts)}); return
        live = [r for r in contexts if r["I"]["hash"] or r["C"]["hash"]]
        if not live:
            print("판정: MECH-FAIL (두 버전 모두 유효한 patch를 하나도 못 냄)")
            log_ledger({**base, "label": "MECH-FAIL", "n_distinct": len(contexts)}); return
        same = sum(r["I"]["hash"] == r["C"]["hash"] for r in live) / len(live)
        if same >= COSMETIC_FRAC:
            print(f"판정: COSMETIC (두 버전 patch가 {same:.0%} 같음 → 평가 생략)")
            log_ledger({**base, "label": "COSMETIC", "n_distinct": len(contexts), "same_patch": round(same, 2)}); return
        # 2) 서로 다른 patch만 평가 (parent별, patch별 1회)
        results: dict = {}
        for r in contexts:
            for arm in ("I", "C"):
                pt = r[arm]["patch"]
                if pt is None or (r["parent"], r[arm]["hash"]) in results:
                    continue
                res, preds, _ = before[r["parent"]]
                archive.checkout(r["parent"], files)
                target = P.locate(files, pt.block); src = target.read_text()
                P.apply(target, pt.block, pt.content)
                net, ok, changed = 0, False, 0
                if not P.compiles(f"{target.parent.name}.{target.stem}") and not P.outside_changed(src, target.read_text(), pt.block):
                    fres, err, fpreds = R.eval_on(problems, cfg, "meta")
                    if not err:
                        ok, info = K.kernel_accept(res, fres, preds, fpreds, db_of)
                        net, changed = info["net"], info["changed"]
                target.write_text(src)
                results[(r["parent"], r[arm]["hash"])] = (net, ok, changed)
    finally:
        archive.checkout(archive.best, files)
    # 3) 통계와 판정
    def score(r, arm):   # (순증, kernel accept, 바뀐 답 수). 유효 patch가 아니면 0점 (ITT)
        return results.get((r["parent"], r[arm]["hash"]), (0, False, 0)) if r[arm]["patch"] else (0, False, 0)
    d = [score(r, "C")[0] - score(r, "I")[0] for r in contexts]
    vI = [score(r, "I")[2] > 0 for r in contexts]   # 유효 = 사전 검사 통과 + 실행됨 + 예측이 parent와 다름
    vC = [score(r, "C")[2] > 0 for r in contexts]
    worse, better = sum(a and not b for a, b in zip(vI, vC)), sum(b and not a for a, b in zip(vI, vC))
    p_up, p_down = sign_flip_p(d), sign_flip_p([-x for x in d])
    p_worse = binom_tail(worse, worse + better)
    gate = p_worse > GATE_P
    k = 1 + sum(1 for r in ledger() if r.get("type") == "eval" and r.get("week") == cfg["week"] and r.get("label") in ("ADOPT-RECOMMENDED", "KEEP", "REJECT"))
    alpha = ALPHA / k
    mean = sum(d) / len(d) if d else 0.0
    if p_up < alpha and mean > 0 and gate:
        label = "ADOPT-RECOMMENDED"
    elif (p_down < alpha and mean < 0) or not gate:
        label = "REJECT"
    else:
        label = "KEEP"
    acc = (sum(score(r, "I")[1] for r in contexts), sum(score(r, "C")[1] for r in contexts))
    print(f"""
meta-eval   challenger={ch_sha} ({file})  vs  incumbent={inc_sha}
바꾼 손잡이  {base['changes']}
상황        서로 다른 상황 {len(contexts)}개 / parent {len(parents)}개   Improver 호출 {calls}회
1단계       같은 patch {same:.0%}   유효 patch I/C = {sum(vI)}/{sum(vC)}   유효성 악화 p={p_worse:.3f} [{'PASS' if gate else 'FAIL'}]
효과        평균 순증 차이 = {mean:+.2f}   sign-flip p = {p_up:.3f} (기준 {alpha:.3f}, 이번 주 {k}번째 판정)
보조        kernel accept 가능 patch I/C = {acc[0]}/{acc[1]}
판정        {label}   ({'채택 권고: make meta-adopt' if label == 'ADOPT-RECOMMENDED' else '기존 유지 권고. 그래도 채택하려면 make meta-adopt FORCE=1 REASON="..."'})
""")
    log_ledger({**base, "label": label, "n_distinct": len(contexts), "parents": len(parents), "mean_d": round(mean, 3), "p": round(p_up, 4),
                "alpha": alpha, "valid_I": sum(vI), "valid_C": sum(vC), "p_worse": round(p_worse, 4), "accept_I": acc[0], "accept_C": acc[1], "calls": calls})


# ---------------- adopt ----------------

def adopt(archive, file: Path, force: bool, reason: str) -> None:
    schema = K.collect_knobs(R.hook_modules())
    ch = load(file, schema)
    sha = K.improver_sha(ch)
    last = next((r for r in reversed(ledger()) if r.get("type") == "eval" and r.get("challenger_sha") == sha), None)
    label = last["label"] if last else "NOT-EVALUATED"
    if label != "ADOPT-RECOMMENDED" and not force:
        sys.exit(f"판정이 {label} 입니다. 그래도 채택하려면 make meta-adopt FORCE=1 REASON=\"사유\"")
    if force and not reason.strip():
        sys.exit("FORCE 채택에는 REASON이 필요합니다")
    prev = K.improver_sha(load(Path(K.IMPROVER_FILE), schema))
    K.write_improver(ch, K.IMPROVER_FILE)
    archive.repo.index.add([K.IMPROVER_FILE])
    archive.repo.index.commit(f"meta: IMPROVER {prev} -> {sha} ({label}{', FORCE: ' + reason if force else ''})")
    log_ledger({"type": "adopt", "challenger_sha": sha, "incumbent_sha": prev, "label": label, "force": force, "reason": reason})
    print(f"채택: IMPROVER {prev} -> {sha} ({label})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["propose", "eval", "adopt"])
    ap.add_argument("--file", default=str(CHALLENGER)); ap.add_argument("--n", type=int); ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--min-parents", type=int); ap.add_argument("--force", action="store_true"); ap.add_argument("--reason", default="")
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()
    cfg, archive = R.setup(a.config)
    if a.cmd == "propose":
        propose(cfg)
    elif a.cmd == "eval":
        evaluate(cfg, archive, Path(a.file), a.n or cfg.get("meta_contexts", 60), a.seed,
                 a.min_parents if a.min_parents is not None else cfg.get("meta_min_parents", 3))
    else:
        adopt(archive, Path(a.file), a.force, a.reason)


if __name__ == "__main__":
    if sys.argv[1:] == ["selfcheck"]:   # python -m loop.meta selfcheck : 통계 함수 확인 (LLM 없음)
        assert sign_flip_p([1, 1, 1, 1, 1]) == 1 / 32
        assert sign_flip_p([0, 0]) == 1.0 and sign_flip_p([-1, -1, -1]) == 1.0
        assert abs(binom_tail(3, 3) - 0.125) < 1e-12 and binom_tail(0, 0) == 1.0
        assert 0 < sign_flip_p([1] * 30 + [-1] * 10) < 0.01
        print("meta ok")
    else:
        main()
