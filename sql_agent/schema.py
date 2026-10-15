"""tables.json → 스키마 텍스트. EVOLVE-BLOCK 밖."""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _tables(path: str) -> dict[str, dict]:
    return {t["db_id"]: t for t in json.loads(Path(path).read_text())}


def schema_text(tables_path: str, db_id: str) -> str:
    t = _tables(tables_path)[db_id]
    cols: dict[int, list[str]] = {}
    for (ti, name), typ in zip(t["column_names_original"], t["column_types"]):
        if ti >= 0:
            cols.setdefault(ti, []).append(f"{name} {typ}")
    lines = [f"table {tn}: " + ", ".join(cols.get(i, [])) for i, tn in enumerate(t["table_names_original"])]
    fks = []
    for a, b in t.get("foreign_keys", []):
        ta, ca = t["column_names_original"][a]; tb, cb = t["column_names_original"][b]
        fks.append(f"{t['table_names_original'][ta]}.{ca} -> {t['table_names_original'][tb]}.{cb}")
    if fks:
        lines.append("foreign keys: " + "; ".join(fks))
    return "\n".join(lines)
