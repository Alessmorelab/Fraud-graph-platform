from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr

CitationKind = Literal["fact", "path", "transaction", "pattern", "system"]
KnowledgeDomain = Literal["account_investigation", "system_explanation"]
AgentFinding = Annotated[str, Field(min_length=1, max_length=500)]


class AgentCitation(BaseModel):
    kind: CitationKind
    id: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1, max_length=256)

    @property
    def marker(self) -> str:
        prefix = {
            "fact": "事实",
            "path": "路径",
            "transaction": "交易",
            "pattern": "模式",
            "system": "系统",
        }[self.kind]
        return f"[{prefix}:{self.id}]"


class AgentDraft(BaseModel):
    answer: str = Field(min_length=1, max_length=500)
    assessment: Literal[
        "manual_review_recommended",
        "insufficient_transaction_evidence",
        "information_only",
    ]
    key_findings: list[AgentFinding] = Field(min_length=1, max_length=6)
    citations: list[AgentCitation]
    limitations: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


class AgentToolTrace(BaseModel):
    tool: str = Field(min_length=1, max_length=128)
    status: Literal["ok", "rejected", "error"]
    result_count: int = Field(default=0, ge=0)
    detail: str | None = Field(default=None, max_length=512)


class AgentQuery(BaseModel):
    account_id: str = Field(min_length=1, max_length=256)
    question: str = Field(min_length=2, max_length=2_000)
    knowledge_domain: Literal[
        "auto", "account_investigation", "system_explanation"
    ] = "auto"


class AgentResponse(BaseModel):
    account_id: str
    knowledge_domain: KnowledgeDomain
    answer: str = Field(min_length=1)
    assessment: Literal[
        "manual_review_recommended",
        "insufficient_transaction_evidence",
        "information_only",
    ]
    key_findings: list[AgentFinding]
    citations: list[AgentCitation]
    tool_trace: list[AgentToolTrace]
    limitations: list[str]
    recommended_actions: list[str]
    evidence_packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_version: str
    data_version: str
    production_eligible: bool = False
    verifier_passed: bool
    generation_mode: Literal["deepseek", "deterministic_fallback"]
    provider_model: str | None = None
    citation_repair_applied: bool = False
    generated_at: datetime
    fallback_reason: str | None = None


class AgentCapabilities(BaseModel):
    available: bool = True
    external_model_enabled: bool
    external_model_ready: bool
    provider: Literal["deepseek"] = "deepseek"
    provider_model: str
    fallback_available: bool = True
    tools: list[str]
    guardrails: list[str]


class AgentSessionCreate(BaseModel):
    api_key: SecretStr = Field(min_length=8, max_length=512)
    model: Literal["deepseek-v4-flash", "deepseek-v4-pro"] = "deepseek-v4-flash"
    data_export_authorized: bool = False


class AgentSessionStatus(BaseModel):
    active: bool
    model: str | None = None
    expires_at: datetime | None = None
    storage: Literal["server_memory"] = "server_memory"
