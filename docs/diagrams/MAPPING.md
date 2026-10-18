# 그림 원본

그림은 [archify](https://github.com/tt-a1i/archify)로 그린다. 원본은 이 폴더의 JSON, 결과 PNG는 `docs/`에 있다.

| 원본 | PNG | 쓰는 곳 |
|---|---|---|
| `structure.json` | `docs/structure.png` | README 맨 위 (세 층, loop 안·밖), Notion OT |
| `sql_agent.json` | `docs/sql_agent.png` | README 맨 위 (SQL agent graph) |
| `round.json` | `docs/round.png` | README 3 (한 round) |
| `week.json` | `docs/week.png` | 위키 주차별-로드맵 (한 주) |
| `loop_inout.json` | `docs/loop_inout.png` | Notion OT "Abstract 풀어 읽기" 5번 (평가자는 loop 안, 측정은 loop 바깥) |

다시 그리기: `node <archify>/bin/archify.mjs finalize <architecture|workflow> docs/diagrams/X.json out.html --repo-root . --quality showcase` 뒤 HTML의 그림을 PNG로 저장한다.
