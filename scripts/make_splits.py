"""Spider 원본 → data/ (validation.json, fewshot_pool.json, test.json, tables.json, database/).
운영자가 한 번 실행하고 결과만 배포한다. 원본 경로는 인자로 받는다.
"""
from __future__ import annotations
import argparse, json, random, shutil, sqlite3
from pathlib import Path


def gold_runs(db_dir: Path, db_id: str, sql: str) -> bool:
    db = db_dir / db_id / f"{db_id}.sqlite"
    if not db.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        con.execute(sql).fetchall()
        con.close()
        return True
    except Exception:
        return False


def pick(items: list[dict], n: int, rng: random.Random) -> list[dict]:
    """hardness 비율을 유지하며 n개 추출 (hardness 없으면 균등)."""
    by: dict[str, list[dict]] = {}
    for it in items:
        by.setdefault(it.get("hardness", "unknown"), []).append(it)
    total = len(items)
    out: list[dict] = []
    for k, group in by.items():
        k_n = round(n * len(group) / total)
        out += rng.sample(group, min(k_n, len(group)))
    # 반올림 오차 보정
    rest = [it for it in items if it not in out]
    rng.shuffle(rest)
    while len(out) < n and rest:
        out.append(rest.pop())
    return out[:n]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spider", required=True, help="spider_data 폴더")
    ap.add_argument("--out", default="data")
    ap.add_argument("--n-val", type=int, default=100)
    ap.add_argument("--n-pool", type=int, default=300)
    ap.add_argument("--n-test", type=int, default=200)
    ap.add_argument("--n-train-db", type=int, default=15)
    ap.add_argument("--n-dev-db", type=int, default=10)
    ap.add_argument("--seed", type=int, default=20261017)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    src = Path(a.spider)
    out = Path(a.out)
    db_dir = src / "database"

    def load(name: str, prefix: str) -> list[dict]:
        rows = json.loads((src / name).read_text())
        items = []
        for i, r in enumerate(rows):
            if not gold_runs(db_dir, r["db_id"], r["query"]):
                continue
            items.append({"id": f"{prefix}_{i:04d}", "db_id": r["db_id"],
                          "question": r["question"], "gold_sql": r["query"]})
        return items

    train = load("train_spider.json", "train")
    dev = load("dev.json", "dev")
    # hardness: Spider 공식 분류기 대신 간단 추정 (JOIN/GROUP/ORDER/중첩 개수)
    def hardness(sql: str) -> str:
        s = sql.upper()
        k = sum(w in s for w in (" JOIN ", "GROUP BY", "ORDER BY", "HAVING", "INTERSECT", "UNION", "EXCEPT")) + s.count("SELECT") - 1
        return ["easy", "medium", "hard", "extra"][min(k, 3)]
    for it in train + dev:
        it["hardness"] = hardness(it["gold_sql"])

    train_dbs = sorted({it["db_id"] for it in train})
    dev_dbs = sorted({it["db_id"] for it in dev})
    sel_train_db = set(rng.sample(train_dbs, a.n_train_db))
    sel_dev_db = set(rng.sample(dev_dbs, a.n_dev_db))
    train_sel = [it for it in train if it["db_id"] in sel_train_db]
    dev_sel = [it for it in dev if it["db_id"] in sel_dev_db]
    assert len(train_sel) >= a.n_val + a.n_pool, f"train 후보 부족: {len(train_sel)}"
    assert len(dev_sel) >= a.n_test, f"dev 후보 부족: {len(dev_sel)}"

    val = pick(train_sel, a.n_val, rng)
    val_ids = {it["id"] for it in val}
    pool = pick([it for it in train_sel if it["id"] not in val_ids], a.n_pool, rng)
    test = pick(dev_sel, a.n_test, rng)

    out.mkdir(parents=True, exist_ok=True)
    (out / "validation.json").write_text(json.dumps(val, ensure_ascii=False, indent=1))
    (out / "fewshot_pool.json").write_text(json.dumps(pool, ensure_ascii=False, indent=1))
    (out / "test.json").write_text(json.dumps(test, ensure_ascii=False, indent=1))
    tables = json.loads((src / "tables.json").read_text())
    used = sel_train_db | sel_dev_db
    (out / "tables.json").write_text(json.dumps([t for t in tables if t["db_id"] in used], ensure_ascii=False))
    for db in used:
        dst = out / "database" / db
        if not dst.exists():
            shutil.copytree(db_dir / db, dst)
    size = sum(p.stat().st_size for p in (out / "database").rglob("*")) / 1e6
    print(f"validation {len(val)} / pool {len(pool)} / test {len(test)} / DB {len(used)}개 {size:.0f}MB")
    assert not ({it["id"] for it in pool} & val_ids)


if __name__ == "__main__":
    main()
