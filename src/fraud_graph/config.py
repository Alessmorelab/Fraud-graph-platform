from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class SourceConfig(BaseModel):
    timezone: str = "Asia/Shanghai"
    account_workbook: str
    label_workbook: str
    transaction_workbook: str


class StorageConfig(BaseModel):
    normalized_dir: Path
    feature_dir: Path
    artifact_dir: Path


class IngestionConfig(BaseModel):
    batch_size: int = Field(default=50_000, ge=1_000, le=500_000)


class FeatureConfig(BaseModel):
    lookback_days: int = Field(default=184, ge=1, le=3_650)


class SnapshotConfig(BaseModel):
    observation_days: int = Field(default=28, ge=1, le=3_650)
    anchor_step_days: int = Field(default=7, ge=1, le=365)
    include_events: bool = True
    train_fraction: float = Field(default=0.6, gt=0.0, lt=1.0)
    validation_fraction: float = Field(default=0.2, gt=0.0, lt=1.0)
    embargo_days: int | None = Field(default=None, ge=0, le=3_650)

    @model_validator(mode="after")
    def validate_window_coverage(self) -> SnapshotConfig:
        if self.anchor_step_days > self.observation_days:
            raise ValueError("anchor_step_days must not exceed observation_days")
        if self.train_fraction + self.validation_fraction >= 1.0:
            raise ValueError("train_fraction + validation_fraction must be less than 1")
        effective_embargo = (
            self.observation_days if self.embargo_days is None else self.embargo_days
        )
        if effective_embargo < self.observation_days:
            raise ValueError("embargo_days must cover the complete observation window")
        return self


class TemporalConfig(BaseModel):
    observation_days: int = Field(default=28, ge=1, le=3_650)
    prediction_days: int = Field(default=7, ge=1, le=365)
    label_maturity_days: int = Field(default=7, ge=0, le=365)
    anchor_step_days: int = Field(default=7, ge=1, le=365)
    train_fraction: float = Field(default=0.6, gt=0.0, lt=1.0)
    validation_fraction: float = Field(default=0.2, gt=0.0, lt=1.0)

    @model_validator(mode="after")
    def validate_fractions(self) -> TemporalConfig:
        if self.train_fraction + self.validation_fraction >= 1.0:
            raise ValueError("train_fraction + validation_fraction must be less than 1")
        return self


class ModelConfig(BaseModel):
    positive_labels: list[int] = Field(default_factory=lambda: [1])
    include_profile_features: bool = False
    random_seed: int = 20260807
    validation_size: float = Field(default=0.2, gt=0.0, lt=0.5)
    test_size: float = Field(default=0.2, gt=0.0, lt=0.5)
    top_fraction: float = Field(default=0.05, gt=0.0, le=0.5)

    @model_validator(mode="after")
    def validate_static_split_fractions(self) -> ModelConfig:
        if self.validation_size + self.test_size >= 1.0:
            raise ValueError("validation_size + test_size must be less than 1")
        return self


class PathConfig(BaseModel):
    max_hops: int = Field(default=4, ge=1, le=6)
    max_gap_hours: int = Field(default=24, ge=1, le=720)
    max_paths: int = Field(default=20, ge=1, le=200)
    min_amount_ratio: float = Field(default=0.3, gt=0.0)
    max_amount_ratio: float = Field(default=3.0, gt=0.0)

    @model_validator(mode="after")
    def validate_amount_ratio(self) -> PathConfig:
        if self.min_amount_ratio > self.max_amount_ratio:
            raise ValueError("min_amount_ratio must not exceed max_amount_ratio")
        return self


class AppConfig(BaseModel):
    schema_version: str = "1.0"
    source: SourceConfig
    storage: StorageConfig
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    snapshots: SnapshotConfig = Field(default_factory=SnapshotConfig)
    temporal: TemporalConfig = Field(default_factory=TemporalConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    paths: PathConfig = Field(default_factory=PathConfig)


def load_config(path: Path) -> AppConfig:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = AppConfig.model_validate(raw)
    base = path.parent.parent.resolve()
    config.storage.normalized_dir = _resolve(base, config.storage.normalized_dir)
    config.storage.feature_dir = _resolve(base, config.storage.feature_dir)
    config.storage.artifact_dir = _resolve(base, config.storage.artifact_dir)
    return config


def _resolve(base: Path, path: Path) -> Path:
    return path if path.is_absolute() else (base / path).resolve()
