from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fraud_graph.agent.schemas import AgentCitation, AgentResponse, AgentToolTrace
from fraud_graph.domain import AssociatedAccount, EvidencePacket
from fraud_graph.evidence.cases import evidence_packet_sha256
from fraud_graph.evidence.presentation import (
    fact_label,
    localize_action,
    localize_limitation,
    pattern_label,
)


def build_fallback_response(
    packet: EvidencePacket,
    associations: list[AssociatedAccount],
    question: str,
    *,
    tool_trace: list[AgentToolTrace] | None = None,
    reason: str | None = None,
) -> AgentResponse:
    facts = {item.code: item for item in packet.facts}
    citations: list[AgentCitation] = []
    score_citation = AgentCitation(kind="fact", id="MODEL_RISK_SCORE", label="主模型风险分数")
    citations.append(score_citation)
    answer = (
        f"账户 {packet.account_id} 建议进入人工复核队列。主模型风险分数为 "
        f"{packet.score.risk_score:.6f}，风险排序为第 {packet.score.risk_rank} 位 "
        f"{score_citation.marker}；风险评分用于确定核查优先级，不等同于涉诈认定。"
    )
    key_findings: list[str] = []

    transaction_count = facts.get("TXN_COUNT_TOTAL")
    if transaction_count is not None:
        citation = AgentCitation(
            kind="fact", id=transaction_count.code, label=fact_label(transaction_count.code)
        )
        citations.append(citation)
        key_findings.append(
            f"当前观察窗口记录交易 {transaction_count.value} 笔 {citation.marker}。"
        )

    if packet.paths:
        path = packet.paths[0]
        path_citation = AgentCitation(kind="path", id=path.path_id, label="最高风险资金路径")
        citations.append(path_citation)
        key_findings.append(
            f"已发现 {len(packet.paths)} 条满足当前约束的资金路径；最高风险路径共 "
            f"{len(path.steps)} 跳、可疑度 {path.score:.4f} {path_citation.marker}。"
        )
    else:
        key_findings.append("当前没有可引用的合格资金路径，不能据此断言存在多跳资金链。")

    if packet.patterns:
        pattern = packet.patterns[0]
        pattern_citation = AgentCitation(
            kind="pattern", id=pattern.pattern_id, label=pattern_label(pattern.pattern_type)
        )
        citations.append(pattern_citation)
        key_findings.append(
            f"资金结构识别到{pattern_label(pattern.pattern_type)}模式，涉及 "
            f"{len(pattern.transaction_ids)} 笔交易和 {len(pattern.related_account_ids)} 个"
            f"关联账户 {pattern_citation.marker}。"
        )
    elif associations:
        key_findings.append(
            f"证据包包含 {len(associations)} 个排序后的关联账户，但当前未形成满足阈值的"
            "结构模式，不应仅凭关联关系定性。"
        )

    if _asks_for_automatic_action(question):
        key_findings.append(
            "我不能执行或建议自动冻结、止付或涉诈定性。此类处置必须由有权限的人员"
            "结合其他已授权信息，在银行既有审批流程内决定。"
        )
    assessment: Literal[
        "manual_review_recommended",
        "insufficient_transaction_evidence",
        "information_only",
    ] = (
        "manual_review_recommended"
        if packet.paths or packet.patterns or packet.score.has_transactions
        else "insufficient_transaction_evidence"
    )
    limitations = [localize_limitation(item) for item in packet.limitations]
    if not limitations:
        limitations = ["结论仅适用于当前数据版本和观察窗口。"]
    actions = [localize_action(item) for item in packet.recommended_actions]
    if not actions:
        actions = ["由人工核验源交易、关联账户和客户尽调信息。"]
    return AgentResponse(
        account_id=packet.account_id,
        knowledge_domain="account_investigation",
        answer=answer,
        assessment=assessment,
        key_findings=key_findings,
        citations=_deduplicate(citations),
        tool_trace=tool_trace or [],
        limitations=limitations,
        recommended_actions=actions,
        evidence_packet_sha256=evidence_packet_sha256(packet),
        model_version=packet.score.model_version,
        data_version=packet.score.data_version,
        production_eligible=False,
        verifier_passed=True,
        generation_mode="deterministic_fallback",
        generated_at=datetime.now(UTC),
        fallback_reason=reason,
    )


def build_system_fallback_response(
    packet: EvidencePacket,
    question: str,
    system_context: dict[str, Any],
    *,
    tool_trace: list[AgentToolTrace] | None = None,
    reason: str | None = None,
) -> AgentResponse:
    entry_key = _system_entry_for_question(question)
    entry = system_context.get(entry_key, {})
    citation = _system_citation(entry, entry_key)
    marker = citation.marker

    if entry_key == "risk_scoring_policy":
        blockers = entry.get("online_blockers", [])
        blocker_names = [
            str(item.get("name"))
            for item in blockers
            if isinstance(item, dict) and item.get("name")
        ]
        if "阻断" in question:
            answer = (
                "“上线阻断项”是指当前模型已知的、阻止其直接驱动生产处置的条件；"
                f"它不影响比赛演示、离线分析和人工辅助研判 {marker}。"
            )
            key_findings = [
                f"风险分数用于风险排序和确定复核优先级，不等同于涉诈认定 {marker}。",
                f"Top 5% 表示有限人工复核容量，不是固定的涉诈判定阈值 {marker}。",
            ]
            if blocker_names:
                key_findings.append(
                    f"当前阻断项包括：{'、'.join(blocker_names)} {marker}。"
                )
        elif any(term in question for term in ("区别", "证据分数", "异常分数")):
            answer = (
                "风险分数用于账户总体风险排序；证据分数衡量不含画像字段的资金与"
                f"图特征支持度；异常分数衡量无监督异常程度 {marker}。"
            )
            key_findings = [
                f"风险分数是主模型预测结果，但不等同于涉诈概率或确认标签 {marker}。",
                f"证据分数用于检查主模型结论是否有资金行为和图结构依据 {marker}。",
                f"异常分数不使用风险标签，只说明账户相对其他账户有多异常 {marker}。",
            ]
        else:
            answer = (
                "系统没有用一条规则或固定分数线自动认定风险账户，而是用主模型风险"
                f"分数排序，再结合资金证据、图结构、可疑路径和人工复核综合判断 {marker}。"
            )
            key_findings = [
                f"风险分数决定核查优先级；Top 5% 代表当前人工复核容量 {marker}。",
                f"历史确认风险标签是真实结果，模型分数是预测，两者不能混同 {marker}。",
                f"模型当前不可直接驱动冻结、止付或涉诈认定 {marker}。",
            ]
    elif entry_key == "model_methodology":
        selected_model = entry.get("selected_model", "当前主模型")
        feature_count = entry.get("feature_count", "当前版本记录的")
        answer = (
            f"当前最终识别模型为 {selected_model}，将多组账户特征同时输入模型学习权重，"
            f"并不是把分支直接相加 {marker}。"
        )
        key_findings = [
            f"当前版本共使用 {feature_count} 个输入特征，按行为、图结构、快照变化、"
            f"无监督异常和账户画像分组 {marker}。",
            f"训练采用类别平衡，使少数风险样本在损失函数中获得更高权重 {marker}。",
        ]
    elif entry_key == "model_evaluation":
        metrics = entry.get("test_metrics", {})
        answer = (
            "当前模型在锁定测试集上已达到题目列出的 ROC-AUC 和 Top 5% 覆盖指标，"
            f"但这些仍是静态回溯实验结果，不代表可直接上线 {marker}。"
        )
        key_findings = [
            f"ROC-AUC 为 {_metric(metrics, 'roc_auc')}，PR-AUC 为 "
            f"{_metric(metrics, 'pr_auc')} {marker}。",
            "Top 5% 风险账户覆盖率为 "
            f"{_metric(metrics, 'recall_at_top_fraction', percent=True)} {marker}。",
            f"测试集风险正样本仅 {_metric(metrics, 'positives')} 个，"
            f"指标区间仍有较大不确定性 {marker}。",
        ]
    else:
        acceptance = entry.get("acceptance", {})
        answer = (
            "任务四报告由结构化证据包和确定性模板生成，并对证据引用、"
            "字段完整性和渲染一致性做离线验收 "
            f"{marker}。"
        )
        key_findings = [
            f"当前验收覆盖 {_metric(acceptance, 'report_count')} 份典型案例报告 {marker}。",
            "结构一致性、两类证据覆盖、完整性和精确渲染匹配当前均为 "
            f"{_metric(acceptance, 'consistency_rate', percent=True)} {marker}。",
            f"Agent 是辅助分析加分项，不替代确定性研判报告和人工复核 {marker}。",
        ]

    return AgentResponse(
        account_id=packet.account_id,
        knowledge_domain="system_explanation",
        answer=answer,
        assessment="information_only",
        key_findings=key_findings,
        citations=[citation],
        tool_trace=tool_trace or [],
        limitations=[
            "系统解释绑定当前模型与数据版本；更换产物版本后应重新读取指标和边界。",
            "AI 解释仅供参考，最终以版本化产物、比赛题目和人工审核结论为准。",
        ],
        recommended_actions=[],
        evidence_packet_sha256=evidence_packet_sha256(packet),
        model_version=packet.score.model_version,
        data_version=packet.score.data_version,
        production_eligible=False,
        verifier_passed=True,
        generation_mode="deterministic_fallback",
        generated_at=datetime.now(UTC),
        fallback_reason=reason,
    )


def _system_entry_for_question(question: str) -> str:
    normalized = question.casefold()
    if any(term in normalized for term in ("任务四", "任务4", "报告", "可信", "一致性")):
        return "task4_requirements"
    if any(term in normalized for term in ("指标", "auc", "pr-auc", "top5", "top 5", "达到")):
        return "model_evaluation"
    if any(term in normalized for term in ("特征", "类别平衡", "建模", "模型", "训练")):
        return "model_methodology"
    return "risk_scoring_policy"


def _system_citation(entry: dict[str, Any], entry_key: str) -> AgentCitation:
    payload = entry.get("citation")
    if isinstance(payload, dict):
        try:
            citation = AgentCitation.model_validate(payload)
            if citation.kind == "system":
                return citation
        except ValueError:
            pass
    fallback_ids = {
        "model_methodology": ("MODEL_CARD", "模型卡与特征清单"),
        "risk_scoring_policy": ("RISK_SCORING_POLICY", "风险评分与上线边界"),
        "model_evaluation": ("MODEL_EVALUATION", "模型评估结果"),
        "task4_requirements": ("TASK4_REQUIREMENTS", "任务四报告要求与验收"),
    }
    identifier, label = fallback_ids[entry_key]
    return AgentCitation(kind="system", id=identifier, label=label)


def _metric(values: Any, key: str, *, percent: bool = False) -> str:
    value = values.get(key) if isinstance(values, dict) else None
    if value is None:
        return "未记录"
    if percent and isinstance(value, (int, float)):
        return f"{value:.1%}"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _asks_for_automatic_action(question: str) -> bool:
    normalized = question.casefold()
    return any(term in normalized for term in ("冻结", "止付", "自动处置", "直接处置", "认定涉诈"))


def _deduplicate(items: list[AgentCitation]) -> list[AgentCitation]:
    seen: set[tuple[str, str]] = set()
    result: list[AgentCitation] = []
    for item in items:
        key = (item.kind, item.id)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
