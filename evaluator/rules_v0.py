"""채점 규칙의 고정 복사본. test 채점(scripts/test_eval.py)이 쓴다. Improver가 고칠 수 없다."""
from __future__ import annotations
import re
from collections import Counter


def select_problems(problems: list[dict]) -> list[dict]:
    return problems


def compare(pred_rows: list[tuple], gold_rows: list[tuple], gold_sql: str) -> bool:
    ordered = re.search(r"\border\s+by\b", gold_sql, re.I) is not None
    return pred_rows == gold_rows if ordered else Counter(pred_rows) == Counter(gold_rows)
