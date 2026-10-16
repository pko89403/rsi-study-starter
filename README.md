# Text-to-SQL starter (RSI 논문 스터디)

각자 두 가지를 가지고 시작한다.

- **SQL agent**: 질문을 받아 SQL을 낸다. 개선 대상이다.
- **Improver**: SQL agent를 고치는 장치다. loop + Improver LLM + hook 10개로 이루어진다.

매주 발제 논문의 method를 hook 하나에 넣어 돌려 보고, 같은 양식으로 경험을 적는다.
이 repo는 그 최소 형태다.

설계 이유, 데이터 형식, 주차별 로드맵 같은 배경은 [위키](https://github.com/pko89403/rsi-study-starter/wiki)에 있다.

![세 층: 참가자가 Improver agent의 hook을 고치고, Improver agent가 SQL agent의 EVOLVE-BLOCK을 고친다](docs/components.png)

**EVOLVE-BLOCK**은 Improver agent가 고칠 수 있는 유일한 코드 구간이다.

- `# === EVOLVE-BLOCK-START: <이름> ===` ~ `# === EVOLVE-BLOCK-END ===` 주석으로 감싼다.
- 세 개가 있다: `prompt`, `code`(`sql_agent/run.py`), `evaluator`(`evaluator/rules.py`).
- `config.yaml`의 `editable_blocks`에 적은 것만 이번 주에 수정할 수 있다. 기본은 `[prompt]`.
- 표기는 AlphaEvolve(DeepMind, 2025)의 `EVOLVE-BLOCK` 주석을 따랐다.

## 1. 10분 안에

**설치**

```bash
# uv 없으면: brew install uv  (또는 curl -LsSf https://astral.sh/uv/install.sh | sh)
make install                 # uv sync: .venv(Python 3.11) + pyproject.toml/uv.lock 의존성
cp .env.example .env         # 열어서 아래 값을 채운다
```

**`.env`** (기본: OpenRouter. 전원 같은 모델·같은 provider)

```bash
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=<OpenRouter 키>
LLM_MODEL=qwen/qwen-2.5-7b-instruct
LLM_PROVIDER=Phala           # provider 고정. 양자화가 같아야 점수를 나란히 놓을 수 있다
LLM_CONCURRENCY=8            # 병렬 호출 수
```

로컬 LM Studio로 쓰려면 위키 [로컬 LM Studio](https://github.com/pko89403/rsi-study-starter/wiki/로컬-LM-Studio)를 따른다.

**실행**

```bash
make test                    # v0 test score (200문제 × 3회 평균)
make round                   # 20 round. N=1 이면 1 round. logs/rounds.jsonl 에 round당 한 줄
make results W=1             # 그 주의 결과 표
```

- `make` 없이 돌리려면 `uv run`을 붙인다. 예: `uv run python -m scripts.test_eval --runs 3`. venv activate는 필요 없다.
- LLM 없이 loop 동작만 보려면 `LLM_BACKEND=mock`을 붙인다. 점수는 의미 없다.
- 데이터(`data/`)는 repo에 들어 있는 것을 그대로 쓴다.
  - validation 100 (Spider train)
  - test 200 (Spider dev)
  - few-shot pool 300
  - SQLite DB 25개
  - Spider 라이선스는 CC BY-SA 4.0.

## 2. 폴더

![구성도: Improver agent와 SQL agent의 경계. 경계를 넘는 것은 실행 요청과 patch(EVOLVE-BLOCK 교체)뿐이고, 결과는 evaluator를 거쳐 돌아온다. test는 loop 밖](docs/structure.png)

| 경로 | 하는 일 | 누가 고치나 |
|---|---|---|
| `sql_agent/run.py` | SQL agent (LangGraph, 아래 2-1) | Improver agent (`editable_blocks`에 적힌 EVOLVE-BLOCK만) |
| `sql_agent/llm.py`, `schema.py` | LLM 호출, 스키마 텍스트 | 사람 |
| `evaluator/evaluate.py` | SQL 실행 결과를 정답과 비교 | 고정 |
| `evaluator/rules.py` | validation 채점 규칙. `evaluator` EVOLVE-BLOCK | Improver agent (6주차에 `editable_blocks`에 추가한 뒤) |
| `evaluator/rules_v0.py` | test 채점 규칙 (고정 복사본) | 고정 |
| `loop/run.py` | round loop. LangGraph `StateGraph` (아래 3) | 고정 (B는 예외, 아래 6) |
| `loop/patch.py` | EVOLVE-BLOCK 교체, guard | 고정 |
| `loop/archive.py` | git commit/checkout, 최고 commit 추적 | 고정 |
| `loop/prompts/` | 진단·patch 프롬프트 | 사람 (8주차에 goal 바꾸기) |
| `hooks/*.py` | 결정 지점 10개. **매주 method는 이 중 하나의 본문을 바꾼다** | 각자 |
| `data/test.json` | test. loop는 열지 않는다 | 아무도 |
| `logs/rounds.jsonl` | round당 한 줄 | loop |
| `scripts/` | test 평가, 결과 표, 분할 생성(운영자), Improver LLM 후보 측정(`probe_code_block`) | — |

### 2-1. SQL agent의 모양

문제 하나를 graph 한 번으로 푼다. 배선은 고정이고, 노드 본문만 EVOLVE-BLOCK이다.

```
START → select_schema → generate → execute → route ─┬─ "fix" → fix → execute (다시)
                                                    └─ END
```

| 노드 | 하는 일 | v0 동작 | 어디에 있나 |
|---|---|---|---|
| `select_schema` | 질문과 관련된 테이블만 남긴다 | 전체 스키마 그대로 | `code` 블록 |
| `generate` | 스키마 + few-shot → SQL | LLM 1회 | `code` 블록 (메시지는 `prompt` 블록) |
| `execute` | SQL을 읽기 전용으로 실행 (5초 상한) | 실행함 | 고정 |
| `route` | END로 갈지 fix로 갈지 | 항상 END (재시도 없음) | `code` 블록 |
| `fix` | 오류를 보여 주고 SQL을 다시 받는다 | 안 불림 | `code` 블록 |

- v0는 문제당 LLM 1회로 이전과 같은 점수를 낸다.
- `route`가 무엇을 내든 fix는 문제당 5회(`MAX_FIX`, 블록 밖)를 넘지 않는다.
- 블록이 노드 이름을 빼먹으면 import 단계에서 실패하므로 guard가 거부한다.

## 3. 한 round와 hook 10개

loop는 round마다 아래 순서로 돌고, 번호 붙은 곳에서 `hooks/`의 함수를 부른다.
함수 이름·인자·반환은 고정이고 본문만 바꾼다.

코드로는 LangGraph `StateGraph` 하나다.

- 노드: `begin → diagnose → propose → review → apply → screen → full → after_eval`
- 거부되면 조건부 엣지로 바로 `after_eval`로 간다.
- `should_stop`이 False면 다음 round의 `begin`으로 돌아간다.
- hook은 노드 안에서 불리므로, hook만 바꾸는 사람은 LangGraph를 몰라도 된다.

![한 round의 흐름과 hook 10개의 위치](docs/round.png)

```
parent = select_parent(archive)                 ① 어느 commit에서 시작하나        기본: 최고 점수 commit
checkout → before = 실행·평가(validation 100)      같은 commit은 다시 재지 않음
on_round_start(ctx)                             ② round 시작 직후 할 일            기본: 없음
ctx.diagnosis = diagnose(ctx)                   ③ 틀린 문제 → 원인 한 줄           기본: LLM 1회 (diagnose.md)
patches = propose(ctx, build_prompt(ctx))       ④ 무엇을 보여 주고 ⑤ 후보를 몇 개   기본: LLM 1회, patch 1개
for patch in patches:
    patch = review_patch(ctx, patch)            ⑥ 적용 전 검토·수정·거부           기본: 그대로. None → rejected_by=review
    guard → EVOLVE-BLOCK 교체 → compile 검사 → commit
    screen(20문제)
    after_screen(ctx, before20, after20)        ⑦ full로 갈지                     기본: screen_drop(2) 이상 떨어지면 중단
    full(100문제) → after
    accept(ctx, before, after, patch)           ⑧ 남길지                          기본: noise_margin(3) 이상 오르면 새 최고
after_eval(ctx)                                 ⑨ round 뒤 기록·축적               기본: 없음
rounds.jsonl 한 줄
should_stop(ctx)                                ⑩ 일찍 멈출지                      기본: False
```

- rollback은 없다. 모든 patch가 commit으로 남고, 다음 round는 `select_parent`가 고른 commit에서 시작한다.
- 거부 사유(`rejected_by`):
  - `guard`: EVOLVE-BLOCK 밖 수정, `editable_blocks`에 없는 블록, 금지 문자열, compile 실패, 변경 없음
  - `review`, `screen`, `accept`, `error`
- Improver LLM 출력 상한은 `config.yaml`의 `improver_max_tokens`(기본 4096)다.
  - 7B의 정상 patch는 300~600토큰이다.
  - 추론 모델(Qwen3, gpt-oss)을 쓰면 생각 토큰도 여기서 쓰므로 8192~16384로 올린다.

## 4. method를 넣는 법

1. `hooks/` 안의 함수 하나(또는 둘)의 본문을 바꾼다. 시그니처는 그대로 둔다.
   hook에 안 들어가는 method는 아래 6의 B·C를 따른다.
2. `config.yaml`의 `hook_changed`에 바꾼 hook 이름을, `week`에 주차를 적는다.
3. `make round`
4. `make test`, `make results W=N`으로 결과 표를 채운다.

hook 안에서 할 수 있는 것:

- `ctx` 읽기
- `logs/` 아래 파일 읽고 쓰기
- `ctx.llm(messages, role="improver")`로 LLM 추가 호출 (호출 수는 결과 표 "비용"에 적는다)

하면 안 되는 것:

- `data/test.json` 열기
- `sql_agent/run.py`의 EVOLVE-BLOCK을 사람이 고치기 (SQL agent 변경은 patch 경로로만)
- `loop/run.py` 밖의 `loop/`·`evaluator/` 고치기

loop는 hook을 검사하지 않는다. 규칙으로 지킨다.

예: Reflexion

- `after_eval`이 거부된 patch의 이유를 `logs/playbook.md`에 적는다.
- `build_prompt`가 그 파일을 읽어 prompt에 넣는다.

`RoundContext`(`loop/context.py`)에 들어 있는 것:
round, parent, 현재 EVOLVE-BLOCK 내용, before 평가 결과(문제별 정오·error), 문제·예측 조회, 진단, screen·after 결과, patch, accepted, 지난 log 5줄, `ctx.llm(...)`.

## 5. 주차별로 `editable_blocks`에 추가하는 것

`config.yaml`의 `editable_blocks`에 블록 이름을 더하면 Improver agent가 그 블록을 고칠 수 있다.

주차별 예와 4주차 `code` 블록 예(노드 본문·`route`)는 위키 [주차별 로드맵](https://github.com/pko89403/rsi-study-starter/wiki/주차별-로드맵)에 있다.

## 6. 고정인 것과 자유인 것

전원이 같게 유지하는 것은 아래 **평가 조건 5개**다.
이것만 지키면 사람마다 다른 Improver agent를 같은 표에 놓을 수 있다.

1. 데이터: `data/validation.json`·`test.json`·`fewshot_pool.json` (6주차 전까지)
2. test 채점: `scripts/test_eval.py` + `evaluator/rules_v0.py`
3. 모델: Qwen2.5-7B-Instruct, temperature 0
4. test 결과를 loop의 입력으로 쓰지 않는다
5. `logs/rounds.jsonl` 한 줄의 필드 (`scripts/results.py`가 읽는다)

나머지는 자유다. 1~5번은 코드가 막지 않는다. 규칙으로 지킨다.

method가 hook 하나에 들어가면 그 함수만 바꾼다. 안 들어가면 아래 넷 중 하나다.

**A. 새 모듈을 hook에서 부름** (대부분)

- 예: few-shot을 유사도로 고르는 retriever, playbook 관리 클래스
- 하는 법: `methods/retriever.py` 같은 파일을 만들고 `hooks/build_prompt.py`에서 import
- 기록: `hook_changed`에 hook 이름. "전제 조건"에 파일명

**B. loop에 단계가 하나 더 필요** (가끔)

- 예: screen과 full 사이의 LLM 검증자, patch 두 개를 짝지어 비교, parent 여러 개를 두는 population
- 하는 법: `loop/run.py`의 graph에 노드·엣지 추가. `rounds.jsonl` 필드는 지우거나 이름을 바꾸지 않는다(추가는 된다)
- 기록: "전제 조건"에 "`loop/run.py`에 X 노드 추가"

**C. SQL agent에 새 부품이 필요** (드묾)

- 예: 스키마 검색기, 새 도구(`explain_sql` 같은 것)
- 하는 법:
  - `sql_agent/tools.py`처럼 EVOLVE-BLOCK 밖에 helper를 만든다.
  - `sql_agent/run.py` 위쪽에 import 한 줄을 넣는다.
  - `hooks/build_prompt.py`의 `CONTRACTS["code"]` 이름 목록에 등록한다.
  - Improver LLM이 그것을 쓰는 patch를 내게 한다.
- 기록: SQL agent 기준선이 바뀌므로 "전" 점수를 다시 잰다. "전제 조건"에 적는다.

**D. 평가 조건 5개를 바꿔야 함**

- 예: validation 문제 교체, test 채점 규칙 변경, 모델 교체, `rounds.jsonl` 필드 삭제
- 안 된다. 그 주 표에 다른 사람과 나란히 놓을 수 없다.
- 하고 싶으면 표 밖에 "참고 실험"으로 따로 적는다.

참고:

- **C의 선**: 사람이 SQL agent를 고치는 것과 Improver agent가 고치는 것을 섞지 않는다.
  - helper 추가(사람)는 SQL agent 기준선을 바꾸는 변경이다. "전" 점수 재측정으로 분리한다.
  - 그 helper를 노드 본문에서 쓰는 것(Improver agent)이 method 효과다. 표의 전/후 차이로 읽는다.
  - helper를 추가하면서 `code` 블록 본문까지 사람이 바꾸면 이 분리가 깨진다.
  - 배선(`make_graph`)을 사람이 바꾸는 것도 C다.
- **hook이 못 하는 것**: 단계 사이에 끼어들기, 단계 순서 바꾸기, 여러 SQL agent를 동시에 유지하기(population), SQL agent가 쓸 수 있는 이름 늘리기. 이것들이 B·C다.
- **가중치를 학습하는 method**(self-reward, STaR 등 Training-time)는 모델이 고정이라 그대로는 안 된다.
  in-context 버전(맞춘 예시를 pool에 쌓기, 문제를 만드는 노드)으로 바꿔 체험하고 "전제 조건"에 적는다.

## 7. 결과 표

매주 같은 6칸을 적는다.

- validation 전/후
- test 전/후
- 비용
- 실패 유형
- 전제 조건
- 서비스에 넣는다면

칸별 설명과 읽는 법은 위키 [결과 표](https://github.com/pko89403/rsi-study-starter/wiki/결과-표)에 있다.

## 8. 자주 막히는 곳

- **LM Studio**: 모델을 context 16384로 로드한다(`lms load <모델> -c 16384`).
  기본 context(8192)면 Improver LLM 프롬프트와 출력이 넘칠 수 있다. 자세한 것은 위키 [로컬 LM Studio](https://github.com/pko89403/rsi-study-starter/wiki/로컬-LM-Studio).
- **agent timeout**: `config.yaml`의 `agent_timeout_sec`(기본 1800초). 100문제 × 모델 속도.
- **round 하나가 몇 분씩 걸림**: Improver LLM이 같은 내용을 반복하다 `improver_max_tokens`까지 쓰는 경우다. 7B에서 가끔 생긴다.
  그 patch는 parse 실패로 거부된다. 상한을 올려도 결과는 같고 시간만 늘어난다.
- **점수가 전혀 안 움직임**: 7B Improver LLM에서는 흔하다. 거부가 많은 것이 정상이다.
  Improver LLM 교체는 평가 조건 3을 바꾸는 일이라 전원이 함께 정한다.
