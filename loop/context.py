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
    block: str                        # open_blocks[0]의 현재 내용
    before: dict                      # evaluate() 결과
    problems: dict                    # id -> problem (gold 포함, loop만 봄)
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

    def problem(self, pid: str) -> dict:
        return self.problems[pid]

    def prediction(self, pid: str) -> str:
        return self.predictions.get(pid, "")

    def prompt_text(self, name: str) -> str:
        return (self.prompts_dir / name).read_text()
