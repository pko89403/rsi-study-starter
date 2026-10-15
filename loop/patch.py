"""EVOLVE-BLOCK 읽기·교체와 guard. loop가 고정한다."""
from __future__ import annotations
import json, re, subprocess, sys
from dataclasses import dataclass
from pathlib import Path

START = "# === EVOLVE-BLOCK-START: {name} ==="
END = "# === EVOLVE-BLOCK-END ==="
FORBIDDEN = ["test.json", "gold_sql", "rounds.jsonl", "os.environ[", "subprocess", "import os", "importlib", "__import__", "open("]


@dataclass
class Patch:
    block: str
    content: str
    reason: str


def clean_content(content: str) -> str:
    """LLM이 섞어 넣기 쉬운 것 제거: ``` 펜스, EVOLVE-BLOCK 표시 줄."""
    content = re.sub(r"^\s*```[a-zA-Z]*\s*\n", "", content)
    content = re.sub(r"\n```\s*$", "", content)
    lines = [l for l in content.splitlines() if "EVOLVE-BLOCK" not in l]
    return "\n".join(lines).strip("\n")


def parse_patch(text: str) -> Patch | None:
    """구분자 형식을 먼저, 없으면 JSON. 코드 여러 줄을 JSON 문자열에 넣으면 소형 모델이 줄바꿈을 잃는다.
    BLOCK: <이름>
    REASON: <한 줄>
    <<<CONTENT
    ...블록 내용...
    CONTENT>>>
    """
    m = re.search(r"BLOCK:\s*(\S+).*?REASON:\s*(.*?)\n.*?<<<CONTENT\n(.*?)\nCONTENT>>>", text, re.S)
    if m:
        return Patch(block=m.group(1).strip(), content=clean_content(m.group(3)), reason=m.group(2).strip()[:200])
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        o = json.loads(m.group(0))
        return Patch(block=str(o["block"]), content=clean_content(str(o["content"])), reason=str(o.get("reason", ""))[:200])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def find_block(src: str, name: str) -> tuple[int, int] | None:
    s = src.find(START.format(name=name))
    if s < 0:
        return None
    s = src.index("\n", s) + 1
    e = src.find(END, s)
    return (s, e) if e >= 0 else None


def read_block(path: Path, name: str) -> str | None:
    src = path.read_text()
    se = find_block(src, name)
    return src[se[0]:se[1]].rstrip("\n") if se else None


def locate(files: list[str], name: str) -> Path | None:
    for f in files:
        if find_block(Path(f).read_text(), name):
            return Path(f)
    return None


def guard(patch: Patch, editable_blocks: list[str]) -> str | None:
    """거부 사유 문자열, 통과면 None."""
    if patch.block not in editable_blocks:
        return f"block '{patch.block}' not open"
    for bad in FORBIDDEN:
        if bad in patch.content:
            return f"forbidden string: {bad}"
    if not patch.content.strip():
        return "empty content"
    return None


def apply(path: Path, name: str, content: str) -> None:
    src = path.read_text()
    s, e = find_block(src, name)
    path.write_text(src[:s] + content.rstrip("\n") + "\n" + src[e:])


def outside_changed(before: str, after: str, name: str) -> bool:
    def strip(src: str) -> str:
        s, e = find_block(src, name)
        return src[:s] + src[e:]
    return strip(before) != strip(after)


def compiles(module: str) -> str | None:
    r = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=60)
    return None if r.returncode == 0 else (r.stderr.strip().splitlines() or ["import failed"])[-1]
