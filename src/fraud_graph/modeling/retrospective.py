from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import joblib
import networkx as nx
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray
from pydantic import BaseModel, Field
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier, IsolationForest
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.neighbors import LocalOutlierFactor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, RobustScaler, StandardScaler

from fraud_graph.artifacts import atomic_artifact_directory
from fraud_graph.data.manifest import DataManifest
from fraud_graph.errors import ArtifactNotReadyError, ProductionSafetyError
from fraud_graph.features.builder import BEHAVIOR_FEATURES, PROFILE_FEATURES, FeatureManifest
from fraud_graph.graph.snapshots import GraphSnapshotSetManifest
from fraud_graph.modeling.evaluation import BinaryMetrics, evaluate_binary_scores
from fraud_graph.modeling.rule_baseline import build_rule_scores

RETROSPECTIVE_IMPLEMENTATION_VERSION = "static-hybrid-v5"
RETROSPECTIVE_EVALUATION_SCHEME = "locked-account-holdout-static-transductive-graph"
CandidateAlgorithm = Literal["logistic", "hist_gradient_boosting"]
FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]

GRAPH_FEATURES = [
    "graph_in_degree",
    "graph_out_degree",
    "graph_total_degree",
    "graph_in_transaction_count",
    "graph_out_transaction_count",
    "graph_transaction_count",
    "graph_in_amount_abs_sum",
    "graph_out_amount_abs_sum",
    "graph_amount_abs_sum",
    "graph_flow_through_ratio",
    "graph_reciprocal_partner_count",
    "graph_reciprocal_partner_ratio",
    "graph_in_amount_hhi",
    "graph_out_amount_hhi",
    "graph_in_max_amount_share",
    "graph_out_max_amount_share",
    "graph_pagerank",
    "graph_core_number",
    "graph_component_size",
    "graph_community_size",
    "graph_neighbor_degree_mean",
]

TEMPORAL_FEATURES = [
    "temporal_active_ratio",
    "temporal_active_state_changes",
    "temporal_txn_mean",
    "temporal_txn_std",
    "temporal_txn_max",
    "temporal_txn_cv",
    "temporal_txn_trend",
    "temporal_txn_change_intensity",
    "temporal_recent_early_txn_ratio",
    "temporal_degree_mean",
    "temporal_degree_std",
    "temporal_degree_max",
    "temporal_degree_cv",
    "temporal_degree_trend",
    "temporal_amount_mean",
    "temporal_amount_std",
    "temporal_amount_max",
    "temporal_amount_cv",
    "temporal_amount_trend",
    "temporal_net_flow_sign_changes",
]

ANOMALY_FEATURES = [
    "anomaly_isolation_forest",
    "anomaly_local_outlier",
    "anomaly_community_deviation",
    "anomaly_temporal_change",
    "anomaly_ensemble_score",
]

COMMUNITY_ANOMALY_FEATURES = [
    "txn_count_total",
    "counterparty_count_total",
    "amount_abs_total",
    "same_day_turnover_ratio_avg",
    "graph_total_degree",
    "graph_flow_through_ratio",
    "graph_reciprocal_partner_ratio",
    "graph_pagerank",
    "temporal_active_ratio",
    "temporal_txn_cv",
    "temporal_txn_trend",
]


class SplitSummary(BaseModel):
    rows: int
    positives: int
    active_rows: int
    active_positives: int


class BootstrapMetricSummary(BaseModel):
    confidence_level: float = 0.95
    replicates: int
    roc_auc: tuple[float, float]
    pr_auc: tuple[float, float]
    recall_at_top_fraction: tuple[float, float]


class RetrospectiveExperimentMetadata(BaseModel):
    schema_version: str = "1.0"
    implementation_version: str = RETROSPECTIVE_IMPLEMENTATION_VERSION
    experiment_version: str
    data_version: str
    feature_version: str
    snapshot_set_version: str
    created_at: datetime
    target_definition: str = "account label is in positive_labels over the full observation period"
    training_mode: Literal["retrospective_static"] = "retrospective_static"
    evaluation_scheme: str = RETROSPECTIVE_EVALUATION_SCHEME
    production_eligible: bool = False
    positive_labels: list[int]
    random_seed: int
    top_fraction: float
    split_strategy: str
    split_fractions: dict[str, float]
    split_summary: dict[str, SplitSummary]
    population_diagnostics: dict[str, int | float]
    feature_groups: dict[str, list[str]]
    validation_metrics: dict[str, BinaryMetrics]
    selection_eligible_candidates: list[str]
    selected_candidate: str
    evidence_candidate: str
    selection_metric: str
    test_metrics: dict[str, BinaryMetrics]
    test_confidence_intervals: dict[str, BootstrapMetricSummary]
    eligibility_blockers: list[str] = Field(default_factory=list)
    files: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> RetrospectiveExperimentMetadata:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    algorithm: CandidateAlgorithm
    feature_columns: tuple[str, ...]
    selection_eligible: bool = True
    uses_profile: bool = False


class FittedAnomalyScorer:
    """Train-only anomaly models with empirical percentile normalization."""

    def __init__(self, random_seed: int, min_community_size: int = 20) -> None:
        self.random_seed = random_seed
        self.min_community_size = min_community_size
        self.base_columns = [*BEHAVIOR_FEATURES, *GRAPH_FEATURES, *TEMPORAL_FEATURES]
        self.imputer = SimpleImputer(strategy="median")
        self.scaler = RobustScaler(quantile_range=(10.0, 90.0))
        self.isolation_forest = IsolationForest(
            n_estimators=300,
            max_samples="auto",
            contamination="auto",
            random_state=random_seed,
            n_jobs=-1,
        )
        self.local_outlier: LocalOutlierFactor | None = None
        self.score_references: dict[str, FloatArray] = {}
        self.global_community_center: FloatArray | None = None
        self.global_community_scale: FloatArray | None = None
        self.community_stats: dict[int, tuple[FloatArray, FloatArray]] = {}
        self.temporal_center: FloatArray | None = None
        self.temporal_scale: FloatArray | None = None

    def fit(self, frame: pd.DataFrame) -> FittedAnomalyScorer:
        _require_columns(frame, [*self.base_columns, "graph_community_id"])
        matrix = self._base_matrix_fit(frame)
        self.isolation_forest.fit(matrix)
        if len(frame) >= 3:
            neighbors = min(35, len(frame) - 1)
            self.local_outlier = LocalOutlierFactor(
                n_neighbors=neighbors,
                novelty=True,
                contamination="auto",
                n_jobs=-1,
            )
            self.local_outlier.fit(matrix)

        community_matrix = _numeric_matrix(frame, COMMUNITY_ANOMALY_FEATURES)
        self.global_community_center, self.global_community_scale = _median_mad(
            community_matrix
        )
        communities = pd.to_numeric(frame["graph_community_id"], errors="coerce").fillna(-1)
        for community_id in sorted(int(value) for value in communities.unique()):
            mask = communities.to_numpy(dtype=int) == community_id
            if int(mask.sum()) < self.min_community_size:
                continue
            self.community_stats[community_id] = _median_mad(community_matrix[mask])

        temporal_matrix = _numeric_matrix(frame, TEMPORAL_FEATURES)
        self.temporal_center, self.temporal_scale = _median_mad(temporal_matrix)
        raw = self._raw_scores(frame, matrix)
        self.score_references = {
            name: np.sort(values.astype(np.float64, copy=False)) for name, values in raw.items()
        }
        return self

    def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
        if not self.score_references:
            raise RuntimeError("anomaly scorer must be fitted before transform")
        matrix = self._base_matrix_transform(frame)
        raw = self._raw_scores(frame, matrix)
        normalized = {
            name: _empirical_percentile(values, self.score_references[name])
            for name, values in raw.items()
        }
        result = pd.DataFrame(index=frame.index)
        result["anomaly_isolation_forest"] = normalized["isolation_forest"]
        result["anomaly_local_outlier"] = normalized["local_outlier"]
        result["anomaly_community_deviation"] = normalized["community_deviation"]
        result["anomaly_temporal_change"] = normalized["temporal_change"]
        result["anomaly_ensemble_score"] = result[ANOMALY_FEATURES[:-1]].mean(axis=1)
        return result

    def _base_matrix_fit(self, frame: pd.DataFrame) -> FloatArray:
        raw = _numeric_frame(frame, self.base_columns)
        imputed = self.imputer.fit_transform(raw)
        matrix = np.asarray(self.scaler.fit_transform(imputed), dtype=np.float64)
        return matrix

    def _base_matrix_transform(self, frame: pd.DataFrame) -> FloatArray:
        raw = _numeric_frame(frame, self.base_columns)
        imputed = self.imputer.transform(raw)
        matrix = np.asarray(self.scaler.transform(imputed), dtype=np.float64)
        return matrix

    def _raw_scores(self, frame: pd.DataFrame, matrix: FloatArray) -> dict[str, FloatArray]:
        isolation = -self.isolation_forest.decision_function(matrix)
        local = (
            -self.local_outlier.decision_function(matrix)
            if self.local_outlier is not None
            else np.zeros(len(frame), dtype=np.float64)
        )
        return {
            "isolation_forest": isolation,
            "local_outlier": local,
            "community_deviation": self._community_deviation(frame),
            "temporal_change": self._temporal_deviation(frame),
        }

    def _community_deviation(self, frame: pd.DataFrame) -> FloatArray:
        if self.global_community_center is None or self.global_community_scale is None:
            raise RuntimeError("community anomaly statistics are unavailable")
        values = _numeric_matrix(frame, COMMUNITY_ANOMALY_FEATURES)
        communities = (
            pd.to_numeric(frame["graph_community_id"], errors="coerce")
            .fillna(-1)
            .to_numpy(dtype=int)
        )
        result = np.empty(len(frame), dtype=np.float64)
        for index, community_id in enumerate(communities):
            center, scale = self.community_stats.get(
                int(community_id),
                (self.global_community_center, self.global_community_scale),
            )
            result[index] = float(np.mean(np.abs((values[index] - center) / scale)))
        return result

    def _temporal_deviation(self, frame: pd.DataFrame) -> FloatArray:
        if self.temporal_center is None or self.temporal_scale is None:
            raise RuntimeError("temporal anomaly statistics are unavailable")
        values = _numeric_matrix(frame, TEMPORAL_FEATURES)
        deviation = np.abs((values - self.temporal_center) / self.temporal_scale)
        result = np.asarray(np.mean(deviation, axis=1), dtype=np.float64)
        return result


def run_retrospective_experiment(
    normalized_root: Path,
    feature_root: Path,
    artifact_root: Path,
    feature_version: str,
    snapshot_set_version: str,
    *,
    positive_labels: list[int],
    allow_static_labels: bool = False,
    random_seed: int = 20260807,
    validation_fraction: float = 0.2,
    test_fraction: float = 0.2,
    top_fraction: float = 0.05,
) -> RetrospectiveExperimentMetadata:
    if not allow_static_labels:
        raise ProductionSafetyError(
            "static retrospective training requires the explicit allow_static_labels override"
        )
    if validation_fraction <= 0 or test_fraction <= 0:
        raise ValueError("validation_fraction and test_fraction must be positive")
    if validation_fraction + test_fraction >= 1.0:
        raise ValueError("validation_fraction + test_fraction must be less than 1")

    feature_dir = feature_root / feature_version
    feature_manifest = FeatureManifest.load(feature_dir / "manifest.json")
    data_dir = normalized_root / feature_manifest.data_version
    data_manifest = DataManifest.load(data_dir / "manifest.json")
    snapshot_dir = artifact_root / "graph_snapshots" / snapshot_set_version
    snapshot_manifest = GraphSnapshotSetManifest.load(snapshot_dir / "manifest.json")
    if snapshot_manifest.data_version != feature_manifest.data_version:
        raise ArtifactNotReadyError("feature and graph snapshot data versions differ")
    if data_manifest.temporal_training_ready:
        raise ProductionSafetyError(
            "temporal labels are available; use the temporal trainer instead of static replay"
        )

    experiment_material = json.dumps(
        {
            "implementation": RETROSPECTIVE_IMPLEMENTATION_VERSION,
            "feature_version": feature_version,
            "snapshot_set_version": snapshot_set_version,
            "positive_labels": sorted(positive_labels),
            "random_seed": random_seed,
            "validation_fraction": validation_fraction,
            "test_fraction": test_fraction,
            "top_fraction": top_fraction,
        },
        sort_keys=True,
    )
    experiment_version = hashlib.sha256(experiment_material.encode()).hexdigest()[:16]
    output_dir = artifact_root / "retrospective_experiments" / experiment_version
    metadata_path = output_dir / "metadata.json"
    if metadata_path.exists():
        return RetrospectiveExperimentMetadata.load(metadata_path)
    if output_dir.exists():
        raise ArtifactNotReadyError(
            f"incomplete retrospective experiment artifact exists: {output_dir}"
        )

    raw_features = pq.read_table(feature_dir / feature_manifest.file).to_pandas()
    raw_features["account_id"] = raw_features["account_id"].astype(str)
    graph_features = build_graph_features(snapshot_dir, snapshot_manifest, random_seed)
    temporal_features = build_snapshot_change_features(snapshot_dir, snapshot_manifest)
    base_features = _merge_feature_groups(raw_features, graph_features, temporal_features)
    labels = pq.read_table(data_dir / "labels.parquet", columns=["account_id", "label"]).to_pandas()
    labels["account_id"] = labels["account_id"].astype(str)
    labels["target"] = labels["label"].isin(positive_labels).astype(np.int8)
    modeling_frame = base_features.merge(
        labels[["account_id", "target"]], on="account_id", how="inner", validate="one_to_one"
    )
    if len(modeling_frame) != data_manifest.account_rows:
        raise ArtifactNotReadyError("feature and label account coverage differs")

    split_frame = build_locked_account_split(
        modeling_frame,
        random_seed=random_seed,
        validation_fraction=validation_fraction,
        test_fraction=test_fraction,
    )
    modeling_frame = modeling_frame.merge(
        split_frame[["account_id", "split"]], on="account_id", validate="one_to_one"
    )
    train_mask = modeling_frame["split"].eq("train").to_numpy()
    validation_mask = modeling_frame["split"].eq("validation").to_numpy()
    test_mask = modeling_frame["split"].eq("test").to_numpy()
    _validate_splits(modeling_frame, train_mask, validation_mask, test_mask)

    validation_anomaly_scorer = FittedAnomalyScorer(random_seed).fit(
        modeling_frame.loc[train_mask]
    )
    validation_frame = _with_anomaly_scores(modeling_frame, validation_anomaly_scorer)
    candidate_specs = _candidate_specs()
    validation_metrics, validation_predictions, fitted_candidates = _fit_validation_candidates(
        validation_frame,
        candidate_specs,
        train_mask,
        validation_mask,
        random_seed,
        top_fraction,
    )
    rule_scores = build_rule_scores(validation_frame[["account_id", *BEHAVIOR_FEATURES]])
    rule_by_account = rule_scores.set_index("account_id")["rule_score"]
    validation_target = validation_frame.loc[validation_mask, "target"].to_numpy(dtype=np.int8)
    validation_accounts = validation_frame.loc[validation_mask, "account_id"]
    validation_rule = validation_accounts.map(rule_by_account).to_numpy(dtype=np.float64)
    validation_unsupervised = validation_frame.loc[
        validation_mask, "anomaly_ensemble_score"
    ].to_numpy(dtype=np.float64)
    validation_metrics["rule_baseline"] = evaluate_binary_scores(
        validation_target, validation_rule, top_fraction=top_fraction
    )
    validation_metrics["unsupervised_ensemble"] = evaluate_binary_scores(
        validation_target, validation_unsupervised, top_fraction=top_fraction
    )
    validation_predictions["rule_baseline_score"] = validation_rule
    validation_predictions["unsupervised_ensemble_score"] = validation_unsupervised

    eligible = [spec.name for spec in candidate_specs if spec.selection_eligible]
    selected_candidate = max(
        eligible,
        key=lambda name: _selection_key(validation_metrics[name]),
    )
    selected_spec = next(spec for spec in candidate_specs if spec.name == selected_candidate)
    evidence_candidates = [
        spec.name
        for spec in candidate_specs
        if spec.selection_eligible and not spec.uses_profile
    ]
    evidence_candidate = max(
        evidence_candidates,
        key=lambda name: _selection_key(validation_metrics[name]),
    )
    evidence_spec = next(spec for spec in candidate_specs if spec.name == evidence_candidate)

    development_mask = train_mask | validation_mask
    final_anomaly_scorer = FittedAnomalyScorer(random_seed).fit(
        modeling_frame.loc[development_mask]
    )
    final_evaluation_frame = _with_anomaly_scores(modeling_frame, final_anomaly_scorer)
    selected_model = _make_candidate_pipeline(selected_spec, random_seed)
    selected_model.fit(
        final_evaluation_frame.loc[development_mask, list(selected_spec.feature_columns)],
        final_evaluation_frame.loc[development_mask, "target"].to_numpy(dtype=np.int8),
    )
    evidence_model = _make_candidate_pipeline(evidence_spec, random_seed)
    evidence_model.fit(
        final_evaluation_frame.loc[development_mask, list(evidence_spec.feature_columns)],
        final_evaluation_frame.loc[development_mask, "target"].to_numpy(dtype=np.int8),
    )
    behavior_spec = next(spec for spec in candidate_specs if spec.name == "behavior_logistic")
    behavior_model = _make_candidate_pipeline(behavior_spec, random_seed)
    behavior_model.fit(
        final_evaluation_frame.loc[development_mask, list(behavior_spec.feature_columns)],
        final_evaluation_frame.loc[development_mask, "target"].to_numpy(dtype=np.int8),
    )
    profile_spec = next(spec for spec in candidate_specs if spec.name == "profile_logistic")
    profile_model = _make_candidate_pipeline(profile_spec, random_seed)
    profile_model.fit(
        final_evaluation_frame.loc[development_mask, list(profile_spec.feature_columns)],
        final_evaluation_frame.loc[development_mask, "target"].to_numpy(dtype=np.int8),
    )
    behavior_profile_spec = next(
        spec for spec in candidate_specs if spec.name == "behavior_profile_logistic"
    )
    behavior_profile_model = _make_candidate_pipeline(behavior_profile_spec, random_seed)
    behavior_profile_model.fit(
        final_evaluation_frame.loc[
            development_mask, list(behavior_profile_spec.feature_columns)
        ],
        final_evaluation_frame.loc[development_mask, "target"].to_numpy(dtype=np.int8),
    )
    test_target = final_evaluation_frame.loc[test_mask, "target"].to_numpy(dtype=np.int8)
    selected_test_scores = selected_model.predict_proba(
        final_evaluation_frame.loc[test_mask, list(selected_spec.feature_columns)]
    )[:, 1]
    evidence_test_scores = evidence_model.predict_proba(
        final_evaluation_frame.loc[test_mask, list(evidence_spec.feature_columns)]
    )[:, 1]
    behavior_test_scores = behavior_model.predict_proba(
        final_evaluation_frame.loc[test_mask, list(behavior_spec.feature_columns)]
    )[:, 1]
    profile_test_scores = profile_model.predict_proba(
        final_evaluation_frame.loc[test_mask, list(profile_spec.feature_columns)]
    )[:, 1]
    behavior_profile_test_scores = behavior_profile_model.predict_proba(
        final_evaluation_frame.loc[test_mask, list(behavior_profile_spec.feature_columns)]
    )[:, 1]
    test_accounts = final_evaluation_frame.loc[test_mask, "account_id"]
    test_rule_scores = test_accounts.map(rule_by_account).to_numpy(dtype=np.float64)
    test_unsupervised_scores = final_evaluation_frame.loc[
        test_mask, "anomaly_ensemble_score"
    ].to_numpy(dtype=np.float64)
    test_metrics = {
        "selected_model": evaluate_binary_scores(
            test_target, selected_test_scores, top_fraction=top_fraction
        ),
        "evidence_model": evaluate_binary_scores(
            test_target, evidence_test_scores, top_fraction=top_fraction
        ),
        "behavior_baseline": evaluate_binary_scores(
            test_target, behavior_test_scores, top_fraction=top_fraction
        ),
        "profile_baseline": evaluate_binary_scores(
            test_target, profile_test_scores, top_fraction=top_fraction
        ),
        "behavior_profile_baseline": evaluate_binary_scores(
            test_target, behavior_profile_test_scores, top_fraction=top_fraction
        ),
        "rule_baseline": evaluate_binary_scores(
            test_target, test_rule_scores, top_fraction=top_fraction
        ),
        "unsupervised_ensemble": evaluate_binary_scores(
            test_target, test_unsupervised_scores, top_fraction=top_fraction
        ),
    }
    test_score_vectors = {
        "selected_model": selected_test_scores,
        "evidence_model": evidence_test_scores,
        "behavior_baseline": behavior_test_scores,
        "profile_baseline": profile_test_scores,
        "behavior_profile_baseline": behavior_profile_test_scores,
        "rule_baseline": test_rule_scores,
        "unsupervised_ensemble": test_unsupervised_scores,
    }
    test_confidence_intervals = {
        name: _stratified_bootstrap_metrics(
            test_target,
            scores,
            top_fraction=top_fraction,
            random_seed=random_seed + index,
        )
        for index, (name, scores) in enumerate(test_score_vectors.items())
    }
    test_predictions = pd.DataFrame(
        {
            "account_id": test_accounts.to_numpy(),
            "target": test_target,
            "selected_model_score": selected_test_scores,
            "evidence_model_score": evidence_test_scores,
            "behavior_baseline_score": behavior_test_scores,
            "profile_baseline_score": profile_test_scores,
            "behavior_profile_baseline_score": behavior_profile_test_scores,
            "rule_baseline_score": test_rule_scores,
            "unsupervised_ensemble_score": test_unsupervised_scores,
        }
    )

    scoring_anomaly_scorer = FittedAnomalyScorer(random_seed).fit(modeling_frame)
    scoring_frame = _with_anomaly_scores(modeling_frame, scoring_anomaly_scorer)
    scoring_model = _make_candidate_pipeline(selected_spec, random_seed)
    scoring_model.fit(
        scoring_frame[list(selected_spec.feature_columns)],
        scoring_frame["target"].to_numpy(dtype=np.int8),
    )
    account_scores = scoring_model.predict_proba(
        scoring_frame[list(selected_spec.feature_columns)]
    )[:, 1]
    evidence_scoring_model = _make_candidate_pipeline(evidence_spec, random_seed)
    evidence_scoring_model.fit(
        scoring_frame[list(evidence_spec.feature_columns)],
        scoring_frame["target"].to_numpy(dtype=np.int8),
    )
    evidence_account_scores = evidence_scoring_model.predict_proba(
        scoring_frame[list(evidence_spec.feature_columns)]
    )[:, 1]
    score_order = np.argsort(-account_scores, kind="stable")
    score_ranks = np.empty(len(scoring_frame), dtype=np.int64)
    score_ranks[score_order] = np.arange(1, len(scoring_frame) + 1)
    scores_frame = pd.DataFrame(
        {
            "account_id": scoring_frame["account_id"].to_numpy(),
            "risk_score": account_scores,
            "evidence_score": evidence_account_scores,
            "risk_rank": score_ranks,
            "has_transactions": scoring_frame["has_transactions"].astype(bool).to_numpy(),
            "anomaly_score": scoring_frame["anomaly_ensemble_score"].to_numpy(),
            "model_name": selected_candidate,
            "model_training_mode": "retrospective_static",
            "production_eligible": False,
        }
    )

    split_summary = _split_summary(modeling_frame)
    population_diagnostics = _population_diagnostics(modeling_frame)
    blockers = [
        "LABEL_TIME_MISSING",
        "RETROSPECTIVE_STATIC_TARGET",
        "TRANSDUCTIVE_GRAPH_EVALUATION",
        "MODEL_APPROVAL_REQUIRED",
    ]
    if population_diagnostics["active_positive_rows"] < 10:
        blockers.append("ACTIVE_POSITIVES_TOO_FEW")
    if selected_spec.uses_profile:
        blockers.append("PROFILE_SHORTCUT_DEPENDENCY")
    files = {
        "split_assignments": "split_assignments.parquet",
        "enhanced_features": "enhanced_features.parquet",
        "validation_predictions": "validation_predictions.parquet",
        "test_predictions": "test_predictions.parquet",
        "scores": "scores.parquet",
        "evaluation_model": "evaluation_model.joblib",
        "scoring_model": "scoring_model.joblib",
        "feature_importance": "feature_importance.csv",
        "report": "report.md",
    }
    metadata = RetrospectiveExperimentMetadata(
        experiment_version=experiment_version,
        data_version=data_manifest.data_version,
        feature_version=feature_version,
        snapshot_set_version=snapshot_set_version,
        created_at=datetime.now(UTC),
        positive_labels=positive_labels,
        random_seed=random_seed,
        top_fraction=top_fraction,
        split_strategy="deterministic_target_and_activity_stratified_account_holdout",
        split_fractions={
            "train": 1.0 - validation_fraction - test_fraction,
            "validation": validation_fraction,
            "test": test_fraction,
        },
        split_summary=split_summary,
        population_diagnostics=population_diagnostics,
        feature_groups={
            "behavior": list(BEHAVIOR_FEATURES),
            "graph": list(GRAPH_FEATURES),
            "temporal_change": list(TEMPORAL_FEATURES),
            "unsupervised": list(ANOMALY_FEATURES),
            "profile_static_only": list(PROFILE_FEATURES),
        },
        validation_metrics=validation_metrics,
        selection_eligible_candidates=eligible,
        selected_candidate=selected_candidate,
        evidence_candidate=evidence_candidate,
        selection_metric="validation_pr_auc_then_top5_recall_then_roc_auc",
        test_metrics=test_metrics,
        test_confidence_intervals=test_confidence_intervals,
        eligibility_blockers=blockers,
        files=files,
    )
    with atomic_artifact_directory(output_dir) as temporary_dir:
        pq.write_table(
            pa.Table.from_pandas(split_frame, preserve_index=False),
            temporary_dir / files["split_assignments"],
            compression="zstd",
        )
        enhanced_output = scoring_frame.drop(columns=["target", "split"])
        pq.write_table(
            pa.Table.from_pandas(enhanced_output, preserve_index=False),
            temporary_dir / files["enhanced_features"],
            compression="zstd",
        )
        pq.write_table(
            pa.Table.from_pandas(validation_predictions, preserve_index=False),
            temporary_dir / files["validation_predictions"],
            compression="zstd",
        )
        pq.write_table(
            pa.Table.from_pandas(test_predictions, preserve_index=False),
            temporary_dir / files["test_predictions"],
            compression="zstd",
        )
        pq.write_table(
            pa.Table.from_pandas(scores_frame, preserve_index=False),
            temporary_dir / files["scores"],
            compression="zstd",
        )
        joblib.dump(
            {
                "anomaly_scorer": final_anomaly_scorer,
                "model": selected_model,
                "candidate": selected_spec,
                "evidence_model": evidence_model,
                "evidence_candidate": evidence_spec,
            },
            temporary_dir / files["evaluation_model"],
        )
        joblib.dump(
            {
                "anomaly_scorer": scoring_anomaly_scorer,
                "model": scoring_model,
                "candidate": selected_spec,
                "evidence_model": evidence_scoring_model,
                "evidence_candidate": evidence_spec,
            },
            temporary_dir / files["scoring_model"],
        )
        _linear_feature_importance(selected_model).to_csv(
            temporary_dir / files["feature_importance"], index=False
        )
        (temporary_dir / files["report"]).write_text(
            render_retrospective_report(metadata), encoding="utf-8"
        )
        (temporary_dir / "metadata.json").write_text(
            metadata.model_dump_json(indent=2), encoding="utf-8"
        )
    del fitted_candidates
    return metadata


def build_locked_account_split(
    frame: pd.DataFrame,
    *,
    random_seed: int,
    validation_fraction: float,
    test_fraction: float,
) -> pd.DataFrame:
    _require_columns(frame, ["account_id", "target", "has_transactions"])
    if frame["account_id"].astype(str).duplicated().any():
        raise ProductionSafetyError("account split requires unique account IDs")
    rng = np.random.default_rng(random_seed)
    output: list[pd.DataFrame] = []
    strata = frame.assign(
        _target=frame["target"].astype(int),
        _active=frame["has_transactions"].astype(bool).astype(int),
    )
    for _, group in strata.groupby(["_target", "_active"], sort=True):
        positions = np.arange(len(group))
        rng.shuffle(positions)
        shuffled = group.iloc[positions].copy()
        count = len(shuffled)
        validation_count = int(round(count * validation_fraction))
        test_count = int(round(count * test_fraction))
        if count >= 3:
            validation_count = max(1, validation_count)
            test_count = max(1, test_count)
        if validation_count + test_count >= count:
            overflow = validation_count + test_count - count + 1
            if test_count >= validation_count:
                test_count = max(0, test_count - overflow)
            else:
                validation_count = max(0, validation_count - overflow)
        splits = np.full(count, "train", dtype=object)
        splits[:validation_count] = "validation"
        splits[validation_count : validation_count + test_count] = "test"
        shuffled["split"] = splits
        output.append(shuffled[["account_id", "target", "has_transactions", "split"]])
    result = pd.concat(output, ignore_index=True)
    for target in sorted(result["target"].astype(int).unique()):
        for split in ("validation", "test"):
            target_mask = result["target"].astype(int).eq(target)
            if (target_mask & result["split"].eq(split)).any():
                continue
            donor = result.index[target_mask & result["split"].eq("train")]
            if len(donor) <= 1:
                raise ProductionSafetyError(
                    f"target class {target} is too small for a three-way account split"
                )
            result.loc[donor[0], "split"] = split
    result["account_id"] = result["account_id"].astype(str)
    return result.sort_values("account_id", kind="stable").reset_index(drop=True)


def build_graph_features(
    snapshot_dir: Path,
    manifest: GraphSnapshotSetManifest,
    random_seed: int,
) -> pd.DataFrame:
    nodes = pq.read_table(snapshot_dir / manifest.files["global_nodes"]).to_pandas()
    edges = pq.read_table(snapshot_dir / manifest.files["global_directed_edges"]).to_pandas()
    if set(nodes.columns) & {"label", "label_type", "risk_event_at", "label_available_at"}:
        raise ProductionSafetyError("labels entered graph feature input")
    nodes["account_id"] = nodes["account_id"].astype(str)
    edges["payer_account_id"] = edges["payer_account_id"].astype(str)
    edges["payee_account_id"] = edges["payee_account_id"].astype(str)

    result = pd.DataFrame({"account_id": nodes["account_id"]})
    result["graph_in_degree"] = nodes["in_degree"].astype(float)
    result["graph_out_degree"] = nodes["out_degree"].astype(float)
    result["graph_total_degree"] = result["graph_in_degree"] + result["graph_out_degree"]
    result["graph_in_transaction_count"] = nodes["in_transaction_count"].astype(float)
    result["graph_out_transaction_count"] = nodes["out_transaction_count"].astype(float)
    result["graph_transaction_count"] = (
        result["graph_in_transaction_count"] + result["graph_out_transaction_count"]
    )
    result["graph_in_amount_abs_sum"] = nodes["in_amount_abs_sum"].astype(float)
    result["graph_out_amount_abs_sum"] = nodes["out_amount_abs_sum"].astype(float)
    result["graph_amount_abs_sum"] = (
        result["graph_in_amount_abs_sum"] + result["graph_out_amount_abs_sum"]
    )
    result["graph_flow_through_ratio"] = (
        np.minimum(result["graph_in_amount_abs_sum"], result["graph_out_amount_abs_sum"])
        / (
            np.maximum(result["graph_in_amount_abs_sum"], result["graph_out_amount_abs_sum"])
            + 1.0
        )
    )

    non_self = edges.loc[edges["payer_account_id"] != edges["payee_account_id"]].copy()
    pair_set = set(zip(non_self["payer_account_id"], non_self["payee_account_id"], strict=False))
    reciprocal_mask = np.fromiter(
        (
            (payee, payer) in pair_set
            for payer, payee in zip(
                non_self["payer_account_id"], non_self["payee_account_id"], strict=False
            )
        ),
        dtype=bool,
        count=len(non_self),
    )
    reciprocal_count = (
        non_self.loc[reciprocal_mask]
        .groupby("payer_account_id", sort=False)
        .size()
        .astype(float)
    )
    result["graph_reciprocal_partner_count"] = (
        result["account_id"].map(reciprocal_count).fillna(0.0)
    )
    result["graph_reciprocal_partner_ratio"] = (
        result["graph_reciprocal_partner_count"] / (result["graph_out_degree"] + 1.0)
    )

    outgoing_concentration = _edge_concentration(
        non_self, "payer_account_id", "amount_abs_sum"
    ).rename(
        columns={
            "hhi": "graph_out_amount_hhi",
            "max_share": "graph_out_max_amount_share",
        }
    )
    incoming_concentration = _edge_concentration(
        non_self, "payee_account_id", "amount_abs_sum"
    ).rename(
        columns={
            "hhi": "graph_in_amount_hhi",
            "max_share": "graph_in_max_amount_share",
        }
    )
    result = result.merge(
        outgoing_concentration,
        left_on="account_id",
        right_index=True,
        how="left",
        validate="one_to_one",
    ).merge(
        incoming_concentration,
        left_on="account_id",
        right_index=True,
        how="left",
        validate="one_to_one",
    )

    directed = nx.DiGraph()
    directed.add_nodes_from(result["account_id"])
    undirected = nx.Graph()
    undirected.add_nodes_from(result["account_id"])
    for row in non_self.itertuples(index=False):
        payer = str(row.payer_account_id)
        payee = str(row.payee_account_id)
        weight = max(float(row.transaction_count), 1.0)
        directed.add_edge(payer, payee, weight=weight)
        if undirected.has_edge(payer, payee):
            undirected[payer][payee]["weight"] += weight
        else:
            undirected.add_edge(payer, payee, weight=weight)
    pagerank = nx.pagerank(directed, alpha=0.85, weight="weight", max_iter=200)
    core_number = nx.core_number(undirected)
    component_size: dict[str, int] = {}
    for component in nx.connected_components(undirected):
        size = len(component)
        component_size.update({str(account_id): size for account_id in component})
    communities = nx.community.louvain_communities(
        undirected,
        weight="weight",
        seed=random_seed,
    )
    community_id: dict[str, int] = {}
    community_size: dict[str, int] = {}
    for identifier, community in enumerate(communities):
        size = len(community)
        for account_id in community:
            community_id[str(account_id)] = identifier
            community_size[str(account_id)] = size
    neighbor_degree = nx.average_neighbor_degree(undirected)
    result["graph_pagerank"] = result["account_id"].map(pagerank).fillna(0.0)
    result["graph_core_number"] = result["account_id"].map(core_number).fillna(0).astype(float)
    result["graph_component_size"] = (
        result["account_id"].map(component_size).fillna(1).astype(float)
    )
    result["graph_community_id"] = (
        result["account_id"].map(community_id).fillna(-1).astype(np.int64)
    )
    result["graph_community_size"] = (
        result["account_id"].map(community_size).fillna(1).astype(float)
    )
    result["graph_neighbor_degree_mean"] = (
        result["account_id"].map(neighbor_degree).fillna(0.0)
    )
    result[[*GRAPH_FEATURES, "graph_community_id"]] = result[
        [*GRAPH_FEATURES, "graph_community_id"]
    ].fillna(0.0)
    return result[["account_id", *GRAPH_FEATURES, "graph_community_id"]]


def build_snapshot_change_features(
    snapshot_dir: Path,
    manifest: GraphSnapshotSetManifest,
) -> pd.DataFrame:
    included = [record for record in manifest.snapshots if record.split_status == "assigned"]
    if not included:
        included = [record for record in manifest.snapshots if record.split_status != "embargo"]
    if not included:
        raise ArtifactNotReadyError("no non-embargo graph snapshots are available")
    frames: list[pd.DataFrame] = []
    for index, record in enumerate(included):
        path = snapshot_dir / "snapshots" / record.snapshot_id / record.files["nodes"]
        frame = pq.read_table(
            path,
            columns=[
                "account_id",
                "is_active",
                "in_degree",
                "out_degree",
                "in_transaction_count",
                "out_transaction_count",
                "in_amount_abs_sum",
                "out_amount_abs_sum",
                "net_flow_amount",
            ],
        ).to_pandas()
        frame["account_id"] = frame["account_id"].astype(str)
        frame["snapshot_index"] = index
        frame["snapshot_txn"] = (
            frame["in_transaction_count"].astype(float)
            + frame["out_transaction_count"].astype(float)
        )
        frame["snapshot_degree"] = frame["in_degree"].astype(float) + frame[
            "out_degree"
        ].astype(float)
        frame["snapshot_amount"] = (
            frame["in_amount_abs_sum"].astype(float)
            + frame["out_amount_abs_sum"].astype(float)
        )
        frames.append(frame)
    history = pd.concat(frames, ignore_index=True)
    history = history.sort_values(["account_id", "snapshot_index"], kind="stable")
    grouped = history.groupby("account_id", sort=True)
    result = grouped.agg(
        temporal_active_ratio=("is_active", "mean"),
        temporal_txn_mean=("snapshot_txn", "mean"),
        temporal_txn_std=("snapshot_txn", "std"),
        temporal_txn_max=("snapshot_txn", "max"),
        temporal_degree_mean=("snapshot_degree", "mean"),
        temporal_degree_std=("snapshot_degree", "std"),
        temporal_degree_max=("snapshot_degree", "max"),
        temporal_amount_mean=("snapshot_amount", "mean"),
        temporal_amount_std=("snapshot_amount", "std"),
        temporal_amount_max=("snapshot_amount", "max"),
    ).fillna(0.0)
    for prefix in ("txn", "degree", "amount"):
        result[f"temporal_{prefix}_cv"] = result[f"temporal_{prefix}_std"] / (
            result[f"temporal_{prefix}_mean"] + 1.0
        )

    account_order = result.index.astype(str)
    x = np.arange(len(included), dtype=np.float64)
    centered_x = x - x.mean()
    denominator = float(np.square(centered_x).sum())
    pivots: dict[str, FloatArray] = {}
    for source, prefix in (
        ("snapshot_txn", "txn"),
        ("snapshot_degree", "degree"),
        ("snapshot_amount", "amount"),
    ):
        pivot = history.pivot(index="account_id", columns="snapshot_index", values=source).reindex(
            account_order
        )
        values = pivot.to_numpy(dtype=np.float64)
        pivots[prefix] = values
        result[f"temporal_{prefix}_trend"] = (
            values @ centered_x / denominator if denominator else np.zeros(len(values))
        )
    txn_values = pivots["txn"]
    result["temporal_txn_change_intensity"] = (
        np.mean(np.abs(np.diff(txn_values, axis=1)), axis=1)
        / (result["temporal_txn_mean"].to_numpy() + 1.0)
        if len(included) > 1
        else np.zeros(len(result))
    )
    edge_count = min(3, len(included))
    result["temporal_recent_early_txn_ratio"] = (
        np.mean(txn_values[:, -edge_count:], axis=1) + 1.0
    ) / (np.mean(txn_values[:, :edge_count], axis=1) + 1.0)

    active_pivot = history.pivot(
        index="account_id", columns="snapshot_index", values="is_active"
    ).reindex(account_order)
    active_values = active_pivot.to_numpy(dtype=np.int8)
    result["temporal_active_state_changes"] = (
        np.count_nonzero(np.diff(active_values, axis=1), axis=1).astype(float)
        if len(included) > 1
        else np.zeros(len(result))
    )
    flow_pivot = history.pivot(
        index="account_id", columns="snapshot_index", values="net_flow_amount"
    ).reindex(account_order)
    flow_sign = np.sign(flow_pivot.to_numpy(dtype=np.float64))
    result["temporal_net_flow_sign_changes"] = (
        np.count_nonzero(
            (flow_sign[:, 1:] != flow_sign[:, :-1])
            & (flow_sign[:, 1:] != 0)
            & (flow_sign[:, :-1] != 0),
            axis=1,
        ).astype(float)
        if len(included) > 1
        else np.zeros(len(result))
    )
    result = result.reset_index().rename(columns={"index": "account_id"})
    result["account_id"] = result["account_id"].astype(str)
    result[TEMPORAL_FEATURES] = result[TEMPORAL_FEATURES].replace(
        [np.inf, -np.inf], np.nan
    ).fillna(0.0)
    return result[["account_id", *TEMPORAL_FEATURES]]


def render_retrospective_report(metadata: RetrospectiveExperimentMetadata) -> str:
    lines = [
        "# 任务二：静态回溯混合建模实验",
        "",
        f"- 实验版本：`{metadata.experiment_version}`",
        f"- 数据版本：`{metadata.data_version}`",
        f"- 图快照版本：`{metadata.snapshot_set_version}`",
        "- 口径：账户级固定训练/验证/测试划分；标签仅用于划分、训练和评估。",
        "- 图口径：标签无关的全量比赛图 transductive 特征，不代表新账户 inductive 能力。",
        "- 限制：缺少标签发生与可得时间，因此结果不能解释为真实时序预警能力。",
        "",
        "## 数据诊断",
        "",
        f"- 账户数：{metadata.population_diagnostics['rows']}",
        f"- 涉诈账户数：{metadata.population_diagnostics['positive_rows']}",
        f"- 有交易的涉诈账户数：{metadata.population_diagnostics['active_positive_rows']}",
        f"- 涉诈账户交易覆盖率：{metadata.population_diagnostics['positive_active_rate']:.2%}",
        "",
        "## 验证集消融",
        "",
        "| 候选 | ROC-AUC | PR-AUC | Recall@Top5% | Lift@Top5% |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metric in metadata.validation_metrics.items():
        lines.append(_metric_row(name, metric))
    lines.extend(
        [
            "",
            f"静态比赛模型：`{metadata.selected_candidate}`",
            f"无画像证据模型：`{metadata.evidence_candidate}`",
            "",
            "## 锁定测试集",
            "",
            "| 模型 | ROC-AUC | PR-AUC | Recall@Top5% | Lift@Top5% |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for name, metric in metadata.test_metrics.items():
        lines.append(_metric_row(name, metric))
    selected_interval = metadata.test_confidence_intervals["selected_model"]
    lines.extend(
        [
            "",
            "静态比赛模型 95% 分层 bootstrap 区间：",
            f"ROC-AUC `{_interval_text(selected_interval.roc_auc)}`，",
            f"PR-AUC `{_interval_text(selected_interval.pr_auc)}`，",
            "Recall@Top5% "
            f"`{_interval_text(selected_interval.recall_at_top_fraction)}`。",
            "",
            "## 使用边界",
            "",
            "该产物用于当前静态数据集上的回溯比较和案例排序，",
            "`production_eligible=false`。获得标签时间后必须重新进行时序回测。",
            "",
        ]
    )
    return "\n".join(lines)


def _candidate_specs() -> list[CandidateSpec]:
    behavior = tuple(BEHAVIOR_FEATURES)
    graph = tuple(GRAPH_FEATURES)
    temporal = tuple(TEMPORAL_FEATURES)
    anomaly = tuple(ANOMALY_FEATURES)
    profile = tuple(PROFILE_FEATURES)
    structural = (*behavior, *graph, *temporal)
    hybrid = (*structural, *anomaly)
    return [
        CandidateSpec("behavior_logistic", "logistic", behavior),
        CandidateSpec("graph_logistic", "logistic", graph),
        CandidateSpec("behavior_graph_logistic", "logistic", (*behavior, *graph)),
        CandidateSpec("behavior_graph_temporal_logistic", "logistic", structural),
        CandidateSpec("hybrid_logistic", "logistic", hybrid),
        CandidateSpec("hybrid_hist_gradient_boosting", "hist_gradient_boosting", hybrid),
        CandidateSpec("profile_logistic", "logistic", profile, uses_profile=True),
        CandidateSpec(
            "behavior_profile_logistic",
            "logistic",
            (*behavior, *profile),
            uses_profile=True,
        ),
        CandidateSpec(
            "hybrid_profile_logistic",
            "logistic",
            (*hybrid, *profile),
            uses_profile=True,
        ),
    ]


def _fit_validation_candidates(
    frame: pd.DataFrame,
    specs: list[CandidateSpec],
    train_mask: BoolArray,
    validation_mask: BoolArray,
    random_seed: int,
    top_fraction: float,
) -> tuple[dict[str, BinaryMetrics], pd.DataFrame, dict[str, Pipeline]]:
    target_train = frame.loc[train_mask, "target"].to_numpy(dtype=np.int8)
    target_validation = frame.loc[validation_mask, "target"].to_numpy(dtype=np.int8)
    predictions = pd.DataFrame(
        {
            "account_id": frame.loc[validation_mask, "account_id"].to_numpy(),
            "target": target_validation,
        }
    )
    metrics: dict[str, BinaryMetrics] = {}
    models: dict[str, Pipeline] = {}
    for spec in specs:
        model = _make_candidate_pipeline(spec, random_seed)
        columns = list(spec.feature_columns)
        model.fit(frame.loc[train_mask, columns], target_train)
        scores = model.predict_proba(frame.loc[validation_mask, columns])[:, 1]
        metrics[spec.name] = evaluate_binary_scores(
            target_validation, scores, top_fraction=top_fraction
        )
        predictions[f"{spec.name}_score"] = scores
        models[spec.name] = model
    return metrics, predictions, models


def _make_candidate_pipeline(spec: CandidateSpec, random_seed: int) -> Pipeline:
    columns = list(spec.feature_columns)
    categorical = [
        column for column in ("region_code", "customer_type") if column in columns
    ]
    numeric = [column for column in columns if column not in categorical]
    if spec.algorithm == "hist_gradient_boosting":
        if categorical:
            raise ValueError("histogram candidate does not support raw profile categories")
        return Pipeline(
            [
                ("impute", SimpleImputer(strategy="median")),
                (
                    "classifier",
                    HistGradientBoostingClassifier(
                        learning_rate=0.05,
                        max_iter=250,
                        max_leaf_nodes=15,
                        min_samples_leaf=20,
                        l2_regularization=1.0,
                        class_weight="balanced",
                        early_stopping=False,
                        random_state=random_seed,
                    ),
                ),
            ]
        )
    signed_log = FunctionTransformer(_signed_log_transform, feature_names_out="one-to-one")
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
    return Pipeline(
        [
            ("preprocess", ColumnTransformer(transformers, remainder="drop")),
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


def _merge_feature_groups(
    behavior: pd.DataFrame,
    graph: pd.DataFrame,
    temporal: pd.DataFrame,
) -> pd.DataFrame:
    for frame in (behavior, graph, temporal):
        if frame["account_id"].astype(str).duplicated().any():
            raise ArtifactNotReadyError("feature group contains duplicate account IDs")
    merged = behavior.merge(graph, on="account_id", how="inner", validate="one_to_one").merge(
        temporal, on="account_id", how="inner", validate="one_to_one"
    )
    if len(merged) != len(behavior):
        raise ArtifactNotReadyError("behavior, graph, and temporal feature coverage differs")
    return merged


def _with_anomaly_scores(
    frame: pd.DataFrame,
    scorer: FittedAnomalyScorer,
) -> pd.DataFrame:
    output = frame.copy()
    scores = scorer.transform(output)
    for column in ANOMALY_FEATURES:
        output[column] = scores[column].to_numpy(dtype=np.float64)
    return output


def _edge_concentration(
    edges: pd.DataFrame,
    account_column: str,
    value_column: str,
) -> pd.DataFrame:
    values = pd.to_numeric(edges[value_column], errors="coerce").abs().fillna(0.0)
    work = pd.DataFrame({"account_id": edges[account_column].astype(str), "value": values})
    totals = work.groupby("account_id", sort=False)["value"].transform("sum")
    counts = work.groupby("account_id", sort=False)["value"].transform("count")
    shares = np.where(totals.to_numpy() > 0, work["value"].to_numpy() / totals, 1.0 / counts)
    work["share"] = shares
    return work.groupby("account_id", sort=False).agg(
        hhi=("share", lambda value: float(np.square(value).sum())),
        max_share=("share", "max"),
    )


def _validate_splits(
    frame: pd.DataFrame,
    train_mask: BoolArray,
    validation_mask: BoolArray,
    test_mask: BoolArray,
) -> None:
    if not train_mask.any() or not validation_mask.any() or not test_mask.any():
        raise ProductionSafetyError("train, validation, and test splits must all be non-empty")
    for name, mask in (
        ("train", train_mask),
        ("validation", validation_mask),
        ("test", test_mask),
    ):
        if frame.loc[mask, "target"].nunique() != 2:
            raise ProductionSafetyError(f"{name} split must contain both target classes")


def _split_summary(frame: pd.DataFrame) -> dict[str, SplitSummary]:
    result: dict[str, SplitSummary] = {}
    for split in ("train", "validation", "test"):
        subset = frame.loc[frame["split"].eq(split)]
        active = subset["has_transactions"].astype(bool)
        target = subset["target"].astype(bool)
        result[split] = SplitSummary(
            rows=len(subset),
            positives=int(target.sum()),
            active_rows=int(active.sum()),
            active_positives=int((active & target).sum()),
        )
    return result


def _population_diagnostics(frame: pd.DataFrame) -> dict[str, int | float]:
    target = frame["target"].astype(bool)
    active = frame["has_transactions"].astype(bool)
    positive_rows = int(target.sum())
    active_positive_rows = int((target & active).sum())
    return {
        "rows": len(frame),
        "positive_rows": positive_rows,
        "active_rows": int(active.sum()),
        "active_positive_rows": active_positive_rows,
        "inactive_positive_rows": positive_rows - active_positive_rows,
        "prevalence": positive_rows / len(frame),
        "positive_active_rate": active_positive_rows / positive_rows if positive_rows else 0.0,
    }


def _selection_key(metrics: BinaryMetrics) -> tuple[float, float, float]:
    return (
        metrics.pr_auc if metrics.pr_auc is not None else -1.0,
        metrics.recall_at_top_fraction
        if metrics.recall_at_top_fraction is not None
        else -1.0,
        metrics.roc_auc if metrics.roc_auc is not None else -1.0,
    )


def _linear_feature_importance(model: Pipeline) -> pd.DataFrame:
    if "preprocess" not in model.named_steps or "classifier" not in model.named_steps:
        return pd.DataFrame(columns=["feature", "coefficient", "absolute_coefficient"])
    preprocessor = model.named_steps["preprocess"]
    classifier = model.named_steps["classifier"]
    if not hasattr(preprocessor, "get_feature_names_out") or not hasattr(classifier, "coef_"):
        return pd.DataFrame(columns=["feature", "coefficient", "absolute_coefficient"])
    names = [
        str(name).replace("numeric__", "").replace("categorical__", "")
        for name in preprocessor.get_feature_names_out()
    ]
    coefficients = np.asarray(classifier.coef_[0], dtype=np.float64)
    result = pd.DataFrame(
        {
            "feature": names,
            "coefficient": coefficients,
            "absolute_coefficient": np.abs(coefficients),
        }
    )
    return result.sort_values("absolute_coefficient", ascending=False, kind="stable")


def _stratified_bootstrap_metrics(
    target: NDArray[np.int8],
    scores: FloatArray,
    *,
    top_fraction: float,
    random_seed: int,
    replicates: int = 500,
) -> BootstrapMetricSummary:
    positive_indices = np.flatnonzero(target == 1)
    negative_indices = np.flatnonzero(target == 0)
    if len(positive_indices) == 0 or len(negative_indices) == 0:
        raise ProductionSafetyError("bootstrap confidence intervals require both target classes")
    rng = np.random.default_rng(random_seed)
    roc_values = np.empty(replicates, dtype=np.float64)
    pr_values = np.empty(replicates, dtype=np.float64)
    recall_values = np.empty(replicates, dtype=np.float64)
    for index in range(replicates):
        sampled = np.concatenate(
            [
                rng.choice(positive_indices, size=len(positive_indices), replace=True),
                rng.choice(negative_indices, size=len(negative_indices), replace=True),
            ]
        )
        rng.shuffle(sampled)
        metric = evaluate_binary_scores(
            target[sampled], scores[sampled], top_fraction=top_fraction
        )
        assert metric.roc_auc is not None
        assert metric.pr_auc is not None
        assert metric.recall_at_top_fraction is not None
        roc_values[index] = metric.roc_auc
        pr_values[index] = metric.pr_auc
        recall_values[index] = metric.recall_at_top_fraction
    return BootstrapMetricSummary(
        replicates=replicates,
        roc_auc=_percentile_interval(roc_values),
        pr_auc=_percentile_interval(pr_values),
        recall_at_top_fraction=_percentile_interval(recall_values),
    )


def _percentile_interval(values: FloatArray) -> tuple[float, float]:
    interval = np.quantile(values, [0.025, 0.975])
    return float(interval[0]), float(interval[1])


def _signed_log_transform(values: FloatArray) -> FloatArray:
    return np.asarray(np.sign(values) * np.log1p(np.abs(values)), dtype=np.float64)


def _numeric_frame(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    _require_columns(frame, columns)
    return frame[columns].apply(pd.to_numeric, errors="coerce").replace(
        [np.inf, -np.inf], np.nan
    )


def _numeric_matrix(frame: pd.DataFrame, columns: list[str]) -> FloatArray:
    numeric = _numeric_frame(frame, columns)
    medians = numeric.median(axis=0).fillna(0.0)
    matrix = np.asarray(numeric.fillna(medians).to_numpy(dtype=np.float64))
    return matrix


def _median_mad(values: FloatArray) -> tuple[FloatArray, FloatArray]:
    center = np.nanmedian(values, axis=0)
    absolute_deviation = np.abs(values - center)
    scale = 1.4826 * np.nanmedian(absolute_deviation, axis=0)
    scale = np.where(np.isfinite(scale) & (scale > 1e-9), scale, 1.0)
    return np.asarray(center, dtype=np.float64), np.asarray(scale, dtype=np.float64)


def _empirical_percentile(values: FloatArray, reference: FloatArray) -> FloatArray:
    if len(reference) == 0:
        return np.zeros(len(values), dtype=np.float64)
    ranks = np.searchsorted(reference, values, side="right")
    return np.asarray(ranks.astype(np.float64) / len(reference), dtype=np.float64)


def _require_columns(frame: pd.DataFrame, columns: list[str]) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ArtifactNotReadyError(f"required columns missing: {sorted(missing)}")


def _metric_row(name: str, metric: BinaryMetrics) -> str:
    def value(item: float | None) -> str:
        return "NA" if item is None else f"{item:.4f}"

    return (
        f"| {name} | {value(metric.roc_auc)} | {value(metric.pr_auc)} | "
        f"{value(metric.recall_at_top_fraction)} | {value(metric.lift_at_top_fraction)} |"
    )


def _interval_text(interval: tuple[float, float]) -> str:
    return f"{interval[0]:.4f}, {interval[1]:.4f}"
