"""고정 kernel. 참가자 hook도 Improver도 바꾸지 않는다.

- kernel_accept: 최종 accept 판정. hook accept와 AND로 묶여서 hook은 조건을 더 엄격하게만 할 수 있다.
- anchors_sha: 고정 파일 hash. round마다 확인하고 바뀌면 loop가 멈춘다.
- KNOBS: 참가자 hook이 선언한 손잡이(이름·타입·범위·기본값). Improver는 improver/incumbent.py의 IMPROVER 값만 바꾼다.
"""
from __future__ import annotations
import ast, hashlib, json, os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXED_FILES = ["data/validation.json", "data/test.json", "data/fewshot_pool.json", "data/tables.json",
               "scripts/test_eval.py", "evaluator/evaluate.py", "evaluator/rules_v0.py",
               "loop/run.py", "loop/patch.py", "loop/archive.py", "loop/kernel.py", "loop/meta.py", "loop/context.py", "loop/env.py",
               ".claude/settings.json", "AGENTS.md", "CLAUDE.md", ".gemini/settings.json"]
FIXED_DIRS = ["data/database"]   # 채점용 SQLite. 내용이 바뀌면 점수가 바뀐다
RULER_TAG = "ruler-v2"   # 고정 파일의 기준 commit에 운영자가 단 tag. 고정 파일을 바꿀 때마다 번호를 올리고 `make ruler`
RULER_STATUS = "unchecked"   # require_ruler() 결과: ok | override | skipped | changed. round·test·meta 기록에 남는다
IMPROVER_FILE = "improver/incumbent.py"
WIDE_MARGIN = 4   # 많은 답이 바뀐 patch(프롬프트 수정 등)의 최소 순증. placebo 보정 전 기본값


def _fixed_paths(root: Path) -> list[Path]:
    out = [root / f for f in FIXED_FILES]
    for d in FIXED_DIRS:
        out += sorted((root / d).rglob("*.sqlite")) if (root / d).exists() else []
    return out


def anchors_sha(root: Path = ROOT) -> str:
    h = hashlib.sha256()
    for p in _fixed_paths(root):
        h.update(str(p.relative_to(root)).encode()); h.update(p.read_bytes() if p.exists() else b"<missing>")
    return h.hexdigest()[:12]


def _git(root: Path, *args, timeout: int = 20) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def check_ruler(root: Path = ROOT, remote: bool = True) -> tuple[str, str]:
    """고정 파일이 RULER_TAG(origin의 tag)와 같은지. 반환 (status, detail).
    status: ok / override(RULER_OVERRIDE=1로 실행, 기록됨) / skipped(tag 없음) / changed(다름 → 실행 거부).
    로컬 tag가 origin과 다르거나 없으면 origin에서 강제로 받아 온다 (참가자는 origin tag를 못 바꾼다)."""
    ref = f"refs/tags/{RULER_TAG}"
    local = _git(root, "rev-parse", "-q", "--verify", ref)
    if remote:
        ls = _git(root, "ls-remote", "--tags", "origin", RULER_TAG, timeout=15) or ""
        origin = next((l.split()[0] for l in ls.splitlines() if l.strip().endswith(ref)), None)
        if origin and origin != local:
            _git(root, "fetch", "--force", "--tags", "origin", timeout=60)
            local = _git(root, "rev-parse", "-q", "--verify", ref)
    if not local:
        return "skipped", f"tag {RULER_TAG} 없음 (git fetch --force --tags origin)"
    d = _git(root, "diff", "--name-only", RULER_TAG, "--", *FIXED_FILES, *FIXED_DIRS)
    changed = [l for l in (d or "").splitlines() if l.strip()]
    if not changed:
        return "ok", ""
    detail = f"고정 파일이 {RULER_TAG}와 다름: {', '.join(changed)}"
    if os.environ.get("RULER_OVERRIDE") == "1":
        return "override", detail
    return "changed", detail


def require_ruler(root: Path = ROOT) -> str:
    """make round / make test / make meta-* 시작 시 호출. changed면 종료. status를 RULER_STATUS에 둔다."""
    global RULER_STATUS
    status, detail = check_ruler(root)
    RULER_STATUS = status
    if status == "changed":
        sys.exit(f"{detail}\n고정 파일을 되돌리거나(git checkout {RULER_TAG} -- <파일>), 일부러 바꾼 것이면 RULER_OVERRIDE=1 로 실행한다 (결과에 기록됨).")
    if status != "ok":
        print(f"[ruler] {status}: {detail}")
    return status


def kernel_accept(before: dict, after: dict, before_preds: dict, after_preds: dict, db_of: dict) -> tuple[bool, dict]:
    """문제별 짝 비교 (둘 다 rules_v0 채점). 바뀐 답 수에 따라 문턱이 다르다:
    바뀐 답 <= 3: 순증 >= 1, 잃은 문제 0 / <= 12: 순증 >= 2, 잃은 문제 <= 1 / 그 외: 순증 >= WIDE_MARGIN.
    순증 >= 2면 얻은 문제가 2개 이상의 DB에 걸쳐야 한다 (한 DB 특화 patch 차단)."""
    b = {p["id"]: p["correct"] for p in before["per_problem"]}
    a = {p["id"]: p["correct"] for p in after["per_problem"]}
    fixed = sorted(i for i in a if a[i] and not b.get(i))
    broken = sorted(i for i in a if b.get(i) and not a[i])
    net = len(fixed) - len(broken)
    changed = sum(1 for i in a if after_preds.get(i, "") != before_preds.get(i, ""))
    if changed <= 3:
        ok = net >= 1 and not broken
    elif changed <= 12:
        ok = net >= 2 and len(broken) <= 1
    else:
        ok = net >= WIDE_MARGIN
    if ok and net >= 2 and len({db_of.get(i) for i in fixed}) < 2:
        ok = False
    return ok, {"net": net, "fixed_ids": fixed, "broken_ids": broken, "changed": changed}


def strip_gold(problems: list[dict]) -> dict:
    """Improver 쪽 hook에 넘기는 문제: 정답 SQL을 뺀다."""
    return {p["id"]: {k: v for k, v in p.items() if k != "gold_sql"} for p in problems}


# ---- KNOBS: hook이 선언, Improver가 값을 고른다 ----
# 형식: ("int", lo, hi, default) | ("float", lo, hi, default) | ("enum", [choices], default) | ("str", max_len, default) | ("bool", default)

def collect_knobs(modules) -> dict:
    schema: dict = {}
    for m in modules:
        for name, spec in (getattr(m, "KNOBS", None) or {}).items():
            if name in schema and schema[name] != spec:
                raise ValueError(f"KNOB '{name}' declared twice with different specs ({m.__name__})")
            schema[name] = tuple(spec)
    return schema


def collect_docs(modules) -> dict:
    """hook의 KNOB_DOCS = {이름: 무엇을 바꾸는 손잡이인지 한 줄}."""
    docs: dict = {}
    for m in modules:
        docs.update(getattr(m, "KNOB_DOCS", None) or {})
    return docs


def describe(spec: tuple) -> str:
    """Improver LLM이 읽을 손잡이 명세."""
    kind = spec[0]
    if kind in ("int", "float"):
        return f"{'integer' if kind == 'int' else 'number'} from {spec[1]} to {spec[2]} (default {spec[3]!r})"
    if kind == "enum":
        return f"one of {spec[1]} (default {spec[2]!r})"
    if kind == "str":
        return f"free text of at most {spec[1]} characters (default {spec[2]!r})"
    if kind == "bool":
        return f"true or false (default {spec[1]})"
    return str(spec)


def _check(spec: tuple, v):
    kind = spec[0]
    if kind == "int":
        return isinstance(v, int) and not isinstance(v, bool) and spec[1] <= v <= spec[2]
    if kind == "float":
        return isinstance(v, (int, float)) and not isinstance(v, bool) and spec[1] <= v <= spec[2]
    if kind == "enum":
        return v in spec[1]
    if kind == "str":
        return isinstance(v, str) and len(v) <= spec[1]
    if kind == "bool":
        return isinstance(v, bool)
    return False


def validate(values: dict, schema: dict) -> tuple[dict, list[str]]:
    """스키마에 맞는 값만 쓰고, 없거나 틀린 값은 기본값으로 채운다. 반환: (값 전부, 문제 목록)."""
    errors = [f"unknown knob '{k}'" for k in values if k not in schema]
    out = {}
    for name, spec in schema.items():
        if name in values and _check(spec, values[name]):
            out[name] = values[name]
        else:
            if name in values:
                errors.append(f"bad value for '{name}': {values[name]!r} (spec {spec})")
            out[name] = spec[-1]
    return out, errors


def read_improver(path: str | Path = IMPROVER_FILE) -> dict:
    """EVOLVE-BLOCK 'improver'의 `IMPROVER = {...}` 를 literal로만 읽는다 (실행하지 않는다)."""
    from loop.patch import read_block
    src = read_block(Path(path), "improver")
    if src is None:
        raise ValueError(f"{path}: no EVOLVE-BLOCK 'improver'")
    tree = ast.parse(src)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign) or \
       [getattr(t, "id", None) for t in tree.body[0].targets] != ["IMPROVER"]:
        raise ValueError(f"{path}: block must be exactly `IMPROVER = {{...}}`")
    v = ast.literal_eval(tree.body[0].value)
    if not isinstance(v, dict):
        raise ValueError(f"{path}: IMPROVER must be a dict")
    return v


def write_improver(values: dict, path: str | Path = IMPROVER_FILE) -> None:
    from loop.patch import apply
    body = "IMPROVER = {\n" + "".join(f"    {json.dumps(k)}: {v!r},\n" for k, v in sorted(values.items())) + "}"
    apply(Path(path), "improver", body)


def improver_sha(values: dict) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:8]


if __name__ == "__main__":  # python -m loop.kernel : 판정·손잡이 확인 (LLM 없음)
    import tempfile
    pp = lambda ok: {"per_problem": [{"id": f"q{i}", "correct": c} for i, c in enumerate(ok)]}
    db = {f"q{i}": f"db{i % 3}" for i in range(20)}
    preds = {f"q{i}": "S" for i in range(20)}
    base = pp([True] * 10 + [False] * 10)
    narrow = dict(preds, q10="X")
    ok, info = kernel_accept(base, pp([True] * 11 + [False] * 9), preds, narrow, db)
    assert ok and info["net"] == 1 and info["changed"] == 1                     # 좁은 수정: +1, 잃은 것 0 → accept
    ok, _ = kernel_accept(base, pp([False] + [True] * 10 + [False] * 9), preds, dict(narrow, q0="Y"), db)
    assert not ok                                                               # 좁은 수정인데 잃은 문제 있음 → 거부
    wide = {k: "Z" for k in preds}
    ok, _ = kernel_accept(base, pp([True] * 13 + [False] * 7), preds, wide, db)
    assert not ok                                                               # 넓은 수정(바뀐 답 20)은 +3으로 부족
    ok, _ = kernel_accept(base, pp([True] * 14 + [False] * 6), preds, wide, db)
    assert ok                                                                   # +4, 3개 DB → accept
    one_db = {f"q{i}": "same" for i in range(20)}
    ok, _ = kernel_accept(base, pp([True] * 14 + [False] * 6), preds, wide, one_db)
    assert not ok                                                               # 얻은 문제가 한 DB뿐 → 거부
    schema = {"n_wrong": ("int", 3, 20, 10), "mode": ("enum", ["a", "b"], "a"), "hint": ("str", 5, ""), "on": ("bool", False)}
    v, err = validate({"n_wrong": 50, "mode": "b", "hint": "toolong", "x": 1}, schema)
    assert v == {"n_wrong": 10, "mode": "b", "hint": "", "on": False} and len(err) == 3, (v, err)
    f = Path(tempfile.mkdtemp()) / "inc.py"
    f.write_text("# === EVOLVE-BLOCK-START: improver ===\nIMPROVER = {}\n# === EVOLVE-BLOCK-END ===\n")
    write_improver({"mode": "b", "n_wrong": 7}, f)
    assert read_improver(f) == {"mode": "b", "n_wrong": 7}
    f.write_text("# === EVOLVE-BLOCK-START: improver ===\nIMPROVER = __import__('os')\n# === EVOLVE-BLOCK-END ===\n")
    try:
        read_improver(f); raise AssertionError("must reject non-literal")
    except ValueError:
        pass
    assert describe(("int", 3, 20, 10)) == "integer from 3 to 20 (default 10)"
    assert len(anchors_sha()) == 12
    assert describe(("str", 300, "")) == "free text of at most 300 characters (default '')"
    saved = os.environ.pop("RULER_OVERRIDE", None)
    try:
        d = Path(tempfile.mkdtemp())
        for f in FIXED_FILES:
            (d / f).parent.mkdir(parents=True, exist_ok=True); (d / f).write_text("x")
        (d / "data/database/db1").mkdir(parents=True); (d / "data/database/db1/db1.sqlite").write_bytes(b"s")
        g = lambda *a: subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgSign=false", *a], cwd=d, capture_output=True, text=True, check=True)
        g("init", "-q"); g("add", "-A"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v0")
        assert check_ruler(d, remote=False)[0] == "skipped"            # tag 없음 → skipped
        g("tag", RULER_TAG)
        assert check_ruler(d, remote=False)[0] == "ok"
        (d / "loop/run.py").write_text("y")
        st, why = check_ruler(d, remote=False)
        assert st == "changed" and "loop/run.py" in why
        os.environ["RULER_OVERRIDE"] = "1"
        assert check_ruler(d, remote=False)[0] == "override"
        del os.environ["RULER_OVERRIDE"]
        (d / "loop/run.py").write_text("x")
        (d / "data/database/db1/db1.sqlite").write_bytes(b"t")
        assert check_ruler(d, remote=False)[0] == "changed"            # 채점 DB 변경도 잡는다
    finally:
        if saved is not None:
            os.environ["RULER_OVERRIDE"] = saved
    print("kernel ok")
