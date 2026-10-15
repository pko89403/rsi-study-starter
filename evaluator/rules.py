"""채점 규칙. 6주차에 `evaluator` 블록을 열면 Improver가 고칠 수 있다. test 채점은 rules_v0.py(고정)를 쓴다."""
from __future__ import annotations
import re
from collections import Counter

# === EVOLVE-BLOCK-START: evaluator ===
def select_problems(problems: list[dict]) -> list[dict]:
    """validation에서 실제로 채점할 문제. v0: 전부."""
    return problems


def compare(pred_rows: list[tuple], gold_rows: list[tuple], gold_sql: str) -> bool:
    """예측 결과와 정답 결과가 같은가. v0: 정답에 ORDER BY가 있으면 순서까지, 없으면 multiset."""
    ordered = re.search(r"\border\s+by\b", gold_sql, re.I) is not None
    return pred_rows == gold_rows if ordered else Counter(pred_rows) == Counter(gold_rows)
# === EVOLVE-BLOCK-END ===
