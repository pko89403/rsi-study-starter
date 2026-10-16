"""EVOLVE-BLOCK 읽기·교체와 guard. loop가 고정한다."""
from __future__ import annotations
import ast, json, re, subprocess, sys
from dataclasses import dataclass
from pathlib import Path

START = "# === EVOLVE-BLOCK-START: {name} ==="
END = "# === EVOLVE-BLOCK-END ==="
# 블록 코드에서 쓸 수 없는 이름 (파일·프로세스·동적 실행·모듈 밖 데이터 접근). 문자열 검사가 아니라 AST로 본다
DENY_NAMES = {"open", "exec", "eval", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals", "vars",
              "input", "breakpoint", "os", "sys", "subprocess", "sqlite3", "Path", "DATA_DIR", "importlib", "builtins",
              "shutil", "socket", "llm", "providers"}
DENY_STRINGS = ["test.json", "validation.json", "fewshot_pool.json", "rounds.jsonl", "llm_cache", ".sqlite", "gold_sql"]


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
    # 닫는 줄은 모델마다 다르게 쓴다: CONTENT>>>, >>>, <<<CONTENT>>>, <<<END_CONTENT>>> ... → <<<·대문자·_·>>> 만으로 된 줄이면 닫는 줄. 없으면(잘림) 거부
    m = re.search(r"BLOCK:\s*(\S+).*?REASON:\s*(.*?)\n.*?<<<CONTENT[ \t]*\n(.*?)\n[ \t]*(?=[<>A-Z_]*(?:<<<|>>>))[<>A-Z_]+[ \t]*(?:\n|$)", text, re.S)
    if m:
        return Patch(block=m.group(1).strip().strip("'\"`"), content=clean_content(m.group(3)), reason=m.group(2).strip()[:200])
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
    """거부 사유 문자열, 통과면 None. 판단할 수 없으면 거부한다 (fail closed)."""
    if patch.block not in editable_blocks:
        return f"block '{patch.block}' not open"
    if not patch.content.strip():
        return "empty content"
    try:
        tree = ast.parse(patch.content)
    except SyntaxError as e:
        return f"syntax error: {e.msg} (line {e.lineno})"
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            return "import is not allowed inside a block"
        if isinstance(n, ast.Name) and n.id in DENY_NAMES:
            return f"forbidden name: {n.id}"
        if isinstance(n, ast.Attribute) and n.attr.startswith("__"):
            return f"forbidden attribute: {n.attr}"
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            bad = next((d for d in DENY_STRINGS if d in n.value), None)
            if bad:
                return f"forbidden string: {bad}"
    return None


def _names(src: str) -> dict[str, tuple[int, int]]:
    """top-level 이름 -> (시작 줄, 끝 줄) 0-based, 끝 미포함. decorator 포함."""
    out = {}
    for n in ast.parse(src).body:
        s = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])]) - 1
        for t in [n] + list(getattr(n, "targets", [])) + [getattr(n, "target", None)]:
            name = getattr(t, "name", None) or (t.id if isinstance(t, ast.Name) else None)
            if name:
                out[name] = (s, n.end_lineno)
    return out


def normalize(patch: Patch, blocks: dict[str, str]) -> Patch:
    """BLOCK에 함수 이름을 쓴 patch는 그 이름을 정의한 블록으로 바꾸고,
    블록 일부 정의만 낸 patch는 낸 정의만 현재 블록에 끼운다 (나머지 정의는 그대로). 블록 전체를 낸 patch는 그대로."""
    if patch.block not in blocks:
        owner = [b for b, src in blocks.items() if src and patch.block in _names(src)]
        if not owner:
            return patch
        patch = Patch(owner[0], patch.content, patch.reason)
    try:
        old, new = _names(blocks[patch.block]), _names(patch.content)
    except SyntaxError:
        return patch
    if not new or set(old) <= set(new):
        return patch
    lines, nl = blocks[patch.block].splitlines(), patch.content.splitlines()
    for name, (s, e) in sorted(((k, v) for k, v in old.items() if k in new), key=lambda kv: -kv[1][0]):
        lines[s:e] = nl[new[name][0]:new[name][1]]
    lines += [l for k, (s, e) in new.items() if k not in old for l in [""] + nl[s:e]]   # 새 helper는 끝에
    return Patch(patch.block, "\n".join(lines), patch.reason)


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


if __name__ == "__main__":  # python -m loop.patch : parse_patch 확인
    body = "BLOCK: prompt\nREASON: r\n<<<CONTENT\nX = 1\nY = 2\n{}\n"
    for closer in ("CONTENT>>>", ">>>", "<<<CONTENT>>>", "<<<END_BLOCK>>>", "<<<END_CONTENT>>>", "  CONTENT>>>  "):
        p = parse_patch(body.format(closer))
        assert p and p.block == "prompt" and p.content == "X = 1\nY = 2", closer
    assert parse_patch("BLOCK: prompt\nREASON: r\n<<<CONTENT\nX = 1\n") is None          # 잘린 출력은 거부
    assert parse_patch(body.format(">>>").replace("BLOCK: prompt", "BLOCK: 'prompt'")).block == "prompt"  # 따옴표 제거
    assert parse_patch(body.format("CONTENT>>>") + "\nmore >>>\n").content == "X = 1\nY = 2"  # 첫 닫는 줄에서 끊음
    assert parse_patch(body.format(">>>END_BLOCK>>>")).content == "X = 1\nY = 2"
    # guard: 우회 시도 (문자열 쪼개기, os 호출, dunder) → 거부
    bad = ["x = open('data/' + 'test' + '.json').read()", "import os\nos.system('ls')", "y = (1).__class__",
           "z = getattr(build_messages, 'x')", "s = 'gold' + '_sql'\nq = DATA_DIR", "w = 'data/validation.json'"]
    for src in bad:
        assert guard(Patch("prompt", src, "r"), ["prompt"]), src
    assert guard(Patch("prompt", "SYSTEM_PROMPT = 'hi'\nFEWSHOT_IDS: list[str] = []", "r"), ["prompt"]) is None
    assert guard(Patch("code", "def f(s):\n    return {}", "r"), ["prompt"]) == "block 'code' not open"
    assert guard(Patch("prompt", "def f(:", "r"), ["prompt"]).startswith("syntax error")
    # normalize: 함수 이름 BLOCK, 일부 정의만 낸 patch
    code = "N = 1\n\n\ndef f(s):\n    return 1\n\n\ndef g(s):\n    return 2"
    m = normalize(Patch("f", "def f(s):\n    x = 3\n    return x", "r"), {"code": code})
    assert m.block == "code" and "def g" in m.content and "x = 3" in m.content and "return 1" not in m.content, m.content
    m = normalize(Patch("code", "def h(s):\n    return 4\n\n\ndef f(s):\n    return h(s)", "r"), {"code": code})
    assert set(_names(m.content)) == {"N", "f", "g", "h"} and "return h(s)" in m.content
    assert normalize(Patch("code", code.replace("2", "9"), "r"), {"code": code}).content == code.replace("2", "9")
    print("patch ok")
