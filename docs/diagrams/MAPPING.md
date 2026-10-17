# 그림 교체 표

새 그림의 원본은 이 폴더의 `.mmd`다. PNG는 `npx -y @mermaid-js/mermaid-cli -i X.mmd -o X.png`로 만든다.

| 새 원본 | 내용 |
|---|---|
| `layers.mmd` | 세 층. 참가자 → hooks, Improver agent → EVOLVE-BLOCK, 파라미터(KNOBS) 수정안과 채택, 고정 kernel 판정, loop 밖 make test |
| `sql_agent.mmd` | SQL agent graph. select_schema → generate → execute → route → fix / END, EVOLVE-BLOCK 구분, MAX_FIX 5 |
| `round.mmd` | 한 round. `loop/run.py` build_graph의 노드 순서와 거부 경로 |
| `week.mmd` | 한 주. 발제 → hooks 구현 → make check → round → test → results → 결과 표, 선택: meta |
| `measure.mmd` | loop 안(validation 채점, kernel 판정)과 loop 밖(make test) |

## 바꿀 그림

| 위치 | 지금 그림 | 바꿀 원본 | 이유 |
|---|---|---|---|
| README.md 15행 | `docs/components.png` (세 층) | `layers.mmd` (+ `sql_agent.mmd`) | 옛 모델 이름, 옛 accept 기준(+3), 목표를 실패 관찰로 적음 |
| README.md 75행 | `docs/structure.png` (구성도·경계) | `sql_agent.mmd` + `measure.mmd` | 옛 모델 이름, evaluator 블록을 patch 대상으로 적음 |
| README.md 133행 | `docs/round.png` (한 round) | `round.mmd` | build_graph 노드 순서와 맞춤 |
| 위키 `주차별-로드맵.md` 10행 | `docs/week.png` (github raw 링크) | `week.mmd` | make check·results·meta 단계 반영 |
| Notion OT "Text-to-SQL agent 개선 프로젝트" | `f0_layers_v7.png` (세 층) | `layers.mmd` | 같은 그림을 한 원본에서 |
| Notion OT "Abstract 풀어 읽기" 5번 | `loop_inout_v2.png` (loop 안·밖) | `measure.mmd` | 같은 그림을 한 원본에서 |

교체 뒤 `docs/components.png`, `docs/structure.png`, `docs/round.png`, `docs/week.png`는 지운다 (위키 링크를 먼저 바꾼다).

## 남길 그림 (논문 그림·세션 진행)

| 위치 | 그림 | 종류 |
|---|---|---|
| Notion OT "Abstract 풀어 읽기" 2번 | `rsi_figure1_ko_v2.png` | RSI Survey Figure 1 (한국어 재구성) |
| Notion OT "Abstract 풀어 읽기" 4번 | `verification_hierarchy_ko.png` | RSI Survey Figure 5 (한국어 재구성) |
| Notion OT "Abstract 풀어 읽기" 6번 | `fig1_starter_v2.png` | Figure 1 위의 starter 위치 |
| Notion OT "원문 보기" | `rsi_abstract_p1.png` | 논문 Abstract 원문 |
| Notion OT "진행 방식" | `session_100min_flow.png` | 세션 100분 흐름 |

위키의 나머지 페이지와 `examples/README.md`에는 그림이 없다.
