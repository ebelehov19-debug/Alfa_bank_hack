"""Synchronous OpenAI-compatible transport with bounded attempts and safe errors."""
from __future__ import annotations

from dataclasses import dataclass, field
import time

import httpx

from .config import Settings
from .schema import InvalidResponse, decode_json


class ModelError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Budget:
    max_calls: int
    seconds: float
    calls: int = 0
    started: float = field(default_factory=time.monotonic)

    def remaining(self):
        return self.seconds - (time.monotonic() - self.started)

    def take(self):
        if self.calls >= self.max_calls:
            raise ModelError("call_limit", "Достигнут лимит обращений к моделям; необработанные новости сохранены.")
        if self.remaining() <= 0:
            raise ModelError("time_limit", "Достигнут лимит времени; необработанные новости сохранены.")
        self.calls += 1


class ModelClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self.owns_client = client is None

    def close(self):
        if self.owns_client:
            self.client.close()

    def complete(self, model: str, messages: list[dict], validator, budget: Budget) -> dict:
        for attempt in range(self.settings.retries + 1):
            budget.take()
            retry = False
            try:
                with self.client.stream(
                    "POST", self.settings.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.api_key, "Content-Type": "application/json"},
                    json={"model": model, "messages": messages, "temperature": 0,
                          "max_tokens": 2048, "response_format": {"type": "json_object"}},
                    timeout=min(self.settings.timeout, max(0.01, budget.remaining())),
                    follow_redirects=False,
                ) as response:
                    if response.status_code >= 300:
                        # Inspect a bounded error body only to recognize the shared gateway budget.
                        # It is never returned or logged, and does not trigger a failover request.
                        error_parts, error_size = [], 0
                        for chunk in response.iter_bytes():
                            error_parts.append(chunk[:max(0, 4096 - error_size)])
                            error_size += len(chunk)
                            if error_size >= 4096:
                                break
                        if "budget has been exceeded" in b"".join(error_parts).decode("utf-8", errors="replace").lower():
                            raise ModelError("budget_exceeded", "Исчерпан общий суточный бюджет провайдера; запросы остановлены.")
                        retry = response.status_code in {408, 429, 500, 502, 503, 504}
                        code = "provider_auth" if response.status_code in {401, 403} else "provider_http"
                        failure = ModelError(code, "Провайдер отклонил запрос к модели.")
                    else:
                        parts, size = [], 0
                        for chunk in response.iter_bytes():
                            if budget.remaining() <= 0:
                                raise ModelError("time_limit", "Достигнут лимит времени ответа модели.")
                            size += len(chunk)
                            if size > self.settings.max_response_chars * 8:
                                raise ModelError("response_too_large", "Ответ модели превысил допустимый размер.")
                            parts.append(chunk)
                        try:
                            envelope = decode_json(b"".join(parts).decode("utf-8"), self.settings.max_response_chars * 8)
                            content = envelope["choices"][0]["message"]["content"]
                            result = validator(content, self.settings.max_response_chars)
                        except (InvalidResponse, KeyError, IndexError, TypeError, UnicodeError, ValueError):
                            raise ModelError("invalid_response", "Модель вернула ответ, не соответствующий JSON-схеме.") from None
                        return result
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                failure = ModelError("provider_unavailable", "Не удалось получить ответ модели; новость сохранена для повтора.")
                retry = True
            except httpx.HTTPError:
                raise ModelError("provider_unavailable", "Не удалось выполнить запрос к модели.") from None
            if not retry or attempt == self.settings.retries:
                raise failure from None
            pause = min(0.25 * (2 ** attempt), max(0, budget.remaining()))
            if pause:
                time.sleep(pause)
        raise ModelError("provider_unavailable", "Не удалось получить ответ модели.")
