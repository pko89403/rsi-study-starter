"""카탈로그(scripts/make_catalog.py 결과) + 명세(scripts/split_spec.yaml) → data/validation.json, test.json, fewshot_pool.json.
운영자가 한 번 실행하고 결과만 배포한다. 같은 카탈로그·명세·seed면 같은 결과.

  uv run python -m scripts.make_splits --catalog catalog.jsonl [--spec scripts/split_spec.yaml] [--out data]

규칙
- validation·pool은 train DB, test는 dev DB. 쌍둥이(같은 DB·같은 gold 골격)는 한 split에 하나만, pool에는 validation의 쌍둥이를 넣지 않는다.
- 주차 want는 validation 최소 개수. test는 n_test/n_val 배. 남는 자리는 난이도 비율대로 채운다.
"""
from __future__ import annotations
import argparse, json, random, sys
from collections import Counter
from pathlib import Path
import yaml

KEYS = ("v0", "tags", "hardness")


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


def compose(items: list[dict], n: int, qs: list[tuple[str, str, int]], rng: random.Random) -> tuple[list[dict], list[str]]:
    """할당량을 채우고 나머지는 난이도 비율대로. 쌍둥이 그룹당 하나."""
    rng.shuffle(items)
    out: list[dict] = []; used_twin: set[str] = set(); notes = []

    def take(it: dict) -> None:
        out.append(it); used_twin.add(it["twin"])

    for key, val, want in qs:
        have = sum(has(it, key, val) for it in out)
        for it in items:
            if have >= want or len(out) >= n:
                break
            if it not in out and it["twin"] not in used_twin and has(it, key, val):
                take(it); have += 1
        if have < want:
            notes.append(f"{key}={val}: {have}/{want} (후보 부족)")
    rest = [it for it in items if it not in out and it["twin"] not in used_twin]
    by_h = Counter(it["hardness"] for it in items); left = n - len(out)
    for h, cnt in by_h.items():   # 남은 자리를 난이도 비율대로
        need = round(left * cnt / len(items))
        for it in [r for r in rest if r["hardness"] == h][:need]:
            if len(out) < n and it["twin"] not in used_twin:
                take(it)
    for it in rest:
        if len(out) >= n:
            break
        if it not in out and it["twin"] not in used_twin:
            take(it)
    return out[:n], notes


def report(name: str, items: list[dict], spec: dict, scale: float) -> None:
    print(f"\n{name}: {len(items)}  v0 ok={sum(i['v0']=='ok' for i in items)}  DB {len({i['db_id'] for i in items})}개")
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
    a = ap.parse_args()
    spec = yaml.safe_load(Path(a.spec).read_text())
    cat = [json.loads(l) for l in Path(a.catalog).read_text().splitlines() if l.strip()]
    train = [c for c in cat if c["side"] == "train"]; dev = [c for c in cat if c["side"] == "dev"]
    rng = random.Random(spec["seed"])
    val, n1 = compose(train, spec["n_val"], quotas(spec, 1.0), rng)
    t_scale = spec["n_test"] / spec["n_val"]
    test, n2 = compose(dev, spec["n_test"], quotas(spec, t_scale), rng)
    val_ids = {i["id"] for i in val}; val_twins = {i["twin"] for i in val}
    pool_c = [c for c in train if c["id"] not in val_ids and c["twin"] not in val_twins]
    pool, n3 = compose(pool_c, spec["n_pool"], [], rng)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    keep = ("id", "db_id", "question", "gold_sql", "hardness")
    for name, items in (("validation", val), ("test", test), ("fewshot_pool", pool)):
        (out / f"{name}.json").write_text(json.dumps([{k: i[k] for k in keep} for i in items], ensure_ascii=False, indent=1))
    report("validation", val, spec, 1.0); report("test", test, spec, t_scale); report("fewshot_pool", pool, spec, 0)
    cover = {t for i in val for t in i["tags"]} - {t for i in pool for t in i["tags"]}
    print(f"\npool에 없는 validation 태그: {sorted(cover) or '없음'}  | validation 쌍둥이가 pool에: 0 (제외함)")
    for n in n1 + n2 + n3:
        print("  부족:", n)
    assert not (val_ids & {i["id"] for i in pool}) and not ({i["db_id"] for i in val} & {i["db_id"] for i in test})


def selfcheck() -> None:
    items = [{"id": f"t{i}", "twin": f"g{i // 2}", "v0": "exec_error" if i < 10 else "ok", "tags": ["join"] if i % 3 == 0 else [],
              "hardness": ["easy", "hard"][i % 2]} for i in range(200)]
    out, notes = compose(items, 50, [("v0", "exec_error", 5), ("v0", "empty", 1)], random.Random(0))
    assert len(out) == 50 and len({i["twin"] for i in out}) == 50                      # 쌍둥이 그룹당 하나
    assert sum(i["v0"] == "exec_error" for i in out) == 5                              # 후보 10개(그룹 5개)라 5개가 상한
    assert notes == ["v0=empty: 0/1 (후보 부족)"]
    assert quotas({"weeks": {3: {"want": {"v0": {"ok": 4}}}, 4: {"want": {"v0": {"ok": 9}}}, 5: {}}}, 2.0) == [("v0", "ok", 18)]
    print("splits ok")


if __name__ == "__main__":
    selfcheck() if sys.argv[1:] == ["selfcheck"] else main()
