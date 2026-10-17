"""Improver agent의 자기 방법: hook들이 KNOBS로 선언한 파라미터(KNOBS)의 현재 값.
Improver가 make meta-propose로 수정안을 내고, 참가자가 make meta-eval로 재 본 뒤 make meta-adopt로 채택한다.
데이터만 둔다. loop/kernel.py가 literal로 읽고 KNOBS 스키마로 검사한다 (실행하지 않는다). 비어 있으면 전부 기본값."""
# === EVOLVE-BLOCK-START: improver ===
IMPROVER = {}
# === EVOLVE-BLOCK-END ===
