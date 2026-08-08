from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field, field_validator, model_validator

from fraud_graph.domain import EvidenceGrade, EvidencePacket
from fraud_graph.errors import ProductionSafetyError


class AssociationEvaluation(BaseModel):
    evaluable: bool
    top_k: int
    evaluated_roots: int = 0
    root_hit_rate: float | None = None
    associated_precision: float | None = None
    associated_recall: float | None = None
    hits: int = 0
    predicted_accounts: int = 0
    true_accounts: int = 0
    blockers: list[str] = Field(default_factory=list)


class ManualCaseEvaluation(BaseModel):
    evaluable: bool
    acceptance_passed: bool = False
    required_case_count: int = 5
    required_reviewers_per_case: int = 2
    required_case_pass_rate: float = 0.7
    submitted_reviews: int = 0
    human_reviews: int = 0
    automated_prechecks: int = 0
    reviewed_cases: int = 0
    resolved_cases: int = 0
    case_pass_rate: float | None = None
    evidence_pass_rate: float | None = None
    path_explanation_pass_rate: float | None = None
    readability_pass_rate: float | None = None
    adjudication_required_cases: list[str] = Field(default_factory=list)
    case_results: list[dict[str, Any]] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class ReportConsistencyResult(BaseModel):
    evaluable: bool
    acceptance_passed: bool
    packet_count: int
    valid_count: int
    consistency_rate: float | None
    evidence_requirement_rate: float | None
    completeness_rate: float | None
    issues: list[dict[str, Any]] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class ManualCaseReview(BaseModel):
    """One review bound to an immutable evidence packet."""

    schema_version: Literal["1.0"] = "1.0"
    case_id: str = Field(min_length=1)
    packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_id: str = Field(min_length=1)
    reviewer_role: str = Field(min_length=1)
    reviewer_kind: Literal["human", "automated_precheck"]
    review_type: Literal["primary", "adjudication"] = "primary"
    reviewed_at: datetime
    independence_attested: bool
    evidence_consistent: bool
    path_explanation_pass: bool
    readability_pass: bool
    evidence_types_verified: list[
        Literal["MODEL", "BEHAVIOR", "TRANSACTION", "GRAPH_STRUCTURE"]
    ] = Field(min_length=1)
    notes: str = ""

    @field_validator("reviewed_at")
    @classmethod
    def reviewed_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reviewed_at must include a timezone")
        return value

    @field_validator("evidence_types_verified")
    @classmethod
    def evidence_types_must_be_distinct(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("evidence_types_verified must not contain duplicates")
        return value

    @model_validator(mode="after")
    def human_review_must_attest_independence(self) -> ManualCaseReview:
        if self.reviewer_kind == "human" and not self.independence_attested:
            raise ValueError("human reviewers must attest independent review")
        if (
            self.evidence_consistent
            and self.path_explanation_pass
            and self.readability_pass
            and len(self.evidence_types_verified) < 2
        ):
            raise ValueError("a passing review must verify at least two evidence types")
        return self


def evaluate_case_associations(
    predictions: dict[str, list[str]],
    labels: pd.DataFrame,
    *,
    top_k: int = 20,
    positive_labels: tuple[int, ...] = (1,),
) -> AssociationEvaluation:
    """Evaluate account associations only when case-level relationship truth exists."""
    if top_k < 1:
        raise ValueError("top_k must be positive")
    required = {"account_id", "label", "case_id"}
    missing = required - set(labels.columns)
    if missing:
        raise ProductionSafetyError(
            "association evaluation is blocked: case-level truth is missing "
            f"({', '.join(sorted(missing))})"
        )
    usable = labels.loc[labels["case_id"].notna()].copy()
    usable["case_id"] = usable["case_id"].astype(str).str.strip()
    usable = usable.loc[usable["case_id"] != ""]
    if usable.empty:
        raise ProductionSafetyError(
            "association evaluation is blocked: no non-empty case_id values are available"
        )
    case_members: dict[str, set[str]] = defaultdict(set)
    for row in usable[["account_id", "case_id"]].itertuples(index=False):
        case_members[str(row.case_id)].add(str(row.account_id))
    case_members = {key: value for key, value in case_members.items() if len(value) >= 2}
    if not case_members:
        raise ProductionSafetyError(
            "association evaluation is blocked: no case contains at least two accounts"
        )

    account_to_cases: dict[str, set[str]] = defaultdict(set)
    for case_id, members in case_members.items():
        for account_id in members:
            account_to_cases[account_id].add(case_id)
    positive_accounts = set(
        usable.loc[usable["label"].isin(positive_labels), "account_id"].astype(str)
    )
    root_truth: dict[str, set[str]] = {}
    for root in sorted(set(predictions) & positive_accounts):
        truth: set[str] = set()
        for case_id in account_to_cases.get(root, set()):
            truth.update(case_members[case_id] - {root})
        if truth:
            root_truth[root] = truth
    if not root_truth:
        raise ProductionSafetyError(
            "association evaluation is blocked: no predicted positive root has relationship truth"
        )

    root_hits = 0
    predicted_count = 0
    true_count = 0
    hit_count = 0
    for root, truth in root_truth.items():
        predicted = set(str(value) for value in predictions.get(root, [])[:top_k]) - {root}
        hits = predicted & truth
        root_hits += int(bool(hits))
        predicted_count += len(predicted)
        true_count += len(truth)
        hit_count += len(hits)
    roots = len(root_truth)
    return AssociationEvaluation(
        evaluable=True,
        top_k=top_k,
        evaluated_roots=roots,
        root_hit_rate=root_hits / roots,
        associated_precision=hit_count / predicted_count if predicted_count else 0.0,
        associated_recall=hit_count / true_count if true_count else 0.0,
        hits=hit_count,
        predicted_accounts=predicted_count,
        true_accounts=true_count,
    )


def evaluate_manual_cases(
    reviews: list[dict[str, Any]],
    case_bindings: dict[str, str],
    *,
    required_case_count: int = 5,
    required_reviewers_per_case: int = 2,
    required_case_pass_rate: float = 0.7,
) -> ManualCaseEvaluation:
    """Evaluate hash-bound, independent human reviews with conflict adjudication."""
    if not reviews:
        raise ProductionSafetyError(
            "manual case evaluation is blocked: reviewer-labelled cases are required"
        )
    if len(case_bindings) < required_case_count:
        raise ProductionSafetyError(
            "manual case evaluation is blocked: the case package contains fewer than "
            f"{required_case_count} cases"
        )
    if required_reviewers_per_case < 2:
        raise ValueError("required_reviewers_per_case must be at least two")
    if not 0.0 <= required_case_pass_rate <= 1.0:
        raise ValueError("required_case_pass_rate must be between zero and one")

    parsed = [ManualCaseReview.model_validate(review) for review in reviews]
    seen_submissions: set[tuple[str, str, str]] = set()
    for review in parsed:
        expected_hash = case_bindings.get(review.case_id)
        if expected_hash is None:
            raise ProductionSafetyError(
                f"manual case evaluation is blocked: unknown case_id {review.case_id}"
            )
        if review.packet_sha256 != expected_hash:
            raise ProductionSafetyError(
                "manual case evaluation is blocked: evidence packet hash mismatch for "
                f"{review.case_id}"
            )
        key = (review.case_id, review.reviewer_id, review.review_type)
        if key in seen_submissions:
            raise ProductionSafetyError(
                "manual case evaluation is blocked: duplicate reviewer submission for "
                f"{review.case_id}/{review.reviewer_id}/{review.review_type}"
            )
        seen_submissions.add(key)

    automated = [item for item in parsed if item.reviewer_kind == "automated_precheck"]
    human = [item for item in parsed if item.reviewer_kind == "human"]
    grouped: dict[str, list[ManualCaseReview]] = defaultdict(list)
    for review in human:
        grouped[review.case_id].append(review)

    blockers: list[str] = []
    case_results: list[dict[str, Any]] = []
    adjudication_required: list[str] = []
    resolved_values: list[tuple[bool, bool, bool]] = []
    criteria = ("evidence_consistent", "path_explanation_pass", "readability_pass")
    for case_id in sorted(case_bindings):
        case_reviews = grouped.get(case_id, [])
        primary = [item for item in case_reviews if item.review_type == "primary"]
        adjudication = [item for item in case_reviews if item.review_type == "adjudication"]
        primary_reviewer_ids = {item.reviewer_id for item in primary}
        if len(primary_reviewer_ids) < required_reviewers_per_case:
            case_results.append(
                {
                    "case_id": case_id,
                    "status": "insufficient_human_reviews",
                    "primary_reviewer_count": len(primary_reviewer_ids),
                }
            )
            continue
        if len(adjudication) > 1:
            raise ProductionSafetyError(
                "manual case evaluation is blocked: more than one adjudication was supplied "
                f"for {case_id}"
            )
        if adjudication and adjudication[0].reviewer_id in primary_reviewer_ids:
            raise ProductionSafetyError(
                "manual case evaluation is blocked: adjudicator must be independent of primary "
                f"reviewers for {case_id}"
            )

        disagreements = [
            criterion
            for criterion in criteria
            if len({bool(getattr(item, criterion)) for item in primary}) > 1
        ]
        if disagreements and not adjudication:
            adjudication_required.append(case_id)
            case_results.append(
                {
                    "case_id": case_id,
                    "status": "adjudication_required",
                    "primary_reviewer_count": len(primary_reviewer_ids),
                    "disagreements": disagreements,
                }
            )
            continue
        source = adjudication[0] if adjudication else primary[0]
        values = (
            source.evidence_consistent,
            source.path_explanation_pass,
            source.readability_pass,
        )
        evidence_ok, path_ok, readability_ok = values
        resolved_values.append(values)
        case_results.append(
            {
                "case_id": case_id,
                "status": "passed" if all(values) else "failed",
                "primary_reviewer_count": len(primary_reviewer_ids),
                "adjudicated": bool(adjudication),
                "evidence_consistent": evidence_ok,
                "path_explanation_pass": path_ok,
                "readability_pass": readability_ok,
            }
        )

    resolved_count = len(resolved_values)
    reviewed_count = sum(
        result["status"] != "insufficient_human_reviews" for result in case_results
    )
    if resolved_count < required_case_count:
        blockers.append(
            f"RESOLVED_HUMAN_CASES_BELOW_MINIMUM:{resolved_count}/{required_case_count}"
        )
    if adjudication_required:
        blockers.append("UNRESOLVED_REVIEWER_DISAGREEMENTS")
    if not human:
        blockers.append("NO_HUMAN_REVIEWS")

    def rate(index: int) -> float | None:
        if not resolved_values:
            return None
        return sum(values[index] for values in resolved_values) / resolved_count

    case_pass_rate = (
        sum(all(values) for values in resolved_values) / resolved_count if resolved_values else None
    )
    evaluable = not blockers
    acceptance_passed = bool(
        evaluable and case_pass_rate is not None and case_pass_rate >= required_case_pass_rate
    )
    return ManualCaseEvaluation(
        evaluable=evaluable,
        acceptance_passed=acceptance_passed,
        required_case_count=required_case_count,
        required_reviewers_per_case=required_reviewers_per_case,
        required_case_pass_rate=required_case_pass_rate,
        submitted_reviews=len(parsed),
        human_reviews=len(human),
        automated_prechecks=len(automated),
        reviewed_cases=reviewed_count,
        resolved_cases=resolved_count,
        case_pass_rate=case_pass_rate,
        evidence_pass_rate=rate(0),
        path_explanation_pass_rate=rate(1),
        readability_pass_rate=rate(2),
        adjudication_required_cases=adjudication_required,
        case_results=case_results,
        blockers=blockers,
    )


def validate_evidence_packet(packet: EvidencePacket) -> list[str]:
    """Validate that report citations are facts from the packet's paths."""
    errors: list[str] = []
    if packet.account_id != packet.score.account_id:
        errors.append("ACCOUNT_SCORE_MISMATCH")
    path_transaction_ids = {step.transaction_id for path in packet.paths for step in path.steps}
    path_ids = [path.path_id for path in packet.paths]
    if len(path_ids) != len(set(path_ids)):
        errors.append("DUPLICATE_PATH_ID")
    fact_codes = [fact.code for fact in packet.facts]
    if len(fact_codes) != len(set(fact_codes)):
        errors.append("DUPLICATE_FACT_CODE")
    risk_facts = [fact for fact in packet.facts if fact.code == "MODEL_RISK_SCORE"]
    if len(risk_facts) != 1:
        errors.append("MODEL_RISK_SCORE_FACT_COUNT")
    elif (
        not isinstance(risk_facts[0].value, (int, float))
        or abs(float(risk_facts[0].value) - packet.score.risk_score) > 1e-6
    ):
        errors.append("MODEL_RISK_SCORE_FACT_MISMATCH")
    for fact in packet.facts:
        if not set(fact.transaction_ids).issubset(path_transaction_ids):
            errors.append(f"FACT_TRANSACTION_NOT_IN_PATH:{fact.code}")
    for path in packet.paths:
        if path.root_account_id != packet.account_id:
            errors.append(f"PATH_DECLARED_ROOT_MISMATCH:{path.path_id}")
        if not path.steps:
            errors.append(f"EMPTY_PATH:{path.path_id}")
            continue
        if path.direction == "outgoing" and path.steps[0].payer_account_id != packet.account_id:
            errors.append(f"PATH_ROOT_MISMATCH:{path.path_id}")
        if path.direction == "incoming" and path.steps[-1].payee_account_id != packet.account_id:
            errors.append(f"PATH_ROOT_MISMATCH:{path.path_id}")
        for previous, current in zip(path.steps, path.steps[1:], strict=False):
            if previous.payee_account_id != current.payer_account_id:
                errors.append(f"PATH_EDGE_DISCONNECTED:{path.path_id}")
            if current.event_time_utc <= previous.event_time_utc:
                errors.append(f"PATH_TIME_NOT_INCREASING:{path.path_id}")
    multi_hop = any(len(path.steps) >= 2 for path in packet.paths)
    expected_grade = (
        EvidenceGrade.A
        if multi_hop
        else EvidenceGrade.B
        if packet.score.has_transactions
        else EvidenceGrade.C
    )
    if packet.grade != expected_grade:
        errors.append("EVIDENCE_GRADE_MISMATCH")
    if packet.score.has_transactions and not packet.paths:
        errors.append("TRANSACTION_ACCOUNT_WITHOUT_PATH_EVIDENCE")
    if not packet.score.has_transactions and packet.paths:
        errors.append("INACTIVE_ACCOUNT_WITH_PATH_EVIDENCE")
    if not packet.facts:
        errors.append("MISSING_EVIDENCE_LIST")
    if not packet.limitations:
        errors.append("MISSING_LIMITATIONS")
    if not packet.recommended_actions:
        errors.append("MISSING_RECOMMENDED_ACTIONS")
    if packet.score.model_training_mode == "retrospective_static":
        if packet.score.production_eligible:
            errors.append("STATIC_RETROSPECTIVE_MARKED_PRODUCTION_ELIGIBLE")
        required_blockers = {"NON_TEMPORAL_DIAGNOSTIC_EVALUATION", "LABEL_TIME_MISSING"}
        if not required_blockers.issubset(packet.score.eligibility_blockers):
            errors.append("STATIC_RETROSPECTIVE_BLOCKERS_MISSING")
    evidence_types = {"MODEL"}
    if any(
        fact.code not in {"MODEL_RISK_SCORE"} and not fact.transaction_ids for fact in packet.facts
    ):
        evidence_types.add("BEHAVIOR")
    if path_transaction_ids:
        evidence_types.add("TRANSACTION")
    if len(evidence_types) < 2:
        errors.append("INSUFFICIENT_EVIDENCE_TYPES")
    return errors


def evaluate_report_consistency(packets: list[EvidencePacket]) -> ReportConsistencyResult:
    if not packets:
        raise ProductionSafetyError(
            "report consistency evaluation is blocked: no generated reports were supplied"
        )
    issues: list[dict[str, Any]] = []
    valid_count = 0
    evidence_ok = 0
    for packet in packets:
        packet_errors = validate_evidence_packet(packet)
        if not any(item == "INSUFFICIENT_EVIDENCE_TYPES" for item in packet_errors):
            evidence_ok += 1
        if not packet_errors:
            valid_count += 1
        if packet_errors:
            issues.append({"account_id": packet.account_id, "errors": packet_errors})
    total = len(packets)
    consistency_rate = valid_count / total
    evidence_requirement_rate = evidence_ok / total
    complete_count = sum(
        bool(packet.facts and packet.limitations and packet.recommended_actions)
        and (bool(packet.paths) or not packet.score.has_transactions)
        for packet in packets
    )
    completeness_rate = complete_count / total
    return ReportConsistencyResult(
        evaluable=True,
        acceptance_passed=(
            consistency_rate == 1.0
            and evidence_requirement_rate == 1.0
            and completeness_rate == 1.0
        ),
        packet_count=total,
        valid_count=valid_count,
        consistency_rate=consistency_rate,
        evidence_requirement_rate=evidence_requirement_rate,
        completeness_rate=completeness_rate,
        issues=issues,
    )
