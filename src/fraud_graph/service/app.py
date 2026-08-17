from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as pq
from fastapi import Cookie, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from fraud_graph.agent import (
    AgentCapabilities,
    AgentQuery,
    AgentResponse,
    AgentService,
    AgentSessionCreate,
    AgentSessionStatus,
    AgentSessionStore,
)
from fraud_graph.agent.providers.deepseek import DeepSeekError
from fraud_graph.domain import (
    AccountWorkspace,
    AssociatedAccount,
    Capability,
    EvidencePacket,
    OperationsSummary,
    RiskQueueItem,
    RiskQueuePage,
    RiskScore,
)
from fraud_graph.errors import ArtifactNotReadyError, ProductionSafetyError
from fraud_graph.evidence.associations import rank_associated_accounts
from fraud_graph.evidence.builder import build_evidence_packet
from fraud_graph.evidence.presentation import blocker_label
from fraud_graph.evidence.render import render_markdown
from fraud_graph.evidence.task3 import Task3ArtifactManifest
from fraud_graph.features.builder import FeatureManifest
from fraud_graph.graph.path_engine import PathSearchConfig, TemporalPathEngine
from fraud_graph.graph.repository import DuckDbParquetEventRepository
from fraud_graph.logging import opaque_log_id
from fraud_graph.modeling.risk_model import ModelMetadata, ScoreManifest
from fraud_graph.service.report_view import render_evidence_report_html
from fraud_graph.service.review_workflow import (
    ReviewCaseDetail,
    ReviewCaseQueue,
    ReviewDecisionInput,
    ReviewerPrincipal,
    ReviewSubmissionReceipt,
    ReviewWorkflow,
)
from fraud_graph.service.snapshot_view import GraphSnapshotView

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")
AGENT_SESSION_COOKIE = "fraud_graph_agent_session"


class ServiceState:
    def __init__(
        self,
        normalized_root: Path,
        feature_root: Path,
        artifact_root: Path,
        score_version: str,
        path_config: PathSearchConfig,
        *,
        case_sample_version: str | None = None,
        review_submission_dir: Path | None = None,
        review_write_enabled: bool = False,
        trust_review_identity_headers: bool = False,
    ) -> None:
        self.artifact_root = artifact_root.resolve()
        score_dir = artifact_root / "scores" / score_version
        self.score_manifest = ScoreManifest.load(score_dir / "manifest.json")
        model_dir = artifact_root / "models" / self.score_manifest.model_version
        self.model_metadata = ModelMetadata.load(model_dir / "metadata.json")
        feature_dir = feature_root / self.score_manifest.feature_version
        self.feature_manifest = FeatureManifest.load(feature_dir / "manifest.json")
        self._validate_manifest_chain()

        scores = pq.read_table(score_dir / self.score_manifest.file).to_pylist()
        features = pq.read_table(feature_dir / self.feature_manifest.file).to_pylist()
        self.scores = {str(row["account_id"]): row for row in scores}
        self.features = {str(row["account_id"]): row for row in features}
        if len(scores) != self.score_manifest.row_count or len(self.scores) != len(scores):
            raise ArtifactNotReadyError("score row count or account uniqueness check failed")
        if len(features) != self.feature_manifest.row_count or len(self.features) != len(features):
            raise ArtifactNotReadyError("feature row count or account uniqueness check failed")
        if self.scores.keys() != self.features.keys():
            raise ArtifactNotReadyError("score and feature account coverage differs")
        self.task3_manifest: Task3ArtifactManifest | None = None
        self.task3_dir: Path | None = None
        self.task3_packets: dict[str, EvidencePacket] = {}
        self.task3_associations: dict[str, list[AssociatedAccount]] = {}
        self.task3_roots: list[dict[str, Any]] = []
        self._load_task3_cache(artifact_root, score_version)
        transaction_glob = (
            normalized_root
            / self.score_manifest.data_version
            / "transactions"
            / "year=*"
            / "month=*"
            / "*.parquet"
        )
        repository = DuckDbParquetEventRepository(transaction_glob)
        self.path_engine = TemporalPathEngine(repository, path_config)
        self.review_workflow = (
            ReviewWorkflow(
                artifact_root / "case_samples" / case_sample_version,
                submission_dir=review_submission_dir,
                write_enabled=review_write_enabled,
            )
            if case_sample_version is not None
            else None
        )
        self.trust_review_identity_headers = trust_review_identity_headers
        self.graph_snapshot_view = GraphSnapshotView.discover(
            artifact_root / "graph_snapshots",
            self.score_manifest.data_version,
        )

    def _load_task3_cache(self, artifact_root: Path, score_version: str) -> None:
        candidates: list[tuple[Task3ArtifactManifest, Path]] = []
        for manifest_path in sorted((artifact_root / "task3").glob("*/manifest.json")):
            manifest = Task3ArtifactManifest.load(manifest_path)
            if manifest.score_version != score_version:
                continue
            candidates.append((manifest, manifest_path.parent))
        if not candidates:
            return
        manifest, task3_dir = max(candidates, key=lambda item: item[0].created_at)
        packets_payload = json.loads(
            (task3_dir / manifest.files["evidence_packets"]).read_text(encoding="utf-8")
        )
        associations_payload = json.loads(
            (task3_dir / manifest.files["association_map"]).read_text(encoding="utf-8")
        )
        self.task3_manifest = manifest
        self.task3_dir = task3_dir
        self.task3_packets = {
            str(account_id): EvidencePacket.model_validate(packet)
            for account_id, packet in packets_payload.items()
        }
        self.task3_associations = {
            str(account_id): [
                AssociatedAccount.model_validate(item) for item in items
            ]
            for account_id, items in associations_payload.items()
        }
        roots_path = task3_dir / manifest.files["root_accounts"]
        self.task3_roots = pq.read_table(roots_path).to_pylist()

    def _validate_manifest_chain(self) -> None:
        if self.model_metadata.model_version != self.score_manifest.model_version:
            raise ArtifactNotReadyError("score and model versions are incompatible")
        if self.model_metadata.feature_version != self.score_manifest.feature_version:
            raise ArtifactNotReadyError("score and model feature versions are incompatible")
        if self.feature_manifest.feature_version != self.score_manifest.feature_version:
            raise ArtifactNotReadyError("score and feature manifests are incompatible")
        data_versions = {
            self.score_manifest.data_version,
            self.model_metadata.data_version,
            self.feature_manifest.data_version,
        }
        if len(data_versions) != 1:
            raise ArtifactNotReadyError("score, model, and feature data versions are incompatible")

    def risk_score(self, account_id: str) -> RiskScore:
        row = self.scores.get(account_id)
        if row is None:
            raise KeyError(account_id)
        values = dict(row)
        values["eligibility_blockers"] = self.model_metadata.eligibility_blockers
        return RiskScore.model_validate(values)

    def risk_queue(
        self,
        *,
        query: str = "",
        active_only: bool = True,
        offset: int = 0,
        limit: int = 50,
    ) -> RiskQueuePage:
        normalized_query = query.strip().casefold()
        rows = sorted(self.scores.values(), key=lambda row: int(row["risk_rank"]))
        filtered = [
            row
            for row in rows
            if (not active_only or bool(row["has_transactions"]))
            and (not normalized_query or normalized_query in str(row["account_id"]).casefold())
        ]
        items = [
            RiskQueueItem.model_validate(
                {
                    "account_id": row["account_id"],
                    "risk_score": row["risk_score"],
                    "risk_rank": row["risk_rank"],
                    "has_transactions": row["has_transactions"],
                    "model_training_mode": row.get("model_training_mode", "unknown"),
                    "production_eligible": row.get("production_eligible", False),
                }
            )
            for row in filtered[offset : offset + limit]
        ]
        return RiskQueuePage(total=len(filtered), offset=offset, limit=limit, items=items)

    def operations_summary(self) -> OperationsSummary:
        scored_accounts = len(self.scores)
        active_accounts = sum(bool(row["has_transactions"]) for row in self.scores.values())
        review_fraction = self.model_metadata.metrics.top_fraction
        return OperationsSummary(
            scored_accounts=scored_accounts,
            active_accounts=active_accounts,
            review_capacity_count=math.ceil(scored_accounts * review_fraction),
            review_capacity_fraction=review_fraction,
            model_version=self.model_metadata.model_version,
            data_version=self.score_manifest.data_version,
            feature_version=self.score_manifest.feature_version,
            feature_as_of=self.score_manifest.feature_as_of,
            model_training_mode=self.model_metadata.training_mode,
            production_eligible=self.model_metadata.production_eligible,
            eligibility_blockers=self.model_metadata.eligibility_blockers,
        )

    def evidence(self, account_id: str) -> EvidencePacket:
        cached_packet = self.task3_packets.get(account_id)
        if cached_packet is not None:
            return cached_packet
        score = self.risk_score(account_id)
        end_time = self.feature_manifest.as_of
        start_time = end_time - timedelta(days=self.feature_manifest.lookback_days)
        outgoing = self.path_engine.find_paths(
            account_id, start_time, end_time, direction="outgoing"
        )
        incoming = self.path_engine.find_paths(
            account_id, start_time, end_time, direction="incoming"
        )
        paths = sorted([*outgoing, *incoming], key=lambda item: item.score, reverse=True)[
            : self.path_engine.max_paths
        ]
        return build_evidence_packet(score, paths, self.features.get(account_id))

    def associations(self, account_id: str, limit: int = 20) -> list[AssociatedAccount]:
        cached_associations = self.task3_associations.get(account_id)
        if cached_associations is not None:
            return cached_associations[:limit]
        packet = self.evidence(account_id)
        return rank_associated_accounts(account_id, packet.paths, limit=limit)

    def workspace(self, account_id: str, association_limit: int = 20) -> AccountWorkspace:
        packet = self.evidence(account_id)
        return AccountWorkspace(
            account_id=account_id,
            evidence=packet,
            associations=self.associations(account_id, association_limit),
        )

    def agent_system_context(self) -> dict[str, Any]:
        """Build a version-bound, read-only knowledge packet for system explanations."""
        experiment_path = (
            self.artifact_root
            / "retrospective_experiments"
            / self.model_metadata.model_version
            / "metadata.json"
        )
        experiment: dict[str, Any] = {}
        if experiment_path.is_file():
            payload = json.loads(experiment_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                experiment = payload

        feature_groups = experiment.get("feature_groups")
        if not isinstance(feature_groups, dict):
            feature_groups = {
                "behavior": self.feature_manifest.behavior_features,
                "profile_static_only": self.feature_manifest.profile_features,
            }
        group_labels = {
            "behavior": "交易行为特征",
            "graph": "图结构特征",
            "temporal_change": "快照变化特征",
            "unsupervised": "多粒度无监督异常分数",
            "profile_static_only": "账户画像字段",
        }
        feature_group_summary = [
            {
                "code": code,
                "name": group_labels.get(code, code),
                "count": len(columns),
                "features": columns,
            }
            for code, columns in feature_groups.items()
            if isinstance(columns, list)
        ]

        blocker_explanations = {
            "LABEL_TIME_MISSING": "风险标签没有发生时间，无法按标签发生前后构造严格时序训练集。",
            "RETROSPECTIVE_STATIC_TARGET": "当前目标是全观察期静态回溯标签，只适合离线比较。",
            "TRANSDUCTIVE_GRAPH_EVALUATION": (
                "图特征由完整观察图构造，测试节点的连接关系可能参与图统计。"
            ),
            "MODEL_APPROVAL_REQUIRED": "模型尚未经过银行内部模型风险管理和上线审批。",
            "ACTIVE_POSITIVES_TOO_FEW": "有交易行为的确认风险账户仅 3 个，资金证据能力评估不稳定。",
            "PROFILE_SHORTCUT_DEPENDENCY": "静态画像字段区分度较强，主模型可能部分依赖画像捷径。",
            "NON_TEMPORAL_DIAGNOSTIC_EVALUATION": "当前评估不是严格的按时间外推验证。",
            "REFERENCE_MODEL_NOT_APPROVED": "参考模型尚未经过生产审批。",
        }
        online_blockers = [
            {
                "code": code,
                "name": blocker_label(code),
                "meaning": blocker_explanations.get(code, "当前版本记录的生产资格限制。"),
            }
            for code in self.model_metadata.eligibility_blockers
        ]

        selected_metrics = experiment.get("test_metrics", {}).get("selected_model")
        if not isinstance(selected_metrics, dict):
            selected_metrics = self.model_metadata.metrics.model_dump(mode="json")
        baseline_metrics = {
            key: value
            for key, value in experiment.get("test_metrics", {}).items()
            if key in {
                "evidence_model",
                "behavior_baseline",
                "profile_baseline",
                "behavior_profile_baseline",
                "rule_baseline",
                "unsupervised_ensemble",
            }
            and isinstance(value, dict)
        }

        acceptance: dict[str, Any] = {}
        if self.review_workflow is not None:
            acceptance_path = self.review_workflow.case_sample_dir / "task4_acceptance.json"
            if acceptance_path.is_file():
                payload = json.loads(acceptance_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    structured = payload.get("structured_evaluation", {})
                    acceptance = {
                        "passed": payload.get("acceptance_passed", False),
                        "report_count": payload.get("expected_report_count", 0),
                        "consistency_rate": structured.get("consistency_rate"),
                        "evidence_requirement_rate": structured.get(
                            "evidence_requirement_rate"
                        ),
                        "completeness_rate": structured.get("completeness_rate"),
                        "exact_render_match_rate": payload.get(
                            "exact_render_match_rate"
                        ),
                        "case_sample_version": payload.get("case_sample_version"),
                    }

        return {
            "model_methodology": {
                "citation": {
                    "kind": "system",
                    "id": "MODEL_CARD",
                    "label": "模型卡与特征清单",
                },
                "model_version": self.model_metadata.model_version,
                "feature_version": self.feature_manifest.feature_version,
                "data_version": self.model_metadata.data_version,
                "selected_model": experiment.get(
                    "selected_candidate", "hybrid_profile_logistic"
                ),
                "evidence_model": experiment.get(
                    "evidence_candidate", "hybrid_hist_gradient_boosting"
                ),
                "training_mode": self.model_metadata.training_mode,
                "class_balance": (
                    "带类别平衡的逻辑回归：通过提高少数风险样本在损失函数中的权重，"
                    "减轻类别极不平衡造成的多数类偏置。"
                ),
                "feature_count": sum(item["count"] for item in feature_group_summary),
                "feature_groups": feature_group_summary,
                "combination_method": (
                    "各特征作为独立输入列，经预处理后由模型学习权重；不是先在组内直接相加。"
                ),
            },
            "risk_scoring_policy": {
                "citation": {
                    "kind": "system",
                    "id": "RISK_SCORING_POLICY",
                    "label": "风险评分与上线边界",
                },
                "summary": (
                    "上线阻断项是阻止当前模型直接驱动生产处置的已知条件，"
                    "不影响比赛演示、离线分析和人工辅助研判。"
                ),
                "rules": [
                    "没有一个固定风险分数阈值可以自动确认涉诈。",
                    "risk_score 是主模型输出，用于全量账户风险排序。",
                    "evidence_score 来自不含画像字段的资金证据模型，用于观察资金与图特征支持度。",
                    "anomaly_score 是无监督异常程度，不使用风险标签，不等于涉诈概率。",
                    "Top 5% 是人工复核容量口径，不是涉诈判定线。",
                    "active_only 仅筛选观察窗口内有交易的账户，不改变模型分数。",
                    "历史风险标签是已确认结果，模型风险分数是预测，两者不能混同。",
                ],
                "online_blockers": online_blockers,
                "production_eligible": self.model_metadata.production_eligible,
            },
            "model_evaluation": {
                "citation": {
                    "kind": "system",
                    "id": "MODEL_EVALUATION",
                    "label": "模型评估结果",
                },
                "evaluation_scheme": self.model_metadata.evaluation_scheme,
                "test_metrics": selected_metrics,
                "baseline_metrics": baseline_metrics,
                "competition_targets": {
                    "roc_auc": "达到 0.85，或较规则/传统机器学习基线提升不少于 5 个百分点",
                    "pr_auc": "较基线提升不少于 20%",
                    "top5_recall": "达到 50%，或较基线提升不少于 15 个百分点",
                },
                "test_positive_count": selected_metrics.get("positives"),
                "active_positive_count": experiment.get(
                    "population_diagnostics", {}
                ).get("active_positive_rows"),
                "production_eligible": self.model_metadata.production_eligible,
                "online_blockers": online_blockers,
            },
            "task4_requirements": {
                "citation": {
                    "kind": "system",
                    "id": "TASK4_REQUIREMENTS",
                    "label": "任务四报告要求与验收",
                },
                "requirements": [
                    "输出风险摘要。",
                    "至少呈现两类证据。",
                    "解释关键交易边或可疑路径。",
                    "提供可核验的证据清单、局限性和辅助处置建议。",
                    "交付报告模板或生成代码及证据字段字典。",
                    "评估报告可读性和结构化结果一致性。",
                ],
                "acceptance": acceptance,
                "agent_role": (
                    "Agent 是辅助分析和加分方向；确定性证据报告仍是任务四的正式主产物。"
                ),
            },
        }

    def graph_snapshot_list(self) -> dict[str, Any]:
        if self.graph_snapshot_view is None:
            raise ArtifactNotReadyError("graph snapshot artifact is not configured")
        return self.graph_snapshot_view.list_payload()

    def graph_snapshot_detail(
        self,
        snapshot_id: str,
        node_limit: int = 70,
        edge_limit: int = 150,
    ) -> dict[str, Any]:
        if self.graph_snapshot_view is None:
            raise ArtifactNotReadyError("graph snapshot artifact is not configured")
        return self.graph_snapshot_view.detail_payload(
            snapshot_id,
            node_limit=node_limit,
            edge_limit=edge_limit,
        )

    @property
    def graph_snapshots_available(self) -> bool:
        return self.graph_snapshot_view is not None

    @property
    def task3_available(self) -> bool:
        return self.task3_manifest is not None

    def task3_manifest_payload(self) -> dict[str, Any]:
        if self.task3_manifest is None:
            raise ArtifactNotReadyError("task3 artifact is not configured")
        return self.task3_manifest.model_dump(mode="json")

    def task3_root_page(self, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        if self.task3_manifest is None:
            raise ArtifactNotReadyError("task3 artifact is not configured")
        rows = self.task3_roots[offset : offset + limit]
        return {
            "task3_version": self.task3_manifest.task3_version,
            "total": len(self.task3_roots),
            "offset": offset,
            "limit": limit,
            "items": rows,
        }

    def task3_account_graph(self, account_id: str) -> dict[str, Any]:
        if self.task3_manifest is None:
            raise ArtifactNotReadyError("task3 artifact is not configured")
        packet = self.task3_packets.get(account_id)
        if packet is None:
            raise KeyError(account_id)
        root = next(
            (row for row in self.task3_roots if str(row["account_id"]) == account_id),
            None,
        )
        if root is None:
            raise KeyError(account_id)
        return {
            "task3_version": self.task3_manifest.task3_version,
            "account_id": account_id,
            "root": root,
            "associations": [
                item.model_dump(mode="json") for item in self.associations(account_id)
            ],
            "paths": [item.model_dump(mode="json") for item in packet.paths],
            "patterns": [item.model_dump(mode="json") for item in packet.patterns],
        }


def create_app(
    state: ServiceState,
    agent_service: AgentService | None = None,
    agent_session_store: AgentSessionStore | None = None,
) -> FastAPI:
    agent_service = agent_service or AgentService(state)
    agent_session_store = agent_session_store or AgentSessionStore(agent_service.config)
    app = FastAPI(
        title="Fraud Graph Risk API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url=None,
    )
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict[str, str | bool]:
        return {
            "status": "ready",
            "score_version": state.score_manifest.score_version,
            "model_version": state.model_metadata.model_version,
            "feature_version": state.feature_manifest.feature_version,
            "data_version": state.score_manifest.data_version,
            "production_eligible": state.model_metadata.production_eligible,
        }

    @app.get("/", include_in_schema=False, response_class=FileResponse)
    def workbench() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/reviews", include_in_schema=False, response_class=FileResponse)
    def review_workbench() -> FileResponse:
        return FileResponse(STATIC_DIR / "reviews.html")

    @app.get("/snapshots", include_in_schema=False, response_class=FileResponse)
    def snapshot_workbench() -> FileResponse:
        return FileResponse(STATIC_DIR / "snapshots.html")

    @app.get("/v1/operations/summary", response_model=OperationsSummary)
    def operations_summary() -> OperationsSummary:
        return state.operations_summary()

    @app.get("/v1/accounts", response_model=RiskQueuePage)
    def list_accounts(
        query: str = Query(default="", max_length=128),
        active_only: bool = Query(default=True),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> RiskQueuePage:
        return state.risk_queue(
            query=query,
            active_only=active_only,
            offset=offset,
            limit=limit,
        )

    @app.get("/v1/capabilities", response_model=list[Capability])
    def capabilities() -> list[Capability]:
        return [
            Capability(key="risk_queue", status="available", endpoint="/v1/accounts"),
            Capability(
                key="account_workspace",
                status="available",
                endpoint="/v1/accounts/{account_id}/workspace",
            ),
            Capability(
                key="report_markdown",
                status="available",
                endpoint="/v1/accounts/{account_id}/report",
            ),
            Capability(
                key="manual_review",
                status="available"
                if getattr(state, "review_workflow", None) is not None
                else "contract_ready",
                endpoint=(
                    "/v1/reviews/cases"
                    if getattr(state, "review_workflow", None) is not None
                    else None
                ),
            ),
            Capability(
                key="graph_snapshots",
                status=(
                    "available"
                    if getattr(state, "graph_snapshots_available", False)
                    else "contract_ready"
                ),
                endpoint="/v1/graph-snapshots",
            ),
            Capability(
                key="task3_artifacts",
                status="available"
                if getattr(state, "task3_available", False)
                else "contract_ready",
                endpoint="/v1/task3/manifest",
            ),
            Capability(
                key="investigation_agent",
                status="available",
                endpoint="/v1/agent/query",
            ),
            Capability(key="case_feedback", status="contract_ready"),
            Capability(key="iam", status="upstream_required"),
            Capability(key="audit_sink", status="upstream_required"),
        ]

    @app.get("/v1/agent/capabilities", response_model=AgentCapabilities)
    def get_agent_capabilities() -> AgentCapabilities:
        return agent_service.capabilities()

    @app.get("/v1/agent/session", response_model=AgentSessionStatus)
    def get_agent_session(
        session_id: str | None = Cookie(default=None, alias=AGENT_SESSION_COOKIE),
    ) -> AgentSessionStatus:
        return agent_session_store.status(session_id)

    @app.post("/v1/agent/session", response_model=AgentSessionStatus)
    async def create_agent_session(
        payload: AgentSessionCreate,
        response: Response,
        request: Request,
    ) -> AgentSessionStatus:
        if not payload.data_export_authorized:
            raise HTTPException(
                status_code=400,
                detail="必须确认已获授权向 DeepSeek 发送当前脱敏证据",
            )
        try:
            session_id, status = await agent_session_store.create(
                payload.api_key,
                payload.model,
            )
        except DeepSeekError as exc:
            LOGGER.warning("DeepSeek credential validation failed")
            raise HTTPException(
                status_code=502,
                detail="DeepSeek API Key 验证失败，请检查密钥、模型和网络连接",
            ) from exc
        response.set_cookie(
            AGENT_SESSION_COOKIE,
            session_id,
            max_age=agent_session_store.ttl_seconds,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
            path="/v1/agent",
        )
        return status

    @app.delete("/v1/agent/session", response_model=AgentSessionStatus)
    def delete_agent_session(
        response: Response,
        session_id: str | None = Cookie(default=None, alias=AGENT_SESSION_COOKIE),
    ) -> AgentSessionStatus:
        agent_session_store.remove(session_id)
        response.delete_cookie(AGENT_SESSION_COOKIE, path="/v1/agent")
        return AgentSessionStatus(active=False)

    @app.post("/v1/agent/query", response_model=AgentResponse)
    async def query_agent(
        query: AgentQuery,
        session_id: str | None = Cookie(default=None, alias=AGENT_SESSION_COOKIE),
    ) -> AgentResponse:
        try:
            return await agent_service.query(
                query,
                provider_client=agent_session_store.client(session_id),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="account not found") from exc

    @app.get("/v1/graph-snapshots")
    def list_graph_snapshots() -> dict[str, Any]:
        try:
            return state.graph_snapshot_list()
        except ArtifactNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/graph-snapshots/{snapshot_id}")
    async def get_graph_snapshot(
        snapshot_id: str,
        node_limit: int = Query(default=70, ge=20, le=100),
        edge_limit: int = Query(default=150, ge=20, le=300),
    ) -> dict[str, Any]:
        try:
            return await run_in_threadpool(
                state.graph_snapshot_detail,
                snapshot_id,
                node_limit,
                edge_limit,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="graph snapshot not found") from exc
        except ArtifactNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/task3/manifest")
    def get_task3_manifest() -> dict[str, Any]:
        try:
            return state.task3_manifest_payload()
        except ArtifactNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/task3/roots")
    def list_task3_roots(
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        try:
            return state.task3_root_page(offset=offset, limit=limit)
        except ArtifactNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/task3/accounts/{account_id}/graph")
    async def get_task3_account_graph(account_id: str) -> dict[str, Any]:
        try:
            return await run_in_threadpool(state.task3_account_graph, account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task3 root account not found") from exc
        except ArtifactNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/accounts/{account_id}/risk", response_model=RiskScore)
    def get_risk(account_id: str) -> RiskScore:
        try:
            return state.risk_score(account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="account not found") from exc

    @app.get(
        "/v1/accounts/{account_id}/workspace",
        response_model=AccountWorkspace,
    )
    async def get_workspace(
        account_id: str,
        association_limit: int = Query(default=20, ge=1, le=100),
    ) -> AccountWorkspace:
        try:
            return await run_in_threadpool(state.workspace, account_id, association_limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="account not found") from exc

    @app.get("/v1/accounts/{account_id}/evidence", response_model=EvidencePacket)
    async def get_evidence(account_id: str) -> EvidencePacket:
        try:
            packet = await run_in_threadpool(state.evidence, account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="account not found") from exc
        LOGGER.info(
            "evidence generated",
            extra={
                "account_ref": opaque_log_id(account_id),
                "model_version": packet.score.model_version,
                "data_version": packet.score.data_version,
            },
        )
        return packet

    @app.get(
        "/v1/accounts/{account_id}/associations",
        response_model=list[AssociatedAccount],
    )
    async def get_associations(
        account_id: str,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[AssociatedAccount]:
        try:
            return await run_in_threadpool(state.associations, account_id, limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="account not found") from exc

    @app.get(
        "/v1/accounts/{account_id}/report",
        response_model=None,
    )
    async def get_report(
        account_id: str,
        view: Literal["markdown", "html"] = Query(default="markdown"),
    ) -> Response:
        try:
            packet = await run_in_threadpool(state.evidence, account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="account not found") from exc
        markdown = render_markdown(packet)
        if view == "html":
            markdown_url = f"/v1/accounts/{account_id}/report"
            return HTMLResponse(
                render_evidence_report_html(packet, markdown_url=markdown_url)
            )
        return PlainTextResponse(markdown, media_type="text/markdown")

    @app.get("/v1/reviews/cases", response_model=ReviewCaseQueue)
    def list_review_cases() -> ReviewCaseQueue:
        workflow = _review_workflow_or_503(state)
        return workflow.list_cases()

    @app.get("/v1/reviews/cases/{case_id}", response_model=ReviewCaseDetail)
    def get_review_case(case_id: str) -> ReviewCaseDetail:
        workflow = _review_workflow_or_503(state)
        try:
            return workflow.case_detail(case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="review case not found") from exc

    @app.get("/v1/reviews/status")
    def get_review_status() -> dict[str, Any]:
        workflow = _review_workflow_or_503(state)
        return workflow.acceptance_status().model_dump(mode="json")

    @app.get(
        "/v1/reviews/cases/{case_id}/report",
        response_model=None,
    )
    def get_review_case_report(
        case_id: str,
        view: Literal["markdown", "html"] = Query(default="markdown"),
    ) -> Response:
        workflow = _review_workflow_or_503(state)
        try:
            detail = workflow.case_detail(case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="review case not found") from exc
        if view == "html":
            markdown_url = f"/v1/reviews/cases/{case_id}/report"
            return HTMLResponse(
                render_evidence_report_html(
                    detail.packet,
                    markdown_url=markdown_url,
                    case_id=detail.case_id,
                    packet_sha256=detail.packet_sha256,
                )
            )
        content = workflow.report_path(case_id).read_text(encoding="utf-8")
        return PlainTextResponse(content, media_type="text/markdown")

    @app.get(
        "/v1/reviews/templates/{template_name}",
        response_class=FileResponse,
    )
    def get_review_template(template_name: str) -> FileResponse:
        workflow = _review_workflow_or_503(state)
        try:
            path = workflow.template_path(template_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="review template not found") from exc
        return FileResponse(path, media_type="application/json", filename=path.name)

    @app.post("/v1/reviews/submissions", response_model=ReviewSubmissionReceipt, status_code=201)
    def submit_review(
        decision: ReviewDecisionInput,
        verified_reviewer_id: str | None = Header(
            default=None, alias="X-Verified-Reviewer-Id", max_length=256
        ),
        verified_reviewer_role: str | None = Header(
            default=None, alias="X-Verified-Reviewer-Role", max_length=256
        ),
    ) -> ReviewSubmissionReceipt:
        workflow = _review_workflow_or_503(state)
        if not state.trust_review_identity_headers:
            raise HTTPException(
                status_code=503,
                detail="trusted reviewer identity is not configured",
            )
        if not verified_reviewer_id or not verified_reviewer_role:
            raise HTTPException(status_code=401, detail="verified reviewer identity is required")
        try:
            receipt = workflow.submit(
                decision,
                ReviewerPrincipal(
                    reviewer_id=verified_reviewer_id,
                    reviewer_role=verified_reviewer_role,
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="review case not found") from exc
        except ProductionSafetyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        LOGGER.info(
            "manual review accepted",
            extra={
                "case_id": decision.case_id,
                "reviewer_ref": opaque_log_id(verified_reviewer_id),
                "review_type": decision.review_type,
                "submission_id": receipt.submission_id,
            },
        )
        return receipt

    return app


def _review_workflow_or_503(state: ServiceState) -> ReviewWorkflow:
    workflow = state.review_workflow
    if workflow is None:
        raise HTTPException(status_code=503, detail="review case package is not configured")
    return workflow
