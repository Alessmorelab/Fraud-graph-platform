from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceFile(BaseModel):
    name: str
    sha256: str
    size_bytes: int


class QualityFinding(BaseModel):
    code: str
    severity: Literal["INFO", "WARN", "ERROR", "BLOCKER"]
    message: str
    observed: int | float | str | None = None


class DataManifest(BaseModel):
    schema_version: str
    ingestion_version: str = "unknown"
    data_version: str
    created_at: datetime
    source_timezone: str
    sources: list[SourceFile]
    account_rows: int
    label_rows: int
    transaction_rows: int
    transaction_time_min: datetime | None
    transaction_time_max: datetime | None
    unique_active_accounts: int
    label_counts: dict[str, int]
    temporal_training_ready: bool
    findings: list[QualityFinding] = Field(default_factory=list)
    files: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> DataManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
