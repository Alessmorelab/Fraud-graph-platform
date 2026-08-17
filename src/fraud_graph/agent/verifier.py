from __future__ import annotations

import re

from fraud_graph.agent.schemas import AgentCitation, AgentResponse, CitationKind
from fraud_graph.domain import EvidencePacket
from fraud_graph.evidence.presentation import fact_label, pattern_label

MARKER_PATTERN = re.compile(r"\[(事实|路径|交易|模式|系统):([^\]]+)\]")
PREFIX_TO_KIND: dict[str, CitationKind] = {
    "事实": "fact",
    "路径": "path",
    "交易": "transaction",
    "模式": "pattern",
    "系统": "system",
}


class EvidenceVerificationError(ValueError):
    pass


def reconcile_agent_citations(
    response: AgentResponse,
    packet: EvidencePacket,
    system_context: dict[str, object] | None = None,
) -> bool:
    """Align the citation list with valid evidence markers used in the narrative."""
    catalog = _citation_catalog(packet, system_context)
    existing = {(item.kind, item.id): item for item in response.citations}
    normalized: list[AgentCitation] = []
    seen: set[tuple[str, str]] = set()
    for prefix, identifier in MARKER_PATTERN.findall(_answer_narrative(response)):
        kind = PREFIX_TO_KIND[prefix]
        key = (kind, identifier)
        if key not in catalog:
            raise EvidenceVerificationError(
                f"无效或跨账户证据引用：{kind}:{identifier}"
            )
        if key in seen:
            continue
        seen.add(key)
        normalized.append(existing.get(key, catalog[key]))
    changed = [item.model_dump() for item in normalized] != [
        item.model_dump() for item in response.citations
    ]
    response.citations = normalized
    response.citation_repair_applied = changed
    return changed


def verify_agent_response(
    response: AgentResponse,
    packet: EvidencePacket,
    system_context: dict[str, object] | None = None,
) -> None:
    if response.account_id != packet.account_id:
        raise EvidenceVerificationError("Agent 响应账户与证据包不一致")
    catalog = _citation_catalog(packet, system_context)
    narrative = _answer_narrative(response)
    if not response.citations:
        raise EvidenceVerificationError("Agent 响应没有证据引用")
    for citation in response.citations:
        if response.knowledge_domain == "system_explanation" and citation.kind != "system":
            raise EvidenceVerificationError("系统解释不得引用账户级证据")
        if response.knowledge_domain == "account_investigation" and citation.kind == "system":
            raise EvidenceVerificationError("账户研判不得引用系统解释证据")
        if (citation.kind, citation.id) not in catalog:
            raise EvidenceVerificationError(
                f"无效或跨账户证据引用：{citation.kind}:{citation.id}"
            )
        if citation.marker not in narrative:
            raise EvidenceVerificationError(f"回答正文未使用引用：{citation.marker}")
    declared_markers = {citation.marker for citation in response.citations}
    markers_in_answer = {
        f"[{prefix}:{identifier}]" for prefix, identifier in MARKER_PATTERN.findall(narrative)
    }
    undeclared = markers_in_answer - declared_markers
    if undeclared:
        raise EvidenceVerificationError(f"正文包含未声明引用：{', '.join(sorted(undeclared))}")
    prohibited = (
        "应立即冻结",
        "建议自动冻结",
        "建议自动止付",
        "确认该账户涉诈",
        "认定该账户涉诈",
        "直接执行止付",
    )
    safety_text = "\n".join(
        [narrative, *response.limitations, *response.recommended_actions]
    )
    for value in prohibited:
        start = 0
        while True:
            index = safety_text.find(value, start)
            if index < 0:
                break
            context = safety_text[max(0, index - 6) : index]
            if not any(
                context.endswith(prefix) for prefix in ("不能执行或", "不能", "不得", "不应")
            ):
                raise EvidenceVerificationError("Agent 响应包含越权处置或无依据定性")
            start = index + len(value)
    if response.production_eligible:
        raise EvidenceVerificationError("Agent 响应不得标记为可直接生产处置")


def _answer_narrative(response: AgentResponse) -> str:
    return "\n".join([response.answer, *response.key_findings])


def _citation_catalog(
    packet: EvidencePacket,
    system_context: dict[str, object] | None = None,
) -> dict[tuple[str, str], AgentCitation]:
    catalog: dict[tuple[str, str], AgentCitation] = {
        ("fact", "MODEL_RISK_SCORE"): AgentCitation(
            kind="fact",
            id="MODEL_RISK_SCORE",
            label=fact_label("MODEL_RISK_SCORE"),
        )
    }
    for fact in packet.facts:
        catalog[("fact", fact.code)] = AgentCitation(
            kind="fact",
            id=fact.code,
            label=fact_label(fact.code),
        )
    for path in packet.paths:
        catalog[("path", path.path_id)] = AgentCitation(
            kind="path",
            id=path.path_id,
            label="可疑资金路径",
        )
        for step in path.steps:
            catalog[("transaction", step.transaction_id)] = AgentCitation(
                kind="transaction",
                id=step.transaction_id,
                label="源交易",
            )
    for pattern in packet.patterns:
        catalog[("pattern", pattern.pattern_id)] = AgentCitation(
            kind="pattern",
            id=pattern.pattern_id,
            label=pattern_label(pattern.pattern_type),
        )
        for transaction_id in pattern.transaction_ids:
            catalog[("transaction", transaction_id)] = AgentCitation(
                kind="transaction",
                id=transaction_id,
                label="源交易",
            )
    for entry in (system_context or {}).values():
        if not isinstance(entry, dict) or "citation" not in entry:
            continue
        try:
            citation = AgentCitation.model_validate(entry["citation"])
        except (TypeError, ValueError):
            continue
        if citation.kind == "system":
            catalog[(citation.kind, citation.id)] = citation
    return catalog
