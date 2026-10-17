# 논문 메소드 예시 (운영자 실측)

3~8주차 후보 논문에서 고른 메소드를 hook으로 구현해 실제로 돌려 본 예시입니다.
발제자가 method를 제안할 때, 참가자가 hook을 고칠 때 참고합니다. **정답이 아닙니다.** 각자 다르게 구현해도 됩니다.

- 조건: 교체 전 무작위 데이터(v0 validation 72, test 139) 위, SQL agent·Improver LLM 모두 gpt-4.1-nano, 8 round, 1회 측정.
- validation sd ~1, test 1회 sd ~1.5. 아래 test 숫자는 1회라 메소드 효과로 단정할 수 없습니다.
- 4·5주차 예시는 아직 없습니다 (측정 중).

## 쓰는 법

폴더의 `hooks/`, `methods/`를 repo 루트에 덮어씁니다. `config.yaml`의 `week`, `hook_changed`도 바꿉니다.

```bash
cp -r examples/w8_safety_ledger/hooks examples/w8_safety_ledger/methods .
make round N=20
```

이전 주 hook과 같은 파일을 고친 예시(예: 3주차와 7주차의 `build_prompt.py`)는 그대로 덮으면 앞 주 method가 사라집니다. 합쳐서 씁니다.

## 요약

| 주차 | 폴더 | 논문 | 바꾼 hook | 8 round | test (교체 전 데이터, v0 139) | 판정 |
|---|---|---|---|---|---|---|
| 3 | `w3_self_refine` | Self-Refine (2303.17651) | `propose`, `build_prompt` | accept 1/8, 73→77 | 146 | 조건부 |
| 6 | `w6_judge_screen_gain` | LLM-as-a-Judge (2306.05685) + RM overoptimization (2210.10760) | `review_patch`, `after_screen` | accept 1/8, 71→76 | 148 | judge 실패 · screen_gain 유효 |
| 7 | `w7_funsearch_bestshot` | FunSearch (Nature 2024) | `build_prompt` + `methods/program_db.py` | accept 0/8, 72 | — | 실패 사례 |
| 8 | `w8_safety_ledger` | The unfireable safety kernel (2606.26057) | `propose`, `after_eval`, `should_stop` + `methods/safety_ledger.py` | accept 1/8, 71→75 | 144 | 적용 가능 (점수 중립) |

공통: 탈락은 전부 kernel의 문턱(바뀐 답 40~74개라 net≥4 필요)에서 났습니다. nano Improver는 대부분 SYSTEM_PROMPT 한 문장만 고칩니다.

## 3주차 Self-Refine

- 초안 patch → 같은 LLM이 줄 단위 피드백(VERDICT) → 다시 씀. 파라미터(KNOBS) `refine_iters`(0~3, 기본 1), `critic_focus`(문장).
- 비용: round당 LLM +2 × `refine_iters`.
- 실측에서 본 문제와 이 예시에 넣은 수정:
  - 피드백 9회 중 3회가 `VERDICT:` 줄을 빠뜨렸고, OK로 처리돼 초안이 그대로 나갔습니다. 채택된 patch도 초안이었습니다. → VERDICT가 없으면 수정으로 봅니다.
  - "3문장 이내로" 같은 비평 기준이 SYSTEM_PROMPT에 그대로 들어갔습니다. → 비평이 이 누출을 잡게 했습니다.
- 효과는 비평 루프보다 `critic_focus`를 patch 작성자에게 미리 알려 주는 한 줄에서 나왔을 가능성이 큽니다. 같은 코드가 `critic_focus`만 느슨했을 때 test 133(−6)이었습니다.
- 확인 방법: `make meta-eval`로 `refine_iters` 0 vs 1을 짝 비교합니다. 그 전에는 "Self-Refine 효과 있음"으로 적지 않습니다.

## 6주차 LLM-as-a-Judge + screen_gain

- `review_patch`: judge가 patch에 SCORE 1~10. `judge_min_score`(기본 7) 미만이면 평가 전에 버립니다. `judge_mode` single/pairwise.
- `after_screen`: `screen_gain`(0~5, 기본 1). 20문제에서 그만큼 오르지 않으면 100문제 평가를 하지 않습니다.
- 결과
  - judge는 **실패**: 8개 patch 모두 8~9점, kernel이 거부한 7개도 전부 통과. 이전 round의 재표현이라고 알려 줘도 9점. 논문이 보고한 verbosity bias·제한된 추론 능력과 같은 모습입니다. 남은 쓸모는 검증 문제를 그대로 박아 넣은 overfit patch(6점)를 거르는 정도입니다.
  - `screen_gain=1`은 **유효**: 손실 patch 6개를 20문제에서 끊어 약 600 호출을 아꼈고 승자 patch는 통과했습니다. 단 20문제의 ±1 noise 때문에 진짜 +4 patch를 놓칠 확률이 약 17%입니다.
- judge 점수를 kernel 결과 예측이나 후보 순위에 쓰지 않습니다.

## 7주차 FunSearch best-shot (실패 사례)

- archive에서 같은 블록의 이전 버전 k개를 점수 오름차순으로 보여 주고 "다음 버전 v_k를 써라"라고 요청합니다. 파라미터 `n_shots`(1~4, 기본 2), `shot_temp`.
- 메커니즘은 동작했지만(이전 버전 문구가 patch에 다시 나옴) 점수 차가 없는 버전들(전부 72)이라 방향 신호가 없었습니다.
- `n_shots=4`, `shot_temp=0.5`로 다시 돌리면 이전 버전을 복사하는 쪽으로 수렴해 "already tried"로 평가 전에 거부됐습니다.
- 정보를 더 보여 주는 것만으로는 nano Improver가 다른 종류의 수를 내지 못합니다. 쓰려면 점수 차가 큰 버전을 강제로 넣거나, EoH처럼 변형 연산자(지시문 · few-shot · 메시지 구조)를 지정해 줍니다.

## 8주차 safety ledger

- `propose`: patch마다 kernel의 AST guard를 미리 돌려 ALLOW/DENY를 `logs/kernel_ledger.jsonl`에 해시 체인으로 남깁니다.
- `after_eval`: round 증거 한 줄. screen은 오르는데 full은 안 오르면 proxy 괴리로 표시(`proxy_margin`, 기본 2).
- `should_stop`: 체인이 깨지거나, 주간 LLM 호출이 `CALL_BUDGET`(기본 2500)을 넘거나, 규칙 위반 시도가 `GUARD_CEILING`(기본 10)을 넘거나, `stall_rounds` 연속 거부면 멈춥니다. 상한 두 개는 Improver가 못 올리도록 파라미터가 아니라 `.env`로 둡니다.
- 결과: 위반 시도 0, kill switch 미발동, proxy 괴리 1회 포착. 추가 LLM 호출 0. 점수는 중립입니다.
- nano는 `prompt` 블록만 열면 금지된 코드를 쓰지 않아 "규칙 위반 시도 수"가 0으로 나옵니다. 차이를 보려면 `editable_blocks: [prompt, code]`로 엽니다.
- 한계: ledger 파일 삭제는 못 막습니다. 판정이 round 끝에 나므로 예산을 최대 1 round 넘을 수 있습니다.

## 기본 hook으로 올리지 않은 이유

- 기본 hook을 바꾸면 v0가 바뀌고, 그 주차 발제자가 할 실험을 미리 정해 버립니다.
- `screen_gain`을 파라미터로만 두는 것도 검토했지만, `make meta-eval`은 Improver의 patch 제안만 비교하므로 평가 단계 파라미터는 판정이 항상 NULL입니다. 파라미터로 두어도 Improver가 다듬을 수 없습니다.
