from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import joblib
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, Field
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

from fraud_graph.artifacts import atomic_artifact_directory
from fraud_graph.data.manifest import DataManifest
from fraud_graph.errors import ArtifactNotReadyError, ProductionSafetyError
from fraud_graph.features.builder import (
    BEHAVIOR_FEATURES,
    PROFILE_FEATURES,
    FeatureManifest,
)
from fraud_graph.modeling.evaluation import evaluate_binary_scores

FeatureSet = Literal["behavior", "profile", "combined"]
MODEL_IMPLEMENTATION_VERSION = "logistic-reference-v3"
MODEL_EVALUATION_SCHEME = "stratified-oof-diagnostic"
SCORE_IMPLEMENTATION_VERSION = "batch-score-v3"


def _signed_log_transform(values: np.ndarray) -> np.ndarray:
    return np.sign(values) * np.log1p(np.abs(values))


class EvaluationMetrics(BaseModel):
    roc_auc: float
    pr_auc: float
    recall_at_top_fraction: float
    precision_at_top_fraction: float
    top_fraction: float
    prevalence: float
    positives: int
    rows: int
    lift_at_top_fraction: float | None = None
    brier_score: float | None = None
    cutoff: int | None = None
    evaluable: bool = True
    blockers: list[str] = Field(default_factory=list)


class ModelMetadata(BaseModel):
    schema_version: str = "1.0"
    implementation_version: str = "unknown"
    model_version: str
    feature_version: str
    data_version: str
    created_at: datetime
    training_mode: Literal["temporal", "retrospective_static"]
    production_eligible: bool
    positive_labels: list[int]
    feature_set: FeatureSet
    feature_columns: list[str]
    random_seed: int
    folds: int
    evaluation_scheme: str = "unknown"
    eligibility_blockers: list[str] = Field(default_factory=list)
    metrics: EvaluationMetrics
    model_file: str = "model.joblib"

    @classmethod
    def load(cls, path: Path) -> ModelMetadata:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class ScoreManifest(BaseModel):
    schema_version: str = "1.0"
    implementation_version: str = "unknown"
    score_version: str
    model_version: str
    feature_version: str
    data_version: str
    created_at: datetime
    feature_as_of: datetime
    row_count: int
    file: str = "scores.parquet"

    @classmethod
    def load(cls, path: Path) -> ScoreManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def train_risk_model(
    normalized_root: Path,
    feature_root: Path,
    artifact_root: Path,
    feature_version: str,
    *,
    positive_labels: list[int],
    feature_set: FeatureSet = "behavior",
    allow_static_labels: bool = False,
    random_seed: int = 20260807,
    folds: int = 5,
    top_fraction: float = 0.05,
) -> ModelMetadata:
    feature_dir = feature_root / feature_version
    feature_manifest = FeatureManifest.load(feature_dir / "manifest.json")
    data_dir = normalized_root / feature_manifest.data_version
    data_manifest = DataManifest.load(data_dir / "manifest.json")
    if not allow_static_labels:
        if not data_manifest.temporal_training_ready:
            raise ProductionSafetyError(
                "label_available_at is missing; pass allow_static_labels only for an explicitly "
                "retrospective, non-production model"
            )
        raise ProductionSafetyError(
            "the reference trainer uses diagnostic stratified folds; pass allow_static_labels "
            "only for a non-production comparison"
        )
    training_mode: Literal["temporal", "retrospective_static"] = "retrospective_static"
    eligibility_blockers = [
        "REFERENCE_MODEL_NOT_APPROVED",
        "NON_TEMPORAL_DIAGNOSTIC_EVALUATION",
    ]
    if not data_manifest.temporal_training_ready:
        eligibility_blockers.append("LABEL_TIME_MISSING")

    feature_columns = _feature_columns(feature_set)
    model_material = json.dumps(
        {
            "feature_version": feature_version,
            "positive_labels": positive_labels,
            "feature_set": feature_set,
            "random_seed": random_seed,
            "folds": folds,
            "implementation": MODEL_IMPLEMENTATION_VERSION,
            "evaluation_scheme": MODEL_EVALUATION_SCHEME,
        },
        sort_keys=True,
    )
    model_version = hashlib.sha256(model_material.encode()).hexdigest()[:16]
    output_dir = artifact_root / "models" / model_version
    metadata_path = output_dir / "metadata.json"
    if metadata_path.exists():
        return ModelMetadata.load(metadata_path)
    if output_dir.exists():
        raise ArtifactNotReadyError(
            f"incomplete model artifact already exists and requires quarantine: {output_dir}"
        )

    features = pq.read_table(feature_dir / feature_manifest.file).to_pandas()
    labels = pq.read_table(data_dir / "labels.parquet").to_pandas()
    frame = features.merge(labels[["account_id", "label"]], on="account_id", how="inner")
    if len(frame) != len(features):
        raise ArtifactNotReadyError("feature and label account coverage differs")
    target = frame["label"].isin(positive_labels).astype(np.int8).to_numpy()
    positives = int(target.sum())
    if positives < folds * 2:
        raise ProductionSafetyError(
            f"only {positives} positives are available; at least {folds * 2} are required"
        )

    pipeline = _make_pipeline(feature_set, feature_columns, random_seed)
    oof = np.zeros(len(frame), dtype=np.float64)
    splitter = StratifiedKFold(n_splits=folds, shuffle=True, random_state=random_seed)
    for train_index, validation_index in splitter.split(frame, target):
        pipeline.fit(frame.iloc[train_index][feature_columns], target[train_index])
        oof[validation_index] = pipeline.predict_proba(
            frame.iloc[validation_index][feature_columns]
        )[:, 1]
    metrics = _evaluate(target, oof, top_fraction)
    pipeline.fit(frame[feature_columns], target)

    metadata = ModelMetadata(
        implementation_version=MODEL_IMPLEMENTATION_VERSION,
        model_version=model_version,
        feature_version=feature_version,
        data_version=feature_manifest.data_version,
        created_at=datetime.now(UTC),
        training_mode=training_mode,
        production_eligible=False,
        positive_labels=positive_labels,
        feature_set=feature_set,
        feature_columns=feature_columns,
        random_seed=random_seed,
        folds=folds,
        evaluation_scheme=MODEL_EVALUATION_SCHEME,
        eligibility_blockers=eligibility_blockers,
        metrics=metrics,
    )
    with atomic_artifact_directory(output_dir) as temporary_dir:
        joblib.dump(pipeline, temporary_dir / "model.joblib")
        (temporary_dir / "metadata.json").write_text(
            metadata.model_dump_json(indent=2), encoding="utf-8"
        )
    return metadata


def score_accounts(
    feature_root: Path,
    artifact_root: Path,
    model_version: str,
) -> ScoreManifest:
    model_dir = artifact_root / "models" / model_version
    metadata = ModelMetadata.load(model_dir / "metadata.json")
    feature_dir = feature_root / metadata.feature_version
    feature_manifest = FeatureManifest.load(feature_dir / "manifest.json")
    score_material = f"{SCORE_IMPLEMENTATION_VERSION}|{model_version}|{metadata.feature_version}"
    score_version = hashlib.sha256(score_material.encode()).hexdigest()[:16]
    output_dir = artifact_root / "scores" / score_version
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        return ScoreManifest.load(manifest_path)
    if output_dir.exists():
        raise ArtifactNotReadyError(
            f"incomplete score artifact already exists and requires quarantine: {output_dir}"
        )

    frame = pq.read_table(feature_dir / feature_manifest.file).to_pandas()
    pipeline: Pipeline = joblib.load(model_dir / metadata.model_file)
    probabilities = pipeline.predict_proba(frame[metadata.feature_columns])[:, 1]
    order = np.argsort(-probabilities, kind="stable")
    ranks = np.empty(len(frame), dtype=np.int64)
    ranks[order] = np.arange(1, len(frame) + 1)

    output_table = pa.table(
        {
            "account_id": frame["account_id"].astype(str).tolist(),
            "risk_score": probabilities,
            "risk_rank": ranks,
            "model_version": [model_version] * len(frame),
            "data_version": [metadata.data_version] * len(frame),
            "feature_as_of": [feature_manifest.as_of] * len(frame),
            "has_transactions": frame["has_transactions"].astype(bool).tolist(),
            "model_training_mode": [metadata.training_mode] * len(frame),
            "model_feature_set": [metadata.feature_set] * len(frame),
            "production_eligible": [metadata.production_eligible] * len(frame),
        }
    )
    manifest = ScoreManifest(
        implementation_version=SCORE_IMPLEMENTATION_VERSION,
        score_version=score_version,
        model_version=model_version,
        feature_version=metadata.feature_version,
        data_version=metadata.data_version,
        created_at=datetime.now(UTC),
        feature_as_of=feature_manifest.as_of,
        row_count=len(frame),
    )
    with atomic_artifact_directory(output_dir) as temporary_dir:
        pq.write_table(output_table, temporary_dir / "scores.parquet", compression="zstd")
        (temporary_dir / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
    return manifest


def _feature_columns(feature_set: FeatureSet) -> list[str]:
    if feature_set == "behavior":
        return list(BEHAVIOR_FEATURES)
    if feature_set == "profile":
        return list(PROFILE_FEATURES)
    if feature_set == "combined":
        return [*BEHAVIOR_FEATURES, *PROFILE_FEATURES]
    raise ValueError(f"unsupported feature_set: {feature_set}")


def _make_pipeline(
    feature_set: FeatureSet,
    feature_columns: list[str],
    random_seed: int,
) -> Pipeline:
    categorical = [
        column for column in ("region_code", "customer_type") if column in feature_columns
    ]
    numeric = [column for column in feature_columns if column not in categorical]
    signed_log = FunctionTransformer(
        _signed_log_transform,
        feature_names_out="one-to-one",
    )
    numeric_pipeline = Pipeline(
        [
            ("impute", SimpleImputer(strategy="median")),
            ("signed_log", signed_log),
            ("scale", StandardScaler()),
        ]
    )
    transformers: list[tuple[str, Any, list[str]]] = [("numeric", numeric_pipeline, numeric)]
    if categorical:
        transformers.append(
            (
                "categorical",
                OneHotEncoder(handle_unknown="ignore", min_frequency=5),
                categorical,
            )
        )
    preprocessor = ColumnTransformer(transformers=transformers, remainder="drop")
    return Pipeline(
        [
            ("preprocess", preprocessor),
            (
                "classifier",
                LogisticRegression(
                    class_weight="balanced",
                    max_iter=2_000,
                    random_state=random_seed,
                    solver="liblinear",
                ),
            ),
        ]
    )


def _evaluate(target: np.ndarray, scores: np.ndarray, top_fraction: float) -> EvaluationMetrics:
    metrics = evaluate_binary_scores(target, scores, top_fraction=top_fraction)
    return EvaluationMetrics(
        roc_auc=metrics.roc_auc if metrics.roc_auc is not None else 0.0,
        pr_auc=metrics.pr_auc if metrics.pr_auc is not None else 0.0,
        recall_at_top_fraction=metrics.recall_at_top_fraction or 0.0,
        precision_at_top_fraction=metrics.precision_at_top_fraction or 0.0,
        top_fraction=metrics.top_fraction,
        prevalence=metrics.prevalence,
        positives=metrics.positives,
        rows=metrics.rows,
        lift_at_top_fraction=metrics.lift_at_top_fraction,
        brier_score=metrics.brier_score,
        cutoff=metrics.cutoff,
        evaluable=metrics.evaluable,
        blockers=metrics.blockers,
    )
