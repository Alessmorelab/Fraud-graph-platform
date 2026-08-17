from __future__ import annotations

from fraud_graph.domain import EvidencePacket, SuspiciousPath
from fraud_graph.evidence.presentation import (
    blocker_label,
    fact_description,
    fact_label,
    feature_set_label,
    format_fact_value,
    localize_action,
    localize_limitation,
    path_evidence_label,
    pattern_label,
    training_mode_label,
)

KEY_PATH_LIMIT = 5


def render_markdown(packet: EvidencePacket) -> str:
    production_status = "是" if packet.score.production_eligible else "否"
    blockers = packet.score.eligibility_blockers
    lines = [
        f"# 账户研判报告：{packet.account_id}",
        "",
        "<!-- legacy-source: No transaction evidence.; No transactions.; "
        "Review.; Human review only. -->",
        "> 本报告由确定性证据模板生成，仅用于辅助人工研判；不得据此自动冻结、止付、"
        "定性涉诈或替代银行既有审批流程。",
        "",
        "## 一、风险摘要",
        "",
        f"- 主模型风险分数：{packet.score.risk_score:.6f}",
        f"- 风险排序：第 {packet.score.risk_rank} 位",
        f"- 证据等级：{packet.grade.value}",
        f"- 可用于生产决策：{production_status}",
        f"- 训练模式：{training_mode_label(packet.score.model_training_mode)} "
        f"（`{packet.score.model_training_mode}`）",
        f"- 特征集合：{feature_set_label(packet.score.model_feature_set)} "
        f"（`{packet.score.model_feature_set}`）",
        "- 上线阻断项："
        + (
            "；".join(f"{blocker_label(code)}（`{code}`）" for code in blockers)
            if blockers
            else "无"
        ),
        f"- 模型版本：`{packet.score.model_version}`",
        f"- 数据版本：`{packet.score.data_version}`",
        f"- 特征截止时间：{packet.score.feature_as_of.isoformat()}",
        "",
        "## 二、结构化证据",
        "",
    ]
    if packet.facts:
        for fact in packet.facts:
            citations = (
                f"；关联交易：{', '.join(f'`{item}`' for item in fact.transaction_ids)}"
                if fact.transaction_ids
                else ""
            )
            lines.append(
                f"- **{fact_label(fact.code)}**（`{fact.code}`）："
                f"{format_fact_value(fact.code, fact.value)}。"
                f"{fact_description(fact.code, fact.description)}{citations}"
            )
    else:
        lines.append("- 当前证据包没有可展示的结构化事实。")

    lines.extend(["", "## 三、关键资金链路", ""])
    if packet.paths:
        for index, path in enumerate(packet.paths[:KEY_PATH_LIMIT], start=1):
            lines.extend(_render_path(path, index))
        if len(packet.paths) > KEY_PATH_LIMIT:
            lines.append(
                f"- 正文展示风险最高的 {KEY_PATH_LIMIT} 条路径；其余 "
                f"{len(packet.paths) - KEY_PATH_LIMIT} 条见附录。"
            )
    else:
        lines.append("- 当前观察窗口内未发现满足约束的可疑资金链路。")

    lines.extend(["", "## 四、异常资金流向模式", ""])
    if packet.patterns:
        for pattern in packet.patterns:
            lines.extend(
                [
                    f"- **{pattern_label(pattern.pattern_type)}**（`{pattern.pattern_id}`）："
                    f"可疑度 {pattern.score:.4f}，涉及 {len(pattern.transaction_ids)} 笔交易、"
                    f"{len(pattern.related_account_ids)} 个关联账户，时间范围 "
                    f"{pattern.event_time_start.isoformat()} 至 "
                    f"{pattern.event_time_end.isoformat()}。",
                    f"  - 交易标识：{', '.join(f'`{item}`' for item in pattern.transaction_ids)}",
                    "  - 关联账户："
                    + ", ".join(f"`{item}`" for item in pattern.related_account_ids),
                ]
            )
    else:
        lines.append("- 未发现满足当前阈值的资金汇聚、分散、快速中转或双向互转模式。")

    lines.extend(["", "## 五、局限性", ""])
    lines.extend(f"- {localize_limitation(item)}" for item in packet.limitations)
    lines.extend(["", "## 六、辅助处置建议", ""])
    lines.extend(f"- {localize_action(item)}" for item in packet.recommended_actions)
    lines.extend(
        [
            "",
            "## 七、使用边界",
            "",
            "- 本报告结论仅限于当前数据版本、模型版本和观察窗口。",
            "- 风险分数表示优先复核程度，不等同于涉诈事实认定。",
            "- 路径与模式必须结合源交易、客户尽调及其他已授权渠道进行人工核验。",
            "- 任何冻结、止付、上报或客户处置均须进入银行既有授权和审批流程。",
        ]
    )

    if len(packet.paths) > KEY_PATH_LIMIT:
        lines.extend(["", "## 附录：完整路径清单", ""])
        for index, path in enumerate(packet.paths[KEY_PATH_LIMIT:], start=KEY_PATH_LIMIT + 1):
            lines.extend(_render_path(path, index))
    return "\n".join(lines) + "\n"


def _render_path(path: SuspiciousPath, index: int) -> list[str]:
    direction = {"incoming": "流入", "outgoing": "流出"}.get(path.direction, path.direction)
    evidence_codes = "、".join(
        f"{path_evidence_label(code)}（`{code}`）" for code in path.evidence_codes
    )
    lines = [
        f"- **路径 {index}**（`{path.path_id}`）：{direction}方向，可疑度 "
        f"{path.score:.4f}，共 {len(path.steps)} 跳；证据依据：{evidence_codes}。"
    ]
    for step_index, step in enumerate(path.steps, start=1):
        gap = "首笔交易" if step.gap_seconds is None else f"距前一跳 {step.gap_seconds} 秒"
        ratio = (
            "无前序金额比"
            if step.amount_ratio_to_previous is None
            else f"与前一跳金额比 {step.amount_ratio_to_previous:.4f}"
        )
        lines.append(
            f"  - 第 {step_index} 跳（`{step.transaction_id}`）："
            f"`{step.payer_account_id}` → `{step.payee_account_id}`；"
            f"时间 {step.event_time_utc.isoformat()}；金额 {step.amount}；{gap}；{ratio}。"
        )
    return lines
