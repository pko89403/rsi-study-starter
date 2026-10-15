"""OpenAI 호환 API 호출. EVOLVE-BLOCK 밖. 모델·endpoint는 환경 변수에서만 읽는다.
LLM_BACKEND=mock 이면 LLM 없이 규칙으로 답한다(loop 동작 확인용).

환경 변수 (SQL agent):  LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, LLM_PROVIDER(OpenRouter provider 고정, 선택)
환경 변수 (Improver):   IMPROVER_LLM_BASE_URL, IMPROVER_LLM_MODEL, IMPROVER_LLM_API_KEY, IMPROVER_LLM_PROVIDER (없으면 SQL agent 것)
"""
from __future__ import annotations
import os, random, time
from loop.env import load_env

_clients: dict = {}


def _cfg(role: str) -> tuple[str, str, str, str | None]:
    prefix = "IMPROVER_" if role == "improver" else ""
    g = lambda k, d=None: os.environ.get(f"{prefix}{k}") or os.environ.get(k, d)
    return g("LLM_BASE_URL"), g("LLM_MODEL"), g("LLM_API_KEY", "none"), g("LLM_PROVIDER")


def chat(messages: list[dict], *, role: str = "agent", temperature: float = 0.0, max_tokens: int = 512, retries: int = 6) -> str:
    load_env()
    if os.environ.get("LLM_BACKEND", "openai") == "mock":
        from sql_agent.mock_llm import chat as mock_chat
        return mock_chat(messages, role=role)
    from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError
    base, model, key, provider = _cfg(role)
    if not base or not model:
        raise RuntimeError("LLM_BASE_URL / LLM_MODEL 환경 변수가 없습니다")
    client = _clients.get(role) or _clients.setdefault(role, OpenAI(base_url=base, api_key=key, timeout=120))
    extra: dict = {}
    if provider:  # OpenRouter: 같은 모델을 같은 provider(양자화)로 고정
        extra["provider"] = {"order": [provider], "allow_fallbacks": False}
    for attempt in range(retries):
        try:
            r = client.chat.completions.create(model=model, messages=messages, temperature=temperature,
                                               max_tokens=max_tokens, extra_body=extra or None)
            return r.choices[0].message.content or ""
        except (APIStatusError, APIConnectionError, APITimeoutError) as e:
            status = getattr(e, "status_code", None)
            if status is not None and status < 500 and status != 429:
                raise  # 잘못된 요청·인증 실패는 재시도 의미 없음
            if attempt == retries - 1:
                raise
            time.sleep(min(60, 2 ** attempt) + random.random())
    raise RuntimeError("unreachable")
