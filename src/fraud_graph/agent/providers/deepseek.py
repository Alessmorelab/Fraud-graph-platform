from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"


class DeepSeekError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str | None = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    external_enabled: bool = False
    timeout_seconds: float = 30.0
    max_retries: int = 2
    thinking_enabled: bool = False

    @property
    def ready(self) -> bool:
        return self.external_enabled and bool(self.api_key)

    @classmethod
    def from_env(cls, env_file: Path = Path(".env")) -> DeepSeekConfig:
        _load_env_file(env_file)
        return cls(
            api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            base_url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
            external_enabled=_env_bool("FRAUD_GRAPH_AGENT_EXTERNAL_ENABLED", False),
            timeout_seconds=float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "30")),
            max_retries=int(os.getenv("DEEPSEEK_MAX_RETRIES", "2")),
            thinking_enabled=_env_bool("DEEPSEEK_THINKING_ENABLED", False),
        )


Transport = Callable[[str, dict[str, str], bytes, float], dict[str, Any]]


class DeepSeekClient:
    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or _urlopen_json

    def build_request_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.1,
            "stream": False,
            "thinking": {"type": "enabled" if self.config.thinking_enabled else "disabled"},
        }

    def build_structured_request_body(
        self,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_tokens": 2_048,
            "temperature": 0,
            "stream": False,
            "thinking": {"type": "disabled"},
        }

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await self._request(self.build_request_body(messages, tools))

    async def chat_structured(
        self,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await self._request(self.build_structured_request_body(messages))

    async def validate_credentials(self) -> None:
        body = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with OK only. This is a credential validation request.",
                }
            ],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        await self._request(body)

    async def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        if not self.config.ready or not self.config.api_key:
            raise DeepSeekError("DeepSeek 外部调用未启用或 API Key 未配置")
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        endpoint = f"{self.config.base_url}/chat/completions"
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                return await asyncio.to_thread(
                    self.transport,
                    endpoint,
                    headers,
                    payload,
                    self.config.timeout_seconds,
                )
            except Exception as exc:  # Transport errors are normalized here.
                last_error = exc
                if attempt >= self.config.max_retries or not _retryable(exc):
                    break
                await asyncio.sleep(min(0.5 * (2**attempt), 2.0))
        raise DeepSeekError(f"DeepSeek 调用失败：{last_error}") from last_error


def _urlopen_json(
    url: str,
    headers: dict[str, str],
    payload: bytes,
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise DeepSeekError("DeepSeek 返回内容不是 JSON 对象")
    return result


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {429, 500, 502, 503, 504}
    return isinstance(exc, (TimeoutError, urllib.error.URLError))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")
