"""rounds.jsonl → 결과 표 5칸 중 자동으로 채울 수 있는 것(전/후, 비용, 거부 사유 분포)."""
from __future__ import annotations
import argparse, json
from collections import Counter
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--week", type=int, required=True); ap.add_argument("--log", default="logs/rounds.jsonl")
    a = ap.parse_args()
    rows = [json.loads(l) for l in Path(a.log).read_text().splitlines() if l.strip()]
    rows = [r for r in rows if r["week"] == a.week]
    if not rows:
        print("no rounds for week", a.week); return
    first, last = rows[0], rows[-1]
    best_after = max((r["full"]["after"] for r in rows if r["accepted"]), default=None)
    print(f"week {a.week}: rounds {len(rows)}, hook_changed={last['hook_changed']}")
    rul = Counter(r.get("ruler", "?") for r in rows); anch = sorted({r.get("anchors_sha", "?") for r in rows})
    print(f"고정 파일 검사: {dict(rul)} | anchors_sha: {', '.join(anch)}" + ("  ← override/변경 있음. 전제 조건에 적을 것" if set(rul) - {"ok"} else ""))
    print(f"validation 전/후: {first['full']['before']}/{first['full']['n']} -> {best_after if best_after is not None else first['full']['before']}/{first['full']['n']}")
    print(f"비용: LLM 호출 {sum(r['llm_calls'] for r in rows)}회, {sum(r['seconds'] or 0 for r in rows)/60:.1f}분")
    print("accept", sum(r["accepted"] for r in rows), "/ 거부 사유:", dict(Counter(r["rejected_by"] for r in rows if r["rejected_by"])))
    print("진단 목록:"); [print(" -", r["diagnosis"]) for r in rows]
    print("test 전/후, 실패 유형, 전제 조건, 서비스에 넣는다면 은 사람이 적습니다.")


if __name__ == "__main__":
    main()
