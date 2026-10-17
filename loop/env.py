"""starter 루트의 .env 를 읽어 환경 변수로 넣고(이미 있는 값은 유지), 필수 값을 검사한다."""
from __future__ import annotations
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def require_llm() -> None:
    load_env()
    if os.environ.get("LLM_BACKEND", "openai") == "mock":
        return
    missing = [k for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY") if not os.environ.get(k)]   # 로컬 LM Studio는 LLM_API_KEY=none
    if missing:
        sys.exit(f"환경 변수 {', '.join(missing)} 가 없습니다. starter/.env 파일에 적거나 export 하세요. (예: .env.example)")
    if "gpt-4.1-nano" not in os.environ["LLM_MODEL"]:   # Azure 배포 이름이 다르면 같은 모델이어도 뜬다. 막지는 않는다
        print(f"[env] LLM_MODEL={os.environ['LLM_MODEL']}: 평가 조건 3은 gpt-4.1-nano. 다른 모델이면 결과를 표에 그대로 넣을 수 없다", file=sys.stderr)
