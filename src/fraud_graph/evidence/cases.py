from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from fraud_graph.artifacts import atomic_artifact_directory
from fraud_graph.domain import EvidencePacket
from fraud_graph.errors import ArtifactNotReadyError
from fraud_graph.evidence.evaluation import (
    ManualCaseReview,
    ReportConsistencyResult,
    evaluate_report_consistency,
    validate_evidence_packet,
)
from fraud_graph.evidence.render import render_markdown

CASE_EXPORT_VERSION = "review-cases-v5"


def evidence_packet_sha256(packet: EvidencePacket) -> str:
    payload = json.dumps(
        packet.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CaseSampleRecord(BaseModel):
    case_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_file: str = Field(min_length=1)
    evaluation_status: Literal["pending_human_review"] = "pending_human_review"
    evaluation_reason: str
    packet: EvidencePacket


def load_case_records(path: Path) -> list[CaseSampleRecord]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("case sample file must contain a JSON array")
    records = [CaseSampleRecord.model_validate(item) for item in payload]
    case_ids = [record.case_id for record in records]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("case sample contains duplicate case_id values")
    for record in records:
        if record.account_id != record.packet.account_id:
            raise ValueError(f"case/account mismatch for {record.case_id}")
        if evidence_packet_sha256(record.packet) != record.packet_sha256:
            raise ValueError(f"evidence packet hash mismatch for {record.case_id}")
    return records


def render_pending_case_report(packet: EvidencePacket) -> str:
    return (
        "<!-- evaluation_status: pending_human_review -->\n"
        "<!-- valid for explanation review only; not valid for association-hit metrics -->\n\n"
        + render_markdown(packet)
    )


class CaseSampleManifest(BaseModel):
    schema_version: str = "1.0"
    implementation_version: str = CASE_EXPORT_VERSION
    case_sample_version: str
    score_version: str
    data_version: str
    model_version: str
    created_at: datetime
    status: Literal["pending_human_review"] = "pending_human_review"
    case_count: int
    cases_file: str = "cases.json"
    report_directory: str = "reports"
    review_template_directory: str = "review_templates"
    primary_review_template_files: list[str] = Field(
        default_factory=lambda: [
            "review_templates/primary-reviewer-1.json",
            "review_templates/primary-reviewer-2.json",
        ]
    )
    adjudication_template_file: str = "review_templates/adjudicator.json"

    @classmethod
    def load(cls, path: Path) -> CaseSampleManifest:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class Task4ReportAcceptance(BaseModel):
    schema_version: str = "1.0"
    acceptance_scope: Literal["task_4_report"] = "task_4_report"
    case_sample_version: str
    generated_at: datetime
    acceptance_passed: bool
    structured_evaluation: ReportConsistencyResult
    expected_report_count: int
    exact_render_match_count: int
    exact_render_match_rate: float
    issues: list[dict[str, str]] = Field(default_factory=list)
    task_1_status: Literal["static_retrospective_experiment"] = "static_retrospective_experiment"
    task_2_status: Literal["static_retrospective_experiment"] = "static_retrospective_experiment"


def evaluate_case_sample_reports(case_sample_dir: Path) -> Task4ReportAcceptance:
    """Verify structured packets and the exact deterministic reports delivered to reviewers."""
    manifest = CaseSampleManifest.load(case_sample_dir / "manifest.json")
    records = load_case_records(case_sample_dir / manifest.cases_file)
    structured = evaluate_report_consistency([record.packet for record in records])
    issues: list[dict[str, str]] = []
    exact_matches = 0
    resolved_root = case_sample_dir.resolve()
    for record in records:
        report_path = (case_sample_dir / record.report_file).resolve()
        if not report_path.is_relative_to(resolved_root):
            issues.append({"case_id": record.case_id, "error": "REPORT_PATH_OUTSIDE_ARTIFACT"})
            continue
        if not report_path.is_file():
            issues.append({"case_id": record.case_id, "error": "REPORT_FILE_MISSING"})
            continue
        expected = render_pending_case_report(record.packet)
        if report_path.read_text(encoding="utf-8") != expected:
            issues.append({"case_id": record.case_id, "error": "REPORT_CONTENT_MISMATCH"})
            continue
        exact_matches += 1
    expected_count = len(records)
    exact_match_rate = exact_matches / expected_count if expected_count else 0.0
    if manifest.case_count != expected_count:
        issues.append(
            {
                "case_id": "manifest",
                "error": "MANIFEST_CASE_COUNT_MISMATCH",
            }
        )
    return Task4ReportAcceptance(
        case_sample_version=manifest.case_sample_version,
        generated_at=datetime.now(UTC),
        acceptance_passed=(structured.acceptance_passed and exact_match_rate == 1.0 and not issues),
        structured_evaluation=structured,
        expected_report_count=expected_count,
        exact_render_match_count=exact_matches,
        exact_render_match_rate=exact_match_rate,
        issues=issues,
    )


def build_automated_prechecks(records: list[CaseSampleRecord]) -> list[ManualCaseReview]:
    """Create transparent technical checks that cannot count as human reviews."""
    generated_at = datetime.now(UTC)
    checks: list[ManualCaseReview] = []
    for record in records:
        errors = validate_evidence_packet(record.packet)
        evidence_types: list[Literal["MODEL", "BEHAVIOR", "TRANSACTION", "GRAPH_STRUCTURE"]]
        evidence_types = ["MODEL"]
        if any(
            fact.code != "MODEL_RISK_SCORE" and not fact.transaction_ids
            for fact in record.packet.facts
        ):
            evidence_types.append("BEHAVIOR")
        if record.packet.paths:
            evidence_types.append("TRANSACTION")
        if any(len(path.steps) >= 2 for path in record.packet.paths):
            evidence_types.append("GRAPH_STRUCTURE")
        if record.packet.patterns and "GRAPH_STRUCTURE" not in evidence_types:
            evidence_types.append("GRAPH_STRUCTURE")
        checks.append(
            ManualCaseReview(
                case_id=record.case_id,
                packet_sha256=record.packet_sha256,
                reviewer_id="system:evidence-validator-v1",
                reviewer_role="deterministic technical validator",
                reviewer_kind="automated_precheck",
                reviewed_at=generated_at,
                independence_attested=False,
                evidence_consistent=not errors,
                path_explanation_pass=(
                    bool(record.packet.paths)
                    and not any(error.startswith(("PATH_", "EMPTY_PATH")) for error in errors)
                ),
                readability_pass=bool(
                    record.packet.facts
                    and record.packet.limitations
                    and record.packet.recommended_actions
                ),
                evidence_types_verified=evidence_types,
                notes=(
                    "deterministic validation passed"
                    if not errors
                    else "deterministic validation errors: " + ", ".join(errors)
                ),
            )
        )
    return checks


def export_illustrative_cases(
    packets: list[EvidencePacket],
    output_root: Path,
    *,
    score_version: str,
) -> CaseSampleManifest:
    if len(packets) < 5:
        raise ValueError("at least five evidence packets are required for a case sample export")
    versions = {(packet.score.data_version, packet.score.model_version) for packet in packets}
    if len(versions) != 1:
        raise ValueError("all case packets must use the same data and model versions")
    data_version, model_version = versions.pop()
    version_material = json.dumps(
        {
            "implementation": CASE_EXPORT_VERSION,
            "score_version": score_version,
            "account_ids": [packet.account_id for packet in packets],
            "packet_sha256": [evidence_packet_sha256(packet) for packet in packets],
        },
        sort_keys=True,
    )
    case_sample_version = hashlib.sha256(version_material.encode()).hexdigest()[:16]
    output_dir = output_root / case_sample_version
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        return CaseSampleManifest.load(manifest_path)
    if output_dir.exists():
        raise ArtifactNotReadyError(f"incomplete case sample artifact exists: {output_dir}")

    records: list[CaseSampleRecord] = []
    for index, packet in enumerate(packets, start=1):
        case_id = f"case-{index:02d}"
        records.append(
            CaseSampleRecord(
                case_id=case_id,
                account_id=packet.account_id,
                packet_sha256=evidence_packet_sha256(packet),
                report_file=f"reports/{case_id}.md",
                evaluation_reason=(
                    "two independent human reviews have not yet been submitted; "
                    "case relationship truth is unavailable"
                ),
                packet=packet,
            )
        )
    manifest = CaseSampleManifest(
        case_sample_version=case_sample_version,
        score_version=score_version,
        data_version=data_version,
        model_version=model_version,
        created_at=datetime.now(UTC),
        case_count=len(records),
    )
    with atomic_artifact_directory(output_dir) as temporary_dir:
        (temporary_dir / manifest.cases_file).write_text(
            json.dumps(
                [record.model_dump(mode="json") for record in records],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        report_dir = temporary_dir / manifest.report_directory
        report_dir.mkdir()
        for record in records:
            (temporary_dir / record.report_file).write_text(
                render_pending_case_report(record.packet), encoding="utf-8"
            )
        review_template_dir = temporary_dir / manifest.review_template_directory
        review_template_dir.mkdir()
        for reviewer_slot, template_file in enumerate(
            manifest.primary_review_template_files, start=1
        ):
            review_slots = [
                _review_template_record(
                    record,
                    reviewer_id=f"REPLACE_WITH_REVIEWER_{reviewer_slot}_ID",
                    review_type="primary",
                )
                for record in records
            ]
            (temporary_dir / template_file).write_text(
                json.dumps(review_slots, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        adjudication_slots = [
            _review_template_record(
                record,
                reviewer_id="REPLACE_WITH_ADJUDICATOR_ID",
                review_type="adjudication",
            )
            for record in records
        ]
        (temporary_dir / manifest.adjudication_template_file).write_text(
            json.dumps(adjudication_slots, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (temporary_dir / "manifest.json").write_text(
            manifest.model_dump_json(indent=2), encoding="utf-8"
        )
    return manifest


def _review_template_record(
    record: CaseSampleRecord,
    *,
    reviewer_id: str,
    review_type: Literal["primary", "adjudication"],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "case_id": record.case_id,
        "packet_sha256": record.packet_sha256,
        "reviewer_id": reviewer_id,
        "reviewer_role": "反诈运营复核员",
        "reviewer_kind": "human",
        "review_type": review_type,
        "reviewed_at": "REPLACE_WITH_TIMEZONE_AWARE_ISO8601",
        "independence_attested": False,
        "evidence_consistent": None,
        "path_explanation_pass": None,
        "readability_pass": None,
        "evidence_types_verified": ["MODEL", "TRANSACTION"],
        "notes": "请独立填写复核意见，不得由模型自动生成签名或人工结论。",
    }
