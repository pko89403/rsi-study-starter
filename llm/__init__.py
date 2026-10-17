"""LLM 호출 진입점. EVOLVE-BLOCK 밖. 모델·endpoint는 환경 변수에서만 읽고, 서버별 차이는 llm/providers.py가 맡는다.
LLM_BACKEND=mock 이면 LLM 없이 규칙으로 답한다(loop 동작 확인용).

환경 변수 (SQL agent):  LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, LLM_PROVIDER(OpenRouter upstream 고정, 선택), LLM_MAX_TOKENS(기본 512)
환경 변수 (Improver):   IMPROVER_LLM_BASE_URL, IMPROVER_LLM_MODEL, IMPROVER_LLM_API_KEY, IMPROVER_LLM_PROVIDER (없으면 SQL agent 것)
Azure OpenAI: LLM_BASE_URL=https://<리소스>.openai.azure.com/ , LLM_MODEL=<배포 이름>
"""
from __future__ import annotations
import os
from loop.env import load_env


def chat(messages: list[dict], *, role: str = "agent", temperature: float = 0.0, max_tokens: int | None = None, retries: int = 6) -> str:
    load_env()
    if os.environ.get("LLM_BACKEND", "openai") == "mock":
        from llm.mock import chat as mock_chat
        return mock_chat(messages, role=role)
    from llm import providers   # 함수 안에서 import: python -m llm.providers 실행 때 이중 import 경고를 피함
    if max_tokens is None:  # SQL agent 호출. 추론 모델을 쓰면 LLM_MAX_TOKENS를 올린다
        max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "512"))
    return providers.complete(providers.resolve(role), messages, temperature=temperature, max_tokens=max_tokens, retries=retries)
