from __future__ import annotations

import logging
import math
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

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
from fraud_graph.errors import ArtifactNotReadyError
from fraud_graph.evidence.associations import rank_associated_accounts
from fraud_graph.evidence.builder import build_evidence_packet
from fraud_graph.evidence.render import render_markdown
from fraud_graph.features.builder import FeatureManifest
from fraud_graph.graph.path_engine import PathSearchConfig, TemporalPathEngine
from fraud_graph.graph.repository import DuckDbParquetEventRepository
from fraud_graph.logging import opaque_log_id
from fraud_graph.modeling.risk_model import ModelMetadata, ScoreManifest

LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")


class ServiceState:
    def __init__(
        self,
        normalized_root: Path,
        feature_root: Path,
        artifact_root: Path,
        score_version: str,
        path_config: PathSearchConfig,
    ) -> None:
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
        packet = self.evidence(account_id)
        return rank_associated_accounts(account_id, packet.paths, limit=limit)

    def workspace(self, account_id: str, association_limit: int = 20) -> AccountWorkspace:
        packet = self.evidence(account_id)
        return AccountWorkspace(
            account_id=account_id,
            evidence=packet,
            associations=rank_associated_accounts(
                account_id, packet.paths, limit=association_limit
            ),
        )


def create_app(state: ServiceState) -> FastAPI:
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
            Capability(key="manual_review", status="contract_ready"),
            Capability(key="case_feedback", status="contract_ready"),
            Capability(key="iam", status="upstream_required"),
            Capability(key="audit_sink", status="upstream_required"),
        ]

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
        response_class=PlainTextResponse,
    )
    async def get_report(account_id: str) -> PlainTextResponse:
        try:
            packet = await run_in_threadpool(state.evidence, account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="account not found") from exc
        return PlainTextResponse(render_markdown(packet), media_type="text/markdown")

    return app
