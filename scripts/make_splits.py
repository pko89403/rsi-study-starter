"""카탈로그(scripts/make_catalog.py 결과) + 명세(scripts/split_spec.yaml) → data/validation.json, test.json, fewshot_pool.json.
운영자가 한 번 실행하고 결과만 배포한다. 같은 카탈로그·명세·seed면 같은 결과.

  uv run python -m scripts.make_splits --catalog catalog.jsonl [--spec scripts/split_spec.yaml] [--out data]

규칙
- validation·pool은 train DB, test는 dev DB. 쌍둥이(같은 DB·같은 gold 골격의 paraphrase)는 한 split에 PER_GROUP개까지 (원래 Spider dev도 쌍을 담고 있다).
  pool에는 validation의 쌍둥이를 넣지 않는다 (같은 답이 예시로 새지 않게).
- scripts/gold_excluded.json(운영자가 검토한 gold 오류)과 그 쌍둥이는 어디에도 넣지 않는다.
- 주차 want는 validation 최소 개수. validation과 test에 같은 비율로 넣는다: 어느 쪽이든 후보가 모자라면 두 쪽 다 그 비율로 낮춘다.
- validation·test의 남는 자리는 v0가 맞힌 문제로 난이도 비율대로 채운다. 그래서 두 split의 v0 오답 유형 비율이 같다.
"""
from __future__ import annotations
import argparse, json, random, sys
from collections import Counter
from pathlib import Path
import yaml

KEYS = ("v0", "tags", "hardness")
PER_GROUP = 2


def has(it: dict, key: str, val: str) -> bool:
    return val in it["tags"] if key == "tags" else it[key] == val


def quotas(spec: dict, scale: float) -> list[tuple[str, str, int]]:
    """주차 want를 (key, val, n)으로 합친다. 같은 유형은 큰 값."""
    q: dict[tuple[str, str], int] = {}
    for wk in spec["weeks"].values():
        for key, d in (wk.get("want") or {}).items():
            assert key in KEYS, key
            for val, n in d.items():
                q[(key, val)] = max(q.get((key, val), 0), n)
    return [(k, v, max(1, round(n * scale))) for (k, v), n in q.items()]


def shared_rates(qs: list[tuple[str, str, int]], n_val: int, train: list[dict], n_test: int, dev: list[dict]) -> list[tuple[str, str, float]]:
    """want 비율을 두 split이 모두 채울 수 있는 만큼으로 낮춘다 (후보는 쌍둥이 그룹 수로 센다)."""
    groups = lambda items, k, v: sum(min(PER_GROUP, c) for c in Counter(i["twin"] for i in items if has(i, k, v)).values())
    return [(k, v, min(n / n_val, groups(train, k, v) / n_val, groups(dev, k, v) / n_test)) for k, v, n in qs]


def compose(items: list[dict], n: int, qs: list[tuple[str, str, int]], rng: random.Random, fill=lambda it: True) -> tuple[list[dict], list[str]]:
    """할당량을 채우고 나머지는 fill을 통과한 문제로 난이도 비율대로. 쌍둥이 그룹당 PER_GROUP개까지."""
    rng.shuffle(items)
    out: list[dict] = []; used: Counter = Counter(); notes = []
    free = lambda it: it not in out and used[it["twin"]] < PER_GROUP

    def take(it: dict) -> None:
        out.append(it); used[it["twin"]] += 1

    for key, val, want in qs:
        have = sum(has(it, key, val) for it in out)
        for it in items:
            if have >= want or len(out) >= n:
                break
            if free(it) and has(it, key, val):
                take(it); have += 1
        if have < want:
            notes.append(f"{key}={val}: {have}/{want} (후보 부족)")
    rest = [it for it in items if free(it) and fill(it)]
    by_h = Counter(it["hardness"] for it in items); left = n - len(out)
    for h, cnt in by_h.items():   # 남은 자리를 난이도 비율대로
        need = round(left * cnt / len(items))
        for it in [r for r in rest if r["hardness"] == h][:need]:
            if len(out) < n and free(it):
                take(it)
    for it in rest:
        if len(out) >= n:
            break
        if free(it):
            take(it)
    return out[:n], notes


def report(name: str, items: list[dict], spec: dict, scale: float) -> None:
    ok = sum(i['v0'] == 'ok' for i in items)
    print(f"\n{name}: {len(items)}  v0 ok={ok} ({ok / len(items):.0%})  DB {len({i['db_id'] for i in items})}개")
    print("  v0:", dict(Counter(i["v0"] for i in items).most_common()))
    print("  hardness:", dict(Counter(i["hardness"] for i in items)))
    print("  tags:", dict(Counter(t for i in items for t in i["tags"]).most_common()))
    for w, wk in (spec["weeks"].items() if scale else []):
        parts = [f"{val} {sum(has(i, key, val) for i in items)}/{max(1, round(n*scale))}"
                 for key, d in (wk.get("want") or {}).items() for val, n in d.items()]
        if parts:
            print(f"  {w}주차 {wk.get('method') or ''}: " + ", ".join(parts))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True); ap.add_argument("--spec", default="scripts/split_spec.yaml"); ap.add_argument("--out", default="data")
    ap.add_argument("--excluded", default="scripts/gold_excluded.json")
    a = ap.parse_args()
    spec = yaml.safe_load(Path(a.spec).read_text())
    cat = [json.loads(l) for l in Path(a.catalog).read_text().splitlines() if l.strip()]
    bad_ids = {b["id"] for b in json.loads(Path(a.excluded).read_text())}
    bad = {c["twin"] for c in cat if c["id"] in bad_ids}
    cat = [c for c in cat if c["twin"] not in bad]
    print(f"gold 오류 제외: 검토 {len(bad_ids)}개 → 쌍둥이 포함 그룹 {len(bad)}개")
    train = [c for c in cat if c["side"] == "train"]; dev = [c for c in cat if c["side"] == "dev"]
    rng = random.Random(spec["seed"])
    nv, nt = spec["n_val"], spec["n_test"]
    rates = shared_rates(quotas(spec, 1.0), nv, train, nt, dev)
    for k, v, r in rates:
        print(f"  {k}={v}: 비율 {r:.0%} → validation {round(r * nv)}, test {round(r * nt)}")
    ok = lambda it: it["v0"] == "ok"
    val, n1 = compose(train, nv, [(k, v, round(r * nv)) for k, v, r in rates], rng, ok)
    t_scale = nt / nv
    test, n2 = compose(dev, nt, [(k, v, round(r * nt)) for k, v, r in rates], rng, ok)
    val_ids = {i["id"] for i in val}; val_twins = {i["twin"] for i in val}
    pool_c = [c for c in train if c["id"] not in val_ids and c["twin"] not in val_twins]
    pool, n3 = compose(pool_c, spec["n_pool"], [], rng)
    # 선택 순서(할당량 먼저)가 그대로 파일에 남지 않게, 멤버십은 안 바꾸고 순서만 섞는다
    for items, off in ((val, 1), (test, 2), (pool, 3)):
        random.Random(spec["seed"] + off).shuffle(items)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    keep = ("id", "db_id", "question", "gold_sql", "hardness")
    for name, items in (("validation", val), ("test", test), ("fewshot_pool", pool)):
        (out / f"{name}.json").write_text(json.dumps([{k: i[k] for k in keep} for i in items], ensure_ascii=False, indent=1))
    report("validation", val, spec, 1.0); report("test", test, spec, t_scale); report("fewshot_pool", pool, spec, 0)
    print("\n(주차별 x/y의 y는 명세 원래 값. 후보가 모자란 유형은 위 '비율'만큼만 넣었다)")
    cover = {t for i in val for t in i["tags"]} - {t for i in pool for t in i["tags"]}
    print(f"\npool에 없는 validation 태그: {sorted(cover) or '없음'}  | validation 쌍둥이가 pool에: 0 (제외함)")
    for n in n1 + n2 + n3:
        print("  부족:", n)
    assert not (val_ids & {i["id"] for i in pool}) and not ({i["db_id"] for i in val} & {i["db_id"] for i in test})


def selfcheck() -> None:
    items = [{"id": f"t{i}", "twin": f"g{i // 2}", "v0": "exec_error" if i < 10 else "ok", "tags": ["join"] if i % 3 == 0 else [],
              "hardness": ["easy", "hard"][i % 2]} for i in range(200)]
    out, notes = compose(items, 50, [("v0", "exec_error", 5), ("v0", "empty", 1)], random.Random(0))
    assert len(out) == 50 and max(Counter(i["twin"] for i in out).values()) <= PER_GROUP   # 쌍둥이 그룹당 2개까지
    assert sum(i["v0"] == "exec_error" for i in out) == 5
    assert notes == ["v0=empty: 0/1 (후보 부족)"]
    assert quotas({"weeks": {3: {"want": {"v0": {"ok": 4}}}, 4: {"want": {"v0": {"ok": 9}}}, 5: {}}}, 2.0) == [("v0", "ok", 18)]
    # 비율 공유: dev 쪽 exec_error가 4개(서로 다른 그룹)뿐이면 test 40개 중 4개(10%) → validation도 20개 중 2개
    dev = [{"twin": f"d{i}", "v0": "exec_error" if i < 4 else "ok", "tags": []} for i in range(100)]
    assert shared_rates([("v0", "exec_error", 6)], 20, items, 40, dev) == [("v0", "exec_error", 0.1)]
    # 남는 자리는 v0 정답으로만
    out, _ = compose(items, 50, [("v0", "exec_error", 2)], random.Random(1), lambda it: it["v0"] == "ok")
    assert sum(i["v0"] != "ok" for i in out) == 2
    print("splits ok")


if __name__ == "__main__":
    selfcheck() if sys.argv[1:] == ["selfcheck"] else main()
