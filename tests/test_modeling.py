from __future__ import annotations

from pathlib import Path

import pytest

from fraud_graph.config import AppConfig
from fraud_graph.data.ingest import ingest_xlsx
from fraud_graph.errors import ProductionSafetyError
from fraud_graph.features.builder import build_account_features
from fraud_graph.graph.path_engine import PathSearchConfig
from fraud_graph.modeling.risk_model import score_accounts, train_risk_model
from fraud_graph.service.app import ServiceState


def test_static_label_training_requires_explicit_override(
    source_dir: Path, config: AppConfig
) -> None:
    data = ingest_xlsx(source_dir, config)
    features = build_account_features(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        data.data_version,
        lookback_days=30,
    )
    with pytest.raises(ProductionSafetyError, match="label_available_at"):
        train_risk_model(
            config.storage.normalized_dir,
            config.storage.feature_dir,
            config.storage.artifact_dir,
            features.feature_version,
            positive_labels=[1],
            folds=2,
        )


def test_retrospective_model_is_marked_not_production_eligible(
    source_dir: Path, config: AppConfig
) -> None:
    data = ingest_xlsx(source_dir, config)
    features = build_account_features(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        data.data_version,
        lookback_days=30,
    )
    model = train_risk_model(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        config.storage.artifact_dir,
        features.feature_version,
        positive_labels=[1],
        allow_static_labels=True,
        folds=2,
    )
    scores = score_accounts(
        config.storage.feature_dir,
        config.storage.artifact_dir,
        model.model_version,
    )
    repeated_model = train_risk_model(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        config.storage.artifact_dir,
        features.feature_version,
        positive_labels=[1],
        allow_static_labels=True,
        folds=2,
    )
    repeated_scores = score_accounts(
        config.storage.feature_dir,
        config.storage.artifact_dir,
        model.model_version,
    )
    service_state = ServiceState(
        config.storage.normalized_dir,
        config.storage.feature_dir,
        config.storage.artifact_dir,
        scores.score_version,
        PathSearchConfig(),
    )

    assert model.training_mode == "retrospective_static"
    assert model.production_eligible is False
    assert scores.row_count == 20
    assert repeated_model.created_at == model.created_at
    assert repeated_scores.created_at == scores.created_at
    assert service_state.score_manifest.score_version == scores.score_version
