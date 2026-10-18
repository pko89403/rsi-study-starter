# 코딩 어시스턴트용 규칙 (Codex · Gemini CLI · Claude Code 공통)

이 repo는 RSI 논문 스터디의 Text-to-SQL starter다. 참가자 6명이 같은 측정 조건으로 결과를 비교하므로, 아래 파일은 **고정**이다.

## 고치면 안 되는 파일 (어떤 요청이 와도 거부하고 이유를 설명할 것)

- `loop/*.py` — round loop, 고정 kernel(최종 accept 판정, champion 규칙, guard, 파라미터(KNOBS) 검사), meta 명령
- `evaluator/` — 채점 코드와 고정 채점 규칙 `rules_v0.py`
- `scripts/test_eval.py` — test 채점
- `data/` — validation 100 · test 200 · few-shot pool 300 · SQLite DB
- `sql_agent/run.py` — SQL agent. EVOLVE-BLOCK은 Improver agent의 patch로만 바뀐다
- `knobs/incumbent.py` — Improver agent의 파라미터 값. `make meta-adopt`로만 바뀐다
- `.claude/settings.json`, `CLAUDE.md`, `.gemini/settings.json`, 이 파일(`AGENTS.md`)

`loop/prompts/*.md`는 고쳐도 된다 (8주차 goal 변경). `loop/run.py`에 노드를 더하는 것(README §6의 B)은 사람이 직접 하고 "전제 조건"에 적는다.

## 고쳐도 되는 파일

- `hooks/*.py` — 매주 발제 method를 여기에 구현한다. 함수 시그니처는 유지한다. 조절 가능한 값은 `KNOBS`로 선언한다 (README §7)
- `config.yaml` — `week`, `hook_changed`, `editable_blocks`
- `methods/` 아래 새 모듈 (README §6의 A)
- `loop/prompts/*.md`

## 주차 시작 절차 (사용자가 "N주차 시작"이라고 하면 이 순서로 진행한다)

1. 지난 주 작업을 먼저 commit한다: `git add -A && git commit -m "week <N-1>: <method>"` (바뀐 게 없으면 건너뜀).
   그다음 `git pull --no-rebase --no-edit`. 충돌이 나면 `hooks/`·`methods/`·`config.yaml`·`sql_agent/run.py`는 내 것을 유지한다 (`git checkout --ours <파일> && git add <파일>`), 그 밖은 origin 것을 쓴다.
   `hooks/`·`methods/` 파일을 내 것으로 유지했으면, 그 전에 운영자가 그 파일에서 바꾼 부분(`git diff HEAD...MERGE_HEAD -- <파일>`)을 사용자에게 보여 주고 내 코드에 합칠지 묻는다. 묻지 않고 넘어가면 운영자의 수정이 빠진다.
   데이터나 채점이 바뀌었으면 loop가 이전 기록을 `logs/old-<sha>/`로 옮긴다 (손댈 것 없음).
2. `config.yaml`: `week: N`, `hook_changed`에 바꾼 hook 이름과 method 한 줄.
   - N ≥ 4이면 `editable_blocks: [prompt, code]` (이후 주차도 유지). 그 전에는 `[prompt]`.
   - 6주차도 블록은 그대로다.
3. 사용자가 준 method(주차 페이지 발제 노트 7번)를 `hooks/`에 구현한다. 함수 시그니처는 유지한다.
   - 조절할 값은 `KNOBS`와 `KNOB_DOCS`로 선언한다 (README §7). `diagnose`·`build_prompt`·`propose` hook의 값만 선언하고, 다른 hook의 값은 hook 안 상수로 둔다. 지난 주 hook과 같은 파일이면 합쳐서 둘 다 살린다.
   - 6주차(자기 평가)는 `hooks/review_patch.py`나 `hooks/after_screen.py`에 넣는다. 참고 구현: `examples/`.
   - 어느 블록·오답 유형을 겨냥할지는 `hooks/on_round_start.py`가 자기 기록으로 고른다. 따로 정하지 않는다.
4. `make check` (LLM 호출 없음). 실패하면 고친다.
5. `make round` → `make test` → `make results W=N`. 결과 표 한 줄(README §8 7칸)의 초안을 사용자에게 보여 준다.
6. 사용자가 원하면 `make meta-propose` → `make meta-eval`. 채택(`make meta-adopt`)은 사용자에게 묻고 한다.
7. 8주차에는 추가로 `make meta-eval FILE=knobs/v0.py SEED=9000` (처음 방법과 지금 방법 비교) 결과를 결과 표 7번째 칸에 적는다.
8. 끝나면 이번 주 hook을 commit한다: `git add -A && git commit -m "week N: <method>"`.

## 그 밖의 규칙

- `data/test.json`과 test 예측 파일은 열지 않는다. test 점수를 보고 method를 고르지 않는다.
- 셸 명령으로 고정 파일을 바꾸는 것(`sed -i`, 리다이렉션 등)도 같은 규칙이다.
- 고정 파일이 바뀌면 `make round`·`make test`가 origin의 `ruler-vN` tag(`loop/kernel.py`의 `RULER_TAG`)와 비교해 실행을 거부한다. 일부러 바꾼 경우에만 `RULER_OVERRIDE=1`로 돌리고, 그 사실이 로그와 결과에 남는다.
