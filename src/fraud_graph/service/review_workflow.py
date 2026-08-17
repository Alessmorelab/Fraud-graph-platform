from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from fraud_graph.domain import EvidenceGrade, EvidencePacket
from fraud_graph.errors import ArtifactNotReadyError, ProductionSafetyError
from fraud_graph.evidence.cases import (
    CaseSampleManifest,
    CaseSampleRecord,
    build_automated_prechecks,
    load_case_records,
)
from fraud_graph.evidence.evaluation import (
    ManualCaseEvaluation,
    ManualCaseReview,
    evaluate_manual_cases,
)

EvidenceType = Literal["MODEL", "BEHAVIOR", "TRANSACTION", "GRAPH_STRUCTURE"]
ReviewType = Literal["primary", "adjudication"]
ReviewCaseStatus = Literal[
    "awaiting_primary_reviews", "adjudication_required", "passed", "failed"
]


class ReviewCaseSummary(BaseModel):
    case_id: str
    account_id: str
    packet_sha256: str
    grade: EvidenceGrade
    risk_score: float = Field(ge=0.0, le=1.0)
    risk_rank: int = Field(ge=1)
    path_count: int = Field(ge=0)
    max_hops: int = Field(ge=0)
    status: ReviewCaseStatus
    primary_reviewer_count: int = Field(ge=0)


class ReviewCaseQueue(BaseModel):
    case_sample_version: str
    write_enabled: bool
    items: list[ReviewCaseSummary]


class ReviewCaseDetail(BaseModel):
    case_sample_version: str
    case_id: str
    account_id: str
    packet_sha256: str
    status: ReviewCaseStatus
    primary_reviewer_count: int = Field(ge=0)
    packet: EvidencePacket


class ReviewDecisionInput(BaseModel):
    case_id: str = Field(min_length=1)
    packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_type: ReviewType = "primary"
    independence_attested: bool
    evidence_consistent: bool
    path_explanation_pass: bool
    readability_pass: bool
    evidence_types_verified: list[EvidenceType] = Field(min_length=1)
    notes: str = Field(default="", max_length=4_000)


class ReviewSubmissionReceipt(BaseModel):
    submission_id: str
    case_id: str
    review_type: ReviewType
    accepted_at: datetime
    case_status: ReviewCaseStatus
    acceptance: ManualCaseEvaluation


class ReviewerPrincipal(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=256)
    reviewer_role: str = Field(min_length=1, max_length=256)


class ReviewWorkflow:
    """Read immutable case packages and append hash-bound human decisions."""

    def __init__(
        self,
        case_sample_dir: Path,
        *,
        submission_dir: Path | None = None,
        write_enabled: bool = False,
    ) -> None:
        self.case_sample_dir = case_sample_dir.resolve()
        self.manifest = CaseSampleManifest.load(self.case_sample_dir / "manifest.json")
        records = load_case_records(self.case_sample_dir / self.manifest.cases_file)
        if self.manifest.case_count != len(records):
            raise ArtifactNotReadyError("case sample manifest count does not match cases.json")
        self.records = {record.case_id: record for record in records}
        self.case_bindings = {
            record.case_id: record.packet_sha256 for record in records
        }
        self.submission_dir = submission_dir.resolve() if submission_dir is not None else None
        self.write_enabled = write_enabled
        if self.write_enabled and self.submission_dir is None:
            raise ValueError("submission_dir is required when review writes are enabled")
        if self.write_enabled:
            assert self.submission_dir is not None
            self.submission_dir.mkdir(parents=True, exist_ok=True)

    @property
    def case_sample_version(self) -> str:
        return self.manifest.case_sample_version

    def list_cases(self) -> ReviewCaseQueue:
        result_by_case = self._result_by_case(self.acceptance_status())
        items = [
            self._summary(record, result_by_case[record.case_id])
            for record in self.records.values()
        ]
        return ReviewCaseQueue(
            case_sample_version=self.case_sample_version,
            write_enabled=self.write_enabled,
            items=items,
        )

    def case_detail(self, case_id: str) -> ReviewCaseDetail:
        record = self.records.get(case_id)
        if record is None:
            raise KeyError(case_id)
        result = self._result_by_case(self.acceptance_status())[case_id]
        return ReviewCaseDetail(
            case_sample_version=self.case_sample_version,
            case_id=record.case_id,
            account_id=record.account_id,
            packet_sha256=record.packet_sha256,
            status=self._public_status(str(result["status"])),
            primary_reviewer_count=self._primary_reviewer_count(result),
            packet=record.packet,
        )

    def report_path(self, case_id: str) -> Path:
        record = self.records.get(case_id)
        if record is None:
            raise KeyError(case_id)
        path = (self.case_sample_dir / record.report_file).resolve()
        if not path.is_relative_to(self.case_sample_dir) or not path.is_file():
            raise ArtifactNotReadyError(f"case report is missing or unsafe: {case_id}")
        return path

    def template_path(self, template_name: str) -> Path:
        allowed = {
            Path(item).name: item for item in self.manifest.primary_review_template_files
        }
        adjudication = self.manifest.adjudication_template_file
        allowed[Path(adjudication).name] = adjudication
        relative_path = allowed.get(template_name)
        if relative_path is None:
            raise KeyError(template_name)
        path = (self.case_sample_dir / relative_path).resolve()
        if not path.is_relative_to(self.case_sample_dir) or not path.is_file():
            raise ArtifactNotReadyError(f"review template is missing or unsafe: {template_name}")
        return path

    def acceptance_status(self) -> ManualCaseEvaluation:
        reviews = [
            *build_automated_prechecks(list(self.records.values())),
            *self._load_human_reviews(),
        ]
        return evaluate_manual_cases(
            [review.model_dump(mode="json") for review in reviews],
            self.case_bindings,
        )

    def submit(
        self,
        decision: ReviewDecisionInput,
        principal: ReviewerPrincipal,
    ) -> ReviewSubmissionReceipt:
        if not self.write_enabled or self.submission_dir is None:
            raise ProductionSafetyError("manual review writes are disabled")
        record = self.records.get(decision.case_id)
        if record is None:
            raise KeyError(decision.case_id)
        if decision.packet_sha256 != record.packet_sha256:
            raise ProductionSafetyError("evidence packet hash mismatch")

        current = self.acceptance_status()
        current_result = self._result_by_case(current)[decision.case_id]
        current_status = str(current_result["status"])
        if decision.review_type == "primary" and current_status != "insufficient_human_reviews":
            raise ProductionSafetyError("primary review phase is already closed for this case")
        if decision.review_type == "adjudication" and current_status != "adjudication_required":
            raise ProductionSafetyError("adjudication is not required for this case")

        reviewed_at = datetime.now(UTC)
        review = ManualCaseReview(
            case_id=decision.case_id,
            packet_sha256=decision.packet_sha256,
            reviewer_id=principal.reviewer_id,
            reviewer_role=principal.reviewer_role,
            reviewer_kind="human",
            review_type=decision.review_type,
            reviewed_at=reviewed_at,
            independence_attested=decision.independence_attested,
            evidence_consistent=decision.evidence_consistent,
            path_explanation_pass=decision.path_explanation_pass,
            readability_pass=decision.readability_pass,
            evidence_types_verified=decision.evidence_types_verified,
            notes=decision.notes,
        )
        existing = self._load_human_reviews()
        evaluate_manual_cases(
            [item.model_dump(mode="json") for item in [*existing, review]],
            self.case_bindings,
        )

        submission_id = self._submission_id(review)
        target = self.submission_dir / f"{submission_id}.json"
        payload = review.model_dump_json(indent=2) + "\n"
        self._write_exclusive(target, payload)
        acceptance = self.acceptance_status()
        result = self._result_by_case(acceptance)[decision.case_id]
        return ReviewSubmissionReceipt(
            submission_id=submission_id,
            case_id=decision.case_id,
            review_type=decision.review_type,
            accepted_at=reviewed_at,
            case_status=self._public_status(str(result["status"])),
            acceptance=acceptance,
        )

    def _load_human_reviews(self) -> list[ManualCaseReview]:
        if self.submission_dir is None:
            return self._load_offline_human_reviews()
        if not self.submission_dir.exists():
            return []
        reviews: list[ManualCaseReview] = []
        for path in sorted(self.submission_dir.glob("*.json")):
            try:
                review = ManualCaseReview.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ArtifactNotReadyError(
                    f"invalid review submission artifact: {path.name}"
                ) from exc
            if review.reviewer_kind != "human":
                raise ArtifactNotReadyError(
                    f"review submission artifact is not a human review: {path.name}"
                )
            reviews.append(review)
        return reviews

    def _load_offline_human_reviews(self) -> list[ManualCaseReview]:
        reviews: list[ManualCaseReview] = []
        for path in sorted(self.case_sample_dir.glob("*-submission.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, list):
                    raise ValueError("offline review submission must be a JSON array")
                reviews.extend(ManualCaseReview.model_validate(item) for item in payload)
            except (OSError, ValueError) as exc:
                raise ArtifactNotReadyError(
                    f"invalid offline review submission artifact: {path.name}"
                ) from exc
        return reviews

    @staticmethod
    def _write_exclusive(path: Path, payload: str) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise ProductionSafetyError("duplicate reviewer submission") from exc
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _submission_id(review: ManualCaseReview) -> str:
        material = "\x00".join(
            [review.case_id, review.reviewer_id, review.review_type]
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:32]

    @staticmethod
    def _result_by_case(evaluation: ManualCaseEvaluation) -> dict[str, dict[str, object]]:
        return {str(item["case_id"]): item for item in evaluation.case_results}

    @classmethod
    def _summary(
        cls,
        record: CaseSampleRecord,
        result: dict[str, object],
    ) -> ReviewCaseSummary:
        return ReviewCaseSummary(
            case_id=record.case_id,
            account_id=record.account_id,
            packet_sha256=record.packet_sha256,
            grade=record.packet.grade,
            risk_score=record.packet.score.risk_score,
            risk_rank=record.packet.score.risk_rank,
            path_count=len(record.packet.paths),
            max_hops=max((len(path.steps) for path in record.packet.paths), default=0),
            status=cls._public_status(str(result["status"])),
            primary_reviewer_count=cls._primary_reviewer_count(result),
        )

    @staticmethod
    def _primary_reviewer_count(result: dict[str, object]) -> int:
        value = result.get("primary_reviewer_count", 0)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value)
        return 0

    @staticmethod
    def _public_status(status: str) -> ReviewCaseStatus:
        if status == "insufficient_human_reviews":
            return "awaiting_primary_reviews"
        if status in {"adjudication_required", "passed", "failed"}:
            return status  # type: ignore[return-value]
        raise ArtifactNotReadyError(f"unsupported review case status: {status}")
