from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from fraud_graph.agent.providers.deepseek import DeepSeekClient, DeepSeekConfig
from fraud_graph.agent.schemas import AgentSessionStatus

ClientFactory = Callable[[DeepSeekConfig], DeepSeekClient]


@dataclass
class _SessionCredential:
    api_key: SecretStr
    model: str
    expires_at: datetime
    client: DeepSeekClient


class AgentSessionStore:
    """Process-local credential vault; secrets never leave server memory."""

    def __init__(
        self,
        base_config: DeepSeekConfig,
        *,
        ttl_seconds: int = 1_800,
        client_factory: ClientFactory = DeepSeekClient,
    ) -> None:
        if not 60 <= ttl_seconds <= 86_400:
            raise ValueError("Agent session TTL must be between 60 seconds and 24 hours")
        self.base_config = base_config
        self.ttl_seconds = ttl_seconds
        self.client_factory = client_factory
        self._sessions: dict[str, _SessionCredential] = {}

    async def create(self, api_key: SecretStr, model: str) -> tuple[str, AgentSessionStatus]:
        config = DeepSeekConfig(
            api_key=api_key.get_secret_value(),
            base_url=self.base_config.base_url,
            model=model,
            external_enabled=True,
            timeout_seconds=self.base_config.timeout_seconds,
            max_retries=self.base_config.max_retries,
            thinking_enabled=self.base_config.thinking_enabled,
        )
        client = self.client_factory(config)
        await client.validate_credentials()
        self._prune()
        session_id = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=self.ttl_seconds)
        self._sessions[session_id] = _SessionCredential(
            api_key=api_key,
            model=model,
            expires_at=expires_at,
            client=client,
        )
        return session_id, AgentSessionStatus(
            active=True,
            model=model,
            expires_at=expires_at,
        )

    def client(self, session_id: str | None) -> DeepSeekClient | None:
        credential = self._credential(session_id)
        return credential.client if credential is not None else None

    def status(self, session_id: str | None) -> AgentSessionStatus:
        credential = self._credential(session_id)
        if credential is None:
            return AgentSessionStatus(active=False)
        return AgentSessionStatus(
            active=True,
            model=credential.model,
            expires_at=credential.expires_at,
        )

    def remove(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    def _credential(self, session_id: str | None) -> _SessionCredential | None:
        self._prune()
        if not session_id:
            return None
        return self._sessions.get(session_id)

    def _prune(self) -> None:
        now = datetime.now(UTC)
        expired = [
            session_id
            for session_id, credential in self._sessions.items()
            if credential.expires_at <= now
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)
