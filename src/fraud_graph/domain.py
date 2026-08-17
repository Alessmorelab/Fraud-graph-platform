from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class EvidenceGrade(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class RiskLabel(StrEnum):
    OTHER = "其它"
    SUSPECT = "嫌疑人"
    VICTIM = "受害人"


class TransactionEvent(BaseModel):
    transaction_id: str
    payer_account_id: str
    payee_account_id: str
    event_time_utc: datetime
    amount: Decimal


class PathStep(BaseModel):
    transaction_id: str
    payer_account_id: str
    payee_account_id: str
    event_time_utc: datetime
    amount: Decimal
    gap_seconds: int | None = None
    amount_ratio_to_previous: float | None = None


class SuspiciousPath(BaseModel):
    path_id: str
    root_account_id: str
    direction: str
    score: float = Field(ge=0.0, le=1.0)
    steps: list[PathStep]
    evidence_codes: list[str]


class AssociatedAccount(BaseModel):
    root_account_id: str
    account_id: str
    association_score: float = Field(ge=0.0, le=1.0)
    direction: Literal["incoming", "outgoing", "both"]
    min_hops: int = Field(ge=1)
    transaction_ids: list[str]
    path_ids: list[str]
    evidence_codes: list[str]
    supporting_path_count: int = Field(default=1, ge=1)
    associated_risk_score: float | None = Field(default=None, ge=0.0, le=1.0)
    associated_evidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    pattern_types: list[str] = Field(default_factory=list)


class FlowPattern(BaseModel):
    pattern_id: str = Field(min_length=1)
    pattern_type: Literal["FAN_IN", "FAN_OUT", "PASS_THROUGH", "RECIPROCAL"]
    root_account_id: str
    score: float = Field(ge=0.0, le=1.0)
    transaction_ids: list[str]
    related_account_ids: list[str]
    event_time_start: datetime
    event_time_end: datetime
    description: str


class RiskScore(BaseModel):
    account_id: str
    risk_score: float = Field(ge=0.0, le=1.0)
    risk_rank: int
    model_version: str
    data_version: str
    feature_as_of: datetime
    has_transactions: bool
    model_training_mode: Literal["temporal", "retrospective_static", "unknown"] = "unknown"
    model_feature_set: Literal["behavior", "profile", "combined", "unknown"] = "unknown"
    production_eligible: bool = False
    eligibility_blockers: list[str] = Field(default_factory=list)
    evidence_score: float | None = Field(default=None, ge=0.0, le=1.0)
    anomaly_score: float | None = Field(default=None, ge=0.0, le=1.0)


class EvidenceFact(BaseModel):
    code: str
    description: str
    value: float | int | str | None = None
    transaction_ids: list[str] = Field(default_factory=list)


class EvidencePacket(BaseModel):
    account_id: str
    generated_at: datetime
    score: RiskScore
    grade: EvidenceGrade
    facts: list[EvidenceFact]
    paths: list[SuspiciousPath]
    patterns: list[FlowPattern] = Field(default_factory=list)
    limitations: list[str]
    recommended_actions: list[str]


class RiskQueueItem(BaseModel):
    account_id: str
    risk_score: float = Field(ge=0.0, le=1.0)
    risk_rank: int = Field(ge=1)
    has_transactions: bool
    model_training_mode: Literal["temporal", "retrospective_static", "unknown"]
    production_eligible: bool


class RiskQueuePage(BaseModel):
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    items: list[RiskQueueItem]


class OperationsSummary(BaseModel):
    scored_accounts: int = Field(ge=0)
    active_accounts: int = Field(ge=0)
    review_capacity_count: int = Field(ge=0)
    review_capacity_fraction: float = Field(ge=0.0, le=1.0)
    model_version: str
    data_version: str
    feature_version: str
    feature_as_of: datetime
    model_training_mode: Literal["temporal", "retrospective_static", "unknown"]
    production_eligible: bool
    eligibility_blockers: list[str]


class AccountWorkspace(BaseModel):
    account_id: str
    evidence: EvidencePacket
    associations: list[AssociatedAccount]


class Capability(BaseModel):
    key: str
    status: Literal["available", "contract_ready", "upstream_required"]
    endpoint: str | None = None
