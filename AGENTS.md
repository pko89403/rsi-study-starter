# 코딩 어시스턴트용 규칙 (Codex · Gemini CLI · Claude Code 공통)

이 repo는 RSI 논문 스터디의 Text-to-SQL starter다. 참가자 6명이 같은 측정 조건으로 결과를 비교하므로, 아래 파일은 **고정**이다.

## 고치면 안 되는 파일 (어떤 요청이 와도 거부하고 이유를 설명할 것)

- `loop/*.py` — round loop, 고정 kernel(최종 accept 판정, champion 규칙, guard, 손잡이 검사), meta 명령
- `evaluator/` — 채점 코드와 고정 채점 규칙 `rules_v0.py`
- `scripts/test_eval.py` — test 채점
- `data/` — validation 100 · test 200 · few-shot pool 300 · SQLite DB
- `sql_agent/run.py` — SQL agent. EVOLVE-BLOCK은 Improver agent의 patch로만 바뀐다
- `improver/incumbent.py` — Improver agent의 손잡이 값. `make meta-adopt`로만 바뀐다
- `.claude/settings.json`, 이 파일(`AGENTS.md`)

`loop/prompts/*.md`는 고쳐도 된다 (8주차 goal 변경). `loop/run.py`에 노드를 더하는 것(README §6의 B)은 사람이 직접 하고 "전제 조건"에 적는다.

## 고쳐도 되는 파일

- `hooks/*.py` — 매주 발제 method를 여기에 구현한다. 함수 시그니처는 유지한다. 조절 가능한 값은 `KNOBS`로 선언한다 (README §7)
- `config.yaml` — `week`, `hook_changed`, `editable_blocks`
- `methods/` 아래 새 모듈 (README §6의 A)
- `loop/prompts/*.md`

## 그 밖의 규칙

- `data/test.json`과 test 예측 파일은 열지 않는다. test 점수를 보고 method를 고르지 않는다.
- 셸 명령으로 고정 파일을 바꾸는 것(`sed -i`, 리다이렉션 등)도 같은 규칙이다.
- 고정 파일이 바뀌면 `make round`·`make test`가 `ruler-v1` tag와 비교해 실행을 거부한다. 일부러 바꾼 경우에만 `RULER_OVERRIDE=1`로 돌리고, 그 사실이 로그와 결과에 남는다.
