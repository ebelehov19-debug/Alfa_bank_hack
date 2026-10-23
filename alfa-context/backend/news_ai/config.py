"""Explicit provider configuration; reading it never starts a network request."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOPIC = "Экономика и бизнес, финансы, регулирование, отрасли и регионы и их влияние на компании"
DEFAULT_BASE_URL = "https://changellenge.up.railway.app/v1"
DEFAULT_GEMMA_MODEL = "google/gemma-4-26b-a4b-it"
DEFAULT_GLM_MODEL = "z-ai/glm-5.3-flash"


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip().replace("_", "a").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_BASE_URL
    gemma_model: str = DEFAULT_GEMMA_MODEL
    glm_model: str = DEFAULT_GLM_MODEL
    topic: str = DEFAULT_TOPIC
    timeout: float = 90
    retries: int = 1
    batch_limit: int = 10
    max_calls_per_run: int = 20
    max_run_seconds: float = 180
    max_text_chars: int = 12000
    max_response_chars: int = 16000

    def __post_init__(self):
        if self.base_url:
            parsed = urlsplit(self.base_url)
            try:
                parsed.port
            except ValueError:
                raise ValueError("Invalid LLM_BASE_URL") from None
            if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("Invalid LLM_BASE_URL")
        for name, value in (("LLM_API_KEY", self.api_key), ("LLM_GEMMA_MODEL", self.gemma_model),
                            ("LLM_GLM_MODEL", self.glm_model)):
            if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 32 for c in value):
                raise ValueError(f"Invalid {name}")
        if not self.topic.strip() or len(self.topic) > 2000:
            raise ValueError("Invalid NEWS_AI_TOPIC")
        if not 0 < self.timeout <= 120 or not 0 <= self.retries <= 3:
            raise ValueError("LLM_TIMEOUT must be 0..120; LLM_RETRIES must be 0..3")
        if not 1 <= self.batch_limit <= 50 or not 1 <= self.max_calls_per_run <= 100:
            raise ValueError("NEWS_AI_BATCH_LIMIT must be 1..50; LLM_MAX_CALLS_PER_RUN must be 1..100")
        if not 1 <= self.max_run_seconds <= 600:
            raise ValueError("LLM_RUN_TIMEOUT must be 1..600")
        if not 100 <= self.max_text_chars <= 30000 or not 100 <= self.max_response_chars <= 64000:
            raise ValueError("Invalid model text/response limits")

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        values = {**read_env(Path(env_file) if env_file is not None else PROJECT_ROOT / ".env"), **os.environ}

        def integer(key, default):
            try:
                return int(values.get(key, str(default)))
            except (ValueError, TypeError):
                raise ValueError(f"Invalid {key}") from None

        def number(key, default):
            try:
                return float(values.get(key, str(default)))
            except (ValueError, TypeError):
                raise ValueError(f"Invalid {key}") from None

        return cls(api_key=values.get("LLM_API_KEY", "").strip(),
                   base_url=values.get("LLM_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/"),
                   gemma_model=values.get("LLM_GEMMA_MODEL", DEFAULT_GEMMA_MODEL).strip(),
                   glm_model=values.get("LLM_GLM_MODEL", DEFAULT_GLM_MODEL).strip(),
                   topic=values.get("NEWS_AI_TOPIC", DEFAULT_TOPIC).strip(),
                   timeout=number("LLM_TIMEOUT", 90), retries=integer("LLM_RETRIES", 1),
                   batch_limit=integer("NEWS_AI_BATCH_LIMIT", 10),
                   max_calls_per_run=integer("LLM_MAX_CALLS_PER_RUN", 20),
                   max_run_seconds=number("LLM_RUN_TIMEOUT", 180))

    def missing(self) -> list[str]:
        return [key for key, value in (("LLM_API_KEY", self.api_key), ("LLM_BASE_URL", self.base_url),
                                      ("LLM_GEMMA_MODEL", self.gemma_model), ("LLM_GLM_MODEL", self.glm_model))
                if not value.strip()]

    def public(self) -> dict:
        return {"configured": not self.missing(), "missing": self.missing(),
                "models": {"classifier": self.gemma_model, "analyst": self.glm_model},
                "topic": self.topic, "batch_limit": self.batch_limit,
                "max_calls_per_run": self.max_calls_per_run, "timeout": self.timeout,
                "retries": self.retries, "max_run_seconds": self.max_run_seconds}
