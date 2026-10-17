from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class RoundContext:
    round: int
    week: int
    config: dict
    parent: str
    block_name: str
    block: str                        # 이번 round 대상 블록(block_name)의 현재 내용
    before: dict                      # evaluate() 결과
    problems: dict                    # id -> problem (gold_sql 없음. 채점은 kernel만 한다)
    predictions: dict                 # id -> sql (before 실행)
    blocks: dict | None = None        # 열린 블록 전부 {이름: 내용}
    diagnosis: str = ""
    recent_log: list = field(default_factory=list)
    llm: Callable = None              # llm(messages, role=..., ...) -> str
    prompts_dir: Path = Path("loop/prompts")
    screen: dict | None = None          # screen(20문제) evaluate() 결과
    after: dict | None = None
    patch: object = None
    accepted: bool | None = None
    improver: dict = field(default_factory=dict)   # IMPROVER 파라미터(KNOBS) 값 (hook이 KNOBS로 선언, kernel이 검사한 값)
    seed: int = 0                                  # 오답 표본 순서 등 입력을 섞는 seed (loop: round 번호)
    llm_calls: int = 0                             # 이 ctx.llm으로 부른 횟수 (hook의 추가 호출 포함, 캐시 응답도 센다)

    def __post_init__(self):
        if self.llm:
            raw = self.llm

            def counted(*a, **kw):
                self.llm_calls += 1
                return raw(*a, **kw)
            self.llm = counted

    def problem(self, pid: str) -> dict:
        return self.problems[pid]

    def prediction(self, pid: str) -> str:
        return self.predictions.get(pid, "")

    def prompt_text(self, name: str) -> str:
        return (self.prompts_dir / name).read_text()
