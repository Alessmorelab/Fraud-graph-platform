from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from fraud_graph.domain import EvidenceFact, EvidencePacket, FlowPattern, SuspiciousPath

FACT_LABELS = {
    "MODEL_RISK_SCORE": "主模型风险分数",
    "TXN_COUNT_TOTAL": "观察窗口交易笔数",
    "COUNTERPARTY_COUNT_TOTAL": "交易对手数量",
    "SAME_DAY_IN_OUT_DAYS": "同日收付活跃天数",
    "SAME_DAY_TURNOVER_RATIO_AVG": "同日资金周转比例",
    "CITED_DIRECT_TRANSACTIONS": "直接交易关系",
    "SUPPORTED_MULTI_HOP_PATHS": "多跳时序资金链",
    "FLOW_PATTERNS": "异常资金流向模式",
}

BLOCKER_LABELS = {
    "LABEL_TIME_MISSING": "标签发生时间缺失",
    "RETROSPECTIVE_STATIC_TARGET": "静态回溯目标",
    "TRANSDUCTIVE_GRAPH_EVALUATION": "图特征为传导式评估",
    "MODEL_APPROVAL_REQUIRED": "模型尚待审批",
    "ACTIVE_POSITIVES_TOO_FEW": "有交易正样本过少",
    "PROFILE_SHORTCUT_DEPENDENCY": "存在画像捷径依赖",
    "NON_TEMPORAL_DIAGNOSTIC_EVALUATION": "非时序诊断评估",
    "REFERENCE_MODEL_NOT_APPROVED": "参考模型尚待审批",
}

PATTERN_LABELS = {
    "FAN_IN": "资金汇聚",
    "FAN_OUT": "资金分散",
    "PASS_THROUGH": "快速中转",
    "RECIPROCAL": "双向互转",
}

LIMITATION_TRANSLATIONS = {
    "Only direct transaction relationships were found; no qualifying multi-hop temporal "
    "path was established.": "仅发现直接交易关系，尚未形成满足约束的多跳时序资金链。",
    "No qualifying multi-hop temporal path was found in the configured window.": (
        "当前观察窗口内未发现满足约束的多跳时序资金链。"
    ),
    "No transaction was observed in the configured window; no funding path can be asserted.": (
        "当前观察窗口内未发现交易，因此不能认定存在资金链路。"
    ),
    "Model output is decision support and does not authorize automatic freezing.": (
        "模型结果仅用于辅助研判，不构成自动止付或冻结的依据。"
    ),
    "The referenced model is not production-eligible and must not drive an operational "
    "decision.": "当前模型尚不具备生产决策资格，不得直接驱动业务处置。",
    "No transaction evidence.": "当前没有可核验的交易证据。",
    "No transactions.": "当前观察窗口内未发现交易。",
}

ACTION_TRANSLATIONS = {
    "Prioritize manual case review.": "优先进入人工案例复核队列。",
    "Verify the cited transactions and related accounts against authorized internal data.": (
        "结合已授权的内部数据，核验报告引用的交易和关联账户。"
    ),
    "Escalate through the bank's existing approval workflow when corroborated.": (
        "取得其他证据印证后，按银行现有审批流程升级处置。"
    ),
    "Review behavioral anomalies and expand the observation window.": (
        "复核行为异常，并视需要扩大交易观察窗口。"
    ),
    "Check additional authorized channels before escalation.": (
        "升级处置前，通过其他已授权渠道补充核验。"
    ),
    "Treat the score as unsupported by observed transaction evidence.": (
        "当前风险分数缺少已观察交易证据支持，应谨慎使用。"
    ),
    "Obtain additional transaction or channel evidence before case escalation.": (
        "升级为案件前，应补充交易或其他渠道证据。"
    ),
    "Human review only.": "仅供人工复核。",
    "Review.": "建议进行人工复核。",
}


def render_evidence_report_html(
    packet: EvidencePacket,
    *,
    markdown_url: str,
    case_id: str | None = None,
    packet_sha256: str | None = None,
) -> str:
    title_prefix = f"案例 {case_id}" if case_id is not None else "账户研判"
    review_state = "仅回溯研判" if not packet.score.production_eligible else "可进入审批流程"
    blockers = "".join(_blocker_item(item) for item in packet.score.eligibility_blockers)
    facts = "".join(_fact_row(fact) for fact in packet.facts)
    paths = "".join(
        _path_section(path, index) for index, path in enumerate(packet.paths, start=1)
    )
    patterns = "".join(_pattern_section(pattern) for pattern in packet.patterns)
    limitations = "".join(
        f"<li>{escape(_localize_limitation(item))}</li>" for item in packet.limitations
    )
    actions = "".join(
        f"<li>{escape(_localize_action(item))}</li>" for item in packet.recommended_actions
    )
    markdown_href = escape(markdown_url, quote=True)
    training_summary = (
        f"{escape(packet.score.model_training_mode)} · "
        f"{escape(packet.score.model_feature_set)}"
    )
    transaction_observed = "是" if packet.score.has_transactions else "否"
    production_eligible = "已具备" if packet.score.production_eligible else "未具备"
    path_content = paths or '<p class="empty-copy">未发现满足约束的资金链路。</p>'
    pattern_content = patterns or '<p class="empty-copy">未发现满足阈值的资金流向模式。</p>'
    case_metadata = (
        f'<span>证据哈希 <code>{escape(packet_sha256)}</code></span>'
        if packet_sha256 is not None
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="color-scheme" content="light" />
    <title>{escape(title_prefix)} · {escape(packet.account_id)}</title>
    <link rel="stylesheet" href="/assets/report.css?v=report-reader-1" />
  </head>
  <body>
    <header class="reader-bar">
      <a class="reader-brand" href="/" aria-label="返回风险工作台">
        <span aria-hidden="true">审</span>
        <strong>反诈资金图谱</strong>
      </a>
      <div class="reader-actions">
        <span class="reader-state">{review_state}</span>
        <a class="download-link" href="{markdown_href}" download>下载 Markdown</a>
      </div>
    </header>
    <main class="report-page">
      <section class="report-heading">
        <div>
          <p class="eyebrow">{escape(title_prefix)}</p>
          <h1>账户 {escape(packet.account_id)} 研判报告</h1>
          <div class="report-meta">
            <span>生成于 {_format_time(packet.generated_at)}</span>
            <span>模型 <code>{escape(packet.score.model_version)}</code></span>
            <span>数据 <code>{escape(packet.score.data_version)}</code></span>
            {case_metadata}
          </div>
        </div>
        <div class="grade-block grade-{escape(packet.grade.value.lower())}">
          <span>证据等级</span>
          <strong>{escape(packet.grade.value)}</strong>
        </div>
      </section>

      <section class="metric-strip" aria-label="风险摘要">
        {_metric("风险分数", _format_score(packet.score.risk_score), prominent=True)}
        {_metric("证据分数", _format_optional_score(packet.score.evidence_score))}
        {_metric("异常分数", _format_optional_score(packet.score.anomaly_score))}
        {_metric("风险排序", f"#{packet.score.risk_rank:,}")}
        {_metric("资金路径", f"{len(packet.paths)} 条")}
        {_metric("结构模式", f"{len(packet.patterns)} 个")}
      </section>

      <section class="report-section model-section">
        <div class="section-heading">
          <div><span>01</span><h2>模型与使用边界</h2></div>
          <p>{training_summary}</p>
        </div>
        <div class="model-grid">
          <dl>
            <div><dt>特征截止时间</dt><dd>{_format_time(packet.score.feature_as_of)}</dd></div>
            <div><dt>观察到交易</dt><dd>{transaction_observed}</dd></div>
            <div><dt>生产决策资格</dt><dd>{production_eligible}</dd></div>
          </dl>
          <div class="blocker-panel">
            <h3>上线阻断项</h3>
            <div class="blocker-grid">{blockers or '<span class="empty-copy">无</span>'}</div>
          </div>
        </div>
      </section>

      <section class="report-section">
        <div class="section-heading">
          <div><span>02</span><h2>结构化证据</h2></div>
          <p>{len(packet.facts)} 项可核验证据</p>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>证据</th><th>取值</th><th>源交易</th></tr></thead>
            <tbody>{facts}</tbody>
          </table>
        </div>
      </section>

      <section class="report-section">
        <div class="section-heading">
          <div><span>03</span><h2>可疑资金链路</h2></div>
          <p>{sum(len(path.steps) >= 2 for path in packet.paths)} 条多跳路径</p>
        </div>
        <div class="path-list">{path_content}</div>
      </section>

      <section class="report-section">
        <div class="section-heading">
          <div><span>04</span><h2>资金流向模式</h2></div>
          <p>汇聚、分散、中转与互转</p>
        </div>
        <div class="pattern-grid">{pattern_content}</div>
      </section>

      <section class="report-section report-conclusion">
        <div class="section-heading">
          <div><span>05</span><h2>研判边界与建议</h2></div>
        </div>
        <div class="conclusion-grid">
          <section><h3>局限性</h3><ul>{limitations}</ul></section>
          <section><h3>辅助处置建议</h3><ol>{actions}</ol></section>
        </div>
      </section>
    </main>
  </body>
</html>
"""


def _metric(label: str, value: str, *, prominent: bool = False) -> str:
    class_name = "metric-item metric-primary" if prominent else "metric-item"
    return (
        f'<div class="{class_name}"><span>{escape(label)}</span>'
        f"<strong>{escape(value)}</strong></div>"
    )


def _blocker_item(code: str) -> str:
    label = BLOCKER_LABELS.get(code, code)
    return f'<span class="blocker-chip"><b>{escape(label)}</b><code>{escape(code)}</code></span>'


def _fact_row(fact: EvidenceFact) -> str:
    transaction_cell = "-"
    if fact.transaction_ids:
        ids = "".join(f"<code>{escape(item)}</code>" for item in fact.transaction_ids)
        transaction_cell = (
            f"<details><summary>{len(fact.transaction_ids)} 笔</summary>"
            f'<div class="transaction-ids">{ids}</div></details>'
        )
    label = FACT_LABELS.get(fact.code, fact.description)
    value = "-" if fact.value is None else str(fact.value)
    return (
        "<tr>"
        f'<td><strong>{escape(label)}</strong><code>{escape(fact.code)}</code></td>'
        f'<td class="fact-value">{escape(value)}</td>'
        f"<td>{transaction_cell}</td>"
        "</tr>"
    )


def _path_section(path: SuspiciousPath, index: int) -> str:
    direction = "资金流入" if path.direction == "incoming" else "资金流出"
    evidence_codes = "".join(
        f'<code class="evidence-code">{escape(code)}</code>' for code in path.evidence_codes
    )
    steps = "".join(_path_step(path, step_index) for step_index in range(len(path.steps)))
    expanded = " open" if index <= 2 else ""
    return f"""
<details class="path-record"{expanded}>
  <summary>
    <div><span>路径 {index:02d}</span><strong>{direction} · {len(path.steps)} 跳</strong></div>
    <b>{path.score:.3f}</b>
  </summary>
  <div class="path-code-row"><code>{escape(path.path_id)}</code>{evidence_codes}</div>
  <ol class="path-steps">{steps}</ol>
</details>"""


def _path_step(path: SuspiciousPath, index: int) -> str:
    step = path.steps[index]
    gap = "首笔交易" if step.gap_seconds is None else f"间隔 {step.gap_seconds:,} 秒"
    ratio = (
        "无前序金额比"
        if step.amount_ratio_to_previous is None
        else f"金额延续比 {step.amount_ratio_to_previous:.3f}"
    )
    return f"""
<li>
  <div class="step-index">{index + 1}</div>
  <div class="step-body">
    <div class="account-flow">
      <strong>{escape(step.payer_account_id)}</strong><span>→</span>
      <strong>{escape(step.payee_account_id)}</strong>
    </div>
    <div class="step-meta">
      <span>¥{step.amount:,.2f}</span><span>{_format_time(step.event_time_utc)}</span>
      <span>{gap}</span><span>{ratio}</span>
    </div>
    <code>{escape(step.transaction_id)}</code>
  </div>
</li>"""


def _pattern_section(pattern: FlowPattern) -> str:
    related = "、".join(escape(item) for item in pattern.related_account_ids)
    transactions = "".join(
        f"<code>{escape(item)}</code>" for item in pattern.transaction_ids
    )
    return f"""
<article class="pattern-record">
  <header><strong>{escape(PATTERN_LABELS[pattern.pattern_type])}</strong><b>{pattern.score:.3f}</b></header>
  <p>{escape(pattern.description)}</p>
  <dl>
    <div><dt>观察区间</dt><dd>
      {_format_time(pattern.event_time_start)} 至 {_format_time(pattern.event_time_end)}
    </dd></div>
    <div><dt>关联账户</dt><dd>{related or "-"}</dd></div>
  </dl>
  <details>
    <summary>{len(pattern.transaction_ids)} 笔源交易</summary>
    <div class="transaction-ids">{transactions}</div>
  </details>
</article>"""


def _format_score(value: float) -> str:
    return f"{value:.3f}"


def _format_optional_score(value: float | None) -> str:
    return "-" if value is None else _format_score(value)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _localize_limitation(value: str) -> str:
    if value.startswith("Model eligibility blockers:"):
        return "模型仍存在上述上线阻断项，当前不得用于生产决策。"
    return LIMITATION_TRANSLATIONS.get(value, value)


def _localize_action(value: str) -> str:
    return ACTION_TRANSLATIONS.get(value, value)
