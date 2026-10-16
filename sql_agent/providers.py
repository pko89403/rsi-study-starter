"""LLM provider 계층. 어떤 서버(LM Studio, OpenRouter, Azure OpenAI)든 같은 chat 호출로 쓰게 한다.

resolve(role) -> Endpoint : 환경 변수에서 endpoint를 읽고 provider 종류를 URL로 판별한다.
complete(ep, ...) -> str  : 호출. 재시도와 모델별 파라미터 차이를 여기서 흡수한다.

provider 종류
- azure      : https://<리소스>.openai.azure.com/  →  /openai/v1/ 을 붙인다. LLM_MODEL = 배포 이름.
- openrouter : https://openrouter.ai/api/v1       →  LLM_PROVIDER 가 있으면 그 upstream으로 고정(양자화 고정).
- openai     : 그 밖의 OpenAI 호환 서버 (LM Studio 등).

모델별 차이: 추론 모델(gpt-5 계열 등)은 max_tokens 대신 max_completion_tokens를 요구하고 temperature 0을 거부한다.
모델 이름으로 분기하지 않고, 서버가 400으로 거부한 파라미터를 보고 바꿔 다시 보낸 뒤 기억한다.
"""
from __future__ import annotations
import hashlib, json, os, random, sqlite3, sys, threading, time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Endpoint:
    kind: str                       # "azure" | "openrouter" | "openai"
    base_url: str
    model: str
    api_key: str
    extra_body: dict | None = field(default=None, hash=False, compare=False)


def _kind(url: str) -> str:
    if ".openai.azure.com" in url:
        return "azure"
    if "openrouter.ai" in url:
        return "openrouter"
    return "openai"


def resolve(role: str) -> Endpoint:
    """role="improver" 면 IMPROVER_* 를 먼저 보고, 없으면 LLM_* 를 쓴다."""
    prefix = "IMPROVER_" if role == "improver" else ""
    g = lambda k, d=None: os.environ.get(f"{prefix}{k}") or os.environ.get(k, d)
    base, model = g("LLM_BASE_URL"), g("LLM_MODEL")
    if not base or not model:
        raise RuntimeError("LLM_BASE_URL / LLM_MODEL 환경 변수가 없습니다")
    kind = _kind(base)
    extra = None
    if kind == "azure" and "/openai/" not in base:
        base = base.rstrip("/") + "/openai/v1/"
    if kind == "openrouter" and g("LLM_PROVIDER"):
        extra = {"provider": {"order": [g("LLM_PROVIDER")], "allow_fallbacks": False}}
    return Endpoint(kind, base, model, g("LLM_API_KEY", "none"), extra)


_clients: dict = {}
_quirks: dict = {}   # (base_url, model) -> {"max_completion_tokens": bool, "no_temperature": bool}


def _client(ep: Endpoint):
    from openai import OpenAI
    key = (ep.base_url, ep.api_key)
    if key not in _clients:
        _clients[key] = OpenAI(base_url=ep.base_url, api_key=ep.api_key, timeout=120)
    return _clients[key]


_FIX = {"max_tokens": "max_completion_tokens", "temperature": "no_temperature"}


def _adapt(q: dict, tried: set, e, model: str) -> bool:
    """400 오류가 추론 모델의 파라미터 제약이면 q를 고치고 True(바로 재시도). 아니면 False.
    tried는 호출 하나 안에서 이미 바꿔 본 파라미터. 병렬 호출이 같은 400을 동시에 받아도 각자 한 번씩 재시도한다."""
    body = getattr(e, "body", None) or {}
    err = body.get("error", body) if isinstance(body, dict) else {}
    param = err.get("param") if isinstance(err, dict) else None
    if param not in _FIX or param in tried:
        return False
    tried.add(param)
    if not q.get(_FIX[param]):
        q[_FIX[param]] = True
        if param == "temperature":
            print(f"[llm] {model}: temperature 지정 불가 → 기본값으로 호출. 평가 조건 3(temperature 0)과 다름", file=sys.stderr, flush=True)
    return True


_db, _lock = None, threading.Lock()


def _cache():
    """LLM_CACHE=<sqlite 경로> 일 때만 켠다. loop가 켜고(test_eval은 안 켬) SQL agent subprocess가 물려받는다."""
    global _db
    path = os.environ.get("LLM_CACHE")
    if not path:
        return None
    if _db is None:
        _db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        _db.execute("CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, v TEXT)")
    return _db


def complete(ep: Endpoint, messages: list[dict], *, temperature: float, max_tokens: int, retries: int = 6) -> str:
    """temperature 0 호출은 LLM_CACHE가 켜져 있으면 (endpoint, 모델, 메시지, max_tokens)로 캐시한다.
    같은 commit을 다시 재거나 screen 문제를 full에서 다시 풀 때, `route`·`fix`만 바꾼 patch를 잴 때(`generate` 메시지는 그대로) 호출이 줄어든다."""
    db = _cache() if temperature == 0 else None
    if db is None:
        return _call(ep, messages, temperature=temperature, max_tokens=max_tokens, retries=retries)
    k = hashlib.sha256(json.dumps([ep.base_url, ep.model, messages, max_tokens], sort_keys=True).encode()).hexdigest()
    with _lock:
        row = db.execute("SELECT v FROM c WHERE k=?", (k,)).fetchone()
    if row:
        return row[0]
    out = _call(ep, messages, temperature=temperature, max_tokens=max_tokens, retries=retries)
    with _lock:
        db.execute("INSERT OR REPLACE INTO c VALUES (?, ?)", (k, out)); db.commit()
    return out


def _call(ep: Endpoint, messages: list[dict], *, temperature: float, max_tokens: int, retries: int) -> str:
    from openai import APIStatusError, APIConnectionError, APITimeoutError
    client, q, attempt, tried = _client(ep), _quirks.setdefault((ep.base_url, ep.model), {}), 0, set()
    while True:
        kw: dict = {"max_completion_tokens" if q.get("max_completion_tokens") else "max_tokens": max_tokens}
        if not q.get("no_temperature"):
            kw["temperature"] = temperature
        try:
            r = client.chat.completions.create(model=ep.model, messages=messages, extra_body=ep.extra_body, **kw)
            return r.choices[0].message.content or ""
        except (APIStatusError, APIConnectionError, APITimeoutError) as e:
            status = getattr(e, "status_code", None)
            if status == 400 and _adapt(q, tried, e, ep.model):
                continue  # 파라미터만 바꿔 바로 다시 (재시도 횟수에 안 셈)
            if status is not None and status < 500 and status != 429:
                raise  # 잘못된 요청·인증 실패는 재시도 의미 없음
            attempt += 1
            if attempt >= retries:
                raise
            time.sleep(min(60, 2 ** (attempt - 1)) + random.random())


if __name__ == "__main__":  # python -m sql_agent.providers : 캐시 확인 (서버 호출 없음)
    import tempfile
    calls = []
    globals()["_call"] = lambda ep, m, **kw: calls.append(kw["temperature"]) or f"out{len(calls)}"
    ep = Endpoint("openai", "http://x/v1", "m", "k")
    msg = [{"role": "user", "content": "q"}]
    assert complete(ep, msg, temperature=0, max_tokens=8) == "out1" and complete(ep, msg, temperature=0, max_tokens=8) == "out2"  # 꺼져 있으면 매번 호출
    os.environ["LLM_CACHE"] = os.path.join(tempfile.mkdtemp(), "c.sqlite")
    assert complete(ep, msg, temperature=0, max_tokens=8) == "out3"
    assert complete(ep, msg, temperature=0, max_tokens=8) == "out3" and len(calls) == 3          # 같은 요청은 캐시
    assert complete(ep, msg, temperature=0.7, max_tokens=8) == "out4"                           # temperature > 0 은 캐시 안 함
    assert complete(ep, msg, temperature=0, max_tokens=16) == "out5"                            # max_tokens 다르면 다른 키
    assert complete(Endpoint("openai", "http://x/v1", "m2", "k"), msg, temperature=0, max_tokens=8) == "out6"  # 모델 다르면 다른 키
    print("cache ok")
