from __future__ import annotations

from fraud_graph.domain import EvidencePacket


def render_markdown(packet: EvidencePacket) -> str:
    lines = [
        f"# 账户研判报告：{packet.account_id}",
        "",
        "## 风险摘要",
        "",
        f"- 风险分数：{packet.score.risk_score:.6f}",
        f"- 风险排序：{packet.score.risk_rank}",
        f"- 证据等级：{packet.grade.value}",
        f"- 可用于生产决策：{str(packet.score.production_eligible).lower()}",
        f"- 训练模式：`{packet.score.model_training_mode}`",
        f"- 特征集合：`{packet.score.model_feature_set}`",
        "- 上线阻断项：" + (", ".join(packet.score.eligibility_blockers) or "none"),
        f"- 模型版本：`{packet.score.model_version}`",
        f"- 数据版本：`{packet.score.data_version}`",
        f"- 特征截止时间：{packet.score.feature_as_of.isoformat()}",
        "",
        "## 证据清单",
        "",
    ]
    lines.extend(f"- `{fact.code}`: {fact.description} ({fact.value})" for fact in packet.facts)
    lines.extend(["", "## 资金链路说明", ""])
    if packet.paths:
        for path in packet.paths:
            lines.append(
                f"- 路径 `{path.path_id}`：方向={path.direction}，"
                f"可疑度={path.score:.4f}，跳数={len(path.steps)}，"
                f"证据码={','.join(path.evidence_codes)}"
            )
            for index, step in enumerate(path.steps, start=1):
                gap = "首笔" if step.gap_seconds is None else f"间隔={step.gap_seconds}秒"
                ratio = (
                    "无前序金额比"
                    if step.amount_ratio_to_previous is None
                    else f"前序金额比={step.amount_ratio_to_previous:.4f}"
                )
                lines.append(
                    f"  - 第{index}跳 `{step.transaction_id}`："
                    f"`{step.payer_account_id}` -> `{step.payee_account_id}`，"
                    f"时间={step.event_time_utc.isoformat()}，金额={step.amount}，{gap}，{ratio}"
                )
    else:
        lines.append("- 未发现满足约束的可疑路径。")
    lines.extend(["", "## 局限性", ""])
    lines.extend(f"- {item}" for item in packet.limitations)
    lines.extend(["", "## 辅助处置建议", ""])
    lines.extend(f"- {item}" for item in packet.recommended_actions)
    return "\n".join(lines) + "\n"
