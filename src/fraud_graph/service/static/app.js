"use strict";

const state = {
  queue: [],
  total: 0,
  offset: 0,
  limit: 30,
  query: "",
  activeOnly: true,
  selectedAccount: null,
  workspace: null,
  direction: "all",
  graphScale: 1,
  graphPan: { x: 0, y: 0 },
  agentDomain: "account_investigation",
};

const elements = Object.fromEntries(
  [
    "scored-count",
    "active-count",
    "capacity-count",
    "version-label",
    "feature-time",
    "queue-total",
    "queue-range",
    "account-list",
    "account-search",
    "active-only",
    "refresh-button",
    "previous-page",
    "next-page",
    "selected-account",
    "risk-badge",
    "fund-graph",
    "graph-viewport",
    "graph-empty",
    "transaction-inspector",
    "evidence-panel",
    "associations-panel",
    "paths-panel",
    "agent-panel",
    "report-link",
    "zoom-in",
    "zoom-out",
    "fit-graph",
    "toast",
  ].map((id) => [id, document.getElementById(id)])
);

const formatNumber = new Intl.NumberFormat("zh-CN");
const formatScore = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 3,
  maximumFractionDigits: 3,
});
const formatAmount = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

const agentQuestionPresets = {
  account_investigation: [
    "为什么该账户值得优先复核？",
    "当前最关键的资金链路是什么？",
    "资金流向呈现什么模式？",
    "当前证据有哪些局限？",
  ],
  system_explanation: [
    "我们判断风险账户的标准是什么？",
    "风险分数、证据分数和异常分数有什么区别？",
    "模型使用了哪些特征？",
    "当前模型达到了哪些比赛指标？",
    "任务四报告如何保证可信？",
  ],
};

const workbenchFactLabels = {
  MODEL_RISK_SCORE: ["主模型风险分数", "任务二最终模型输出的账户风险概率"],
  TXN_COUNT_TOTAL: ["观察窗口交易笔数", "观察窗口内发生的交易总笔数"],
  COUNTERPARTY_COUNT_TOTAL: ["交易对手数量", "观察窗口内有资金往来的不同账户数"],
  SAME_DAY_IN_OUT_DAYS: ["同日收付活跃天数", "同一天内同时发生资金流入和流出的天数"],
  SAME_DAY_TURNOVER_RATIO_AVG: ["同日资金周转比例", "同日流入资金被流出资金覆盖的平均比例"],
  CITED_DIRECT_TRANSACTIONS: ["直接交易关系", "由源交易记录支持的一跳账户关系"],
  SUPPORTED_MULTI_HOP_PATHS: ["多跳时序资金链", "由源交易支持且时间顺序连续的多跳路径"],
  FLOW_PATTERNS: ["异常资金流向模式", "识别出的资金汇聚、分散、快速中转或双向互转模式"],
};

const workbenchNarrativeTranslations = {
  "Only direct transaction relationships were found; no qualifying multi-hop temporal path was established.": "仅发现直接交易关系，尚未形成满足约束的多跳时序资金链。",
  "No qualifying multi-hop temporal path was found in the configured window.": "当前观察窗口内未发现满足约束的多跳时序资金链。",
  "No transaction was observed in the configured window; no funding path can be asserted.": "当前观察窗口内未发现交易，因此不能认定存在资金链路。",
  "Model output is decision support and does not authorize automatic freezing.": "模型结果仅用于辅助研判，不构成自动止付或冻结的依据。",
  "The referenced model is not production-eligible and must not drive an operational decision.": "当前模型尚不具备生产决策资格，不得直接驱动业务处置。",
  "No transaction evidence.": "当前没有可核验的交易证据。",
  "No transactions.": "当前观察窗口内未发现交易。",
};

function formatOptionalScore(value) {
  return value === null || value === undefined ? "--" : formatScore.format(Number(value));
}

function trainingModeLabel(mode) {
  return {
    temporal: "时序建模",
    retrospective_static: "静态回溯建模",
    unknown: "未知",
  }[mode] || mode;
}

function workbenchFactLabel(code) {
  return workbenchFactLabels[code]?.[0] || code;
}

function workbenchFactDescription(code, fallback) {
  return workbenchFactLabels[code]?.[1] || fallback;
}

function workbenchFactValue(fact) {
  if (fact.value === null || fact.value === undefined) return "--";
  const numeric = Number(fact.value);
  if (!Number.isFinite(numeric)) return String(fact.value);
  if (Number.isInteger(numeric)) return String(numeric);
  if (fact.code === "MODEL_RISK_SCORE") return numeric.toFixed(6);
  return numeric.toFixed(3);
}

function localizeWorkbenchNarrative(value) {
  if (String(value).startsWith("Model eligibility blockers:")) {
    return "当前模型仅用于比赛实验与人工辅助，不得直接用于生产处置。";
  }
  return workbenchNarrativeTranslations[value] || value;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `请求失败 (${response.status})`);
  }
  return response.json();
}

async function postApi(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const errorPayload = await response.json().catch(() => ({}));
    throw new Error(errorPayload.detail || `请求失败 (${response.status})`);
  }
  return response.json();
}

async function deleteApi(path) {
  const response = await fetch(path, {
    method: "DELETE",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const errorPayload = await response.json().catch(() => ({}));
    throw new Error(errorPayload.detail || `请求失败 (${response.status})`);
  }
  return response.json();
}

function showToast(message) {
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => elements.toast.classList.remove("visible"), 3500);
}

async function loadSummary() {
  const summary = await api("/v1/operations/summary");
  elements["scored-count"].textContent = formatNumber.format(summary.scored_accounts);
  elements["active-count"].textContent = formatNumber.format(summary.active_accounts);
  elements["capacity-count"].textContent = `${formatNumber.format(summary.review_capacity_count)} / ${(summary.review_capacity_fraction * 100).toFixed(0)}%`;
  elements["version-label"].textContent = `${summary.data_version} / ${summary.model_version}`;
  elements["feature-time"].textContent = new Date(summary.feature_as_of).toLocaleString("zh-CN", { hour12: false });
}

function renderQueueLoading() {
  elements["account-list"].innerHTML = Array.from({ length: 8 }, () => '<div class="skeleton"></div>').join("");
}

async function loadQueue({ preserveSelection = true } = {}) {
  renderQueueLoading();
  const params = new URLSearchParams({
    query: state.query,
    active_only: String(state.activeOnly),
    offset: String(state.offset),
    limit: String(state.limit),
  });
  const page = await api(`/v1/accounts?${params}`);
  state.queue = page.items;
  state.total = page.total;
  renderQueue();

  const selectedStillVisible = state.queue.some((item) => item.account_id === state.selectedAccount);
  if ((!preserveSelection || !state.selectedAccount) && state.queue[0]) {
    await selectAccount(state.queue[0].account_id);
  } else if (!selectedStillVisible && state.queue[0] && state.query) {
    await selectAccount(state.queue[0].account_id);
  }
}

function renderQueue() {
  elements["queue-total"].textContent = `${formatNumber.format(state.total)} 个账户`;
  if (!state.queue.length) {
    elements["account-list"].innerHTML = '<div class="graph-empty">未找到匹配账户</div>';
  } else {
    elements["account-list"].innerHTML = state.queue
      .map(
        (item) => `
          <button class="account-row ${item.account_id === state.selectedAccount ? "active" : ""}" type="button" data-account="${escapeHtml(item.account_id)}">
            <span class="account-id">${escapeHtml(item.account_id)}</span>
            <span class="score">${formatScore.format(item.risk_score)}</span>
            <span class="rank">#${formatNumber.format(item.risk_rank)}</span>
          </button>`
      )
      .join("");
  }
  const from = state.total ? state.offset + 1 : 0;
  const to = Math.min(state.offset + state.queue.length, state.total);
  elements["queue-range"].textContent = `${formatNumber.format(from)}–${formatNumber.format(to)} / ${formatNumber.format(state.total)}`;
  elements["previous-page"].disabled = state.offset === 0;
  elements["next-page"].disabled = state.offset + state.limit >= state.total;
}

async function selectAccount(accountId) {
  if (!accountId) return;
  state.selectedAccount = accountId;
  renderQueue();
  elements["selected-account"].textContent = accountId;
  elements["risk-badge"].textContent = "加载中";
  elements["graph-viewport"].innerHTML = "";
  elements["graph-empty"].hidden = true;
  elements["evidence-panel"].innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  elements["associations-panel"].innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  elements["paths-panel"].innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  elements["agent-panel"].innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  try {
    const workspace = await api(`/v1/accounts/${encodeURIComponent(accountId)}/workspace?association_limit=20`);
    if (state.selectedAccount !== accountId) return;
    state.workspace = workspace;
    state.graphScale = 1;
    state.graphPan = { x: 0, y: 0 };
    elements["risk-badge"].textContent = `风险 ${formatScore.format(workspace.evidence.score.risk_score)}`;
    elements["report-link"].href = `/v1/accounts/${encodeURIComponent(accountId)}/report?view=html`;
    renderDetails();
    renderGraph();
  } catch (error) {
    showToast(error.message);
  }
}

function renderDetails() {
  const { evidence, associations } = state.workspace;
  const score = evidence.score;
  elements["evidence-panel"].innerHTML = `
    <p class="section-label">风险摘要</p>
    <div class="summary-grid">
      <div class="summary-cell"><span>风险分数</span><strong>${formatScore.format(score.risk_score)}</strong></div>
      <div class="summary-cell"><span>证据分数</span><strong>${formatOptionalScore(score.evidence_score)}</strong></div>
      <div class="summary-cell"><span>异常分数</span><strong>${formatOptionalScore(score.anomaly_score)}</strong></div>
      <div class="summary-cell"><span>风险排序</span><strong>#${formatNumber.format(score.risk_rank)}</strong></div>
      <div class="summary-cell"><span>证据等级</span><strong>${escapeHtml(evidence.grade)}</strong></div>
      <div class="summary-cell"><span>训练模式</span><strong>${escapeHtml(trainingModeLabel(score.model_training_mode))}</strong></div>
    </div>
    <p class="section-label">结构化证据</p>
    <div class="evidence-list">${evidence.facts.length ? evidence.facts.map((fact) => `
      <div class="evidence-item" data-fact="${escapeHtml(fact.code)}">
        <div class="evidence-head">
          <strong>${escapeHtml(workbenchFactLabel(fact.code))}</strong>
          <span class="evidence-value">${escapeHtml(workbenchFactValue(fact))}</span>
        </div>
        <p>${escapeHtml(workbenchFactDescription(fact.code, fact.description))}</p>
        <small>字段代码 ${escapeHtml(fact.code)}</small>
      </div>`).join("") : '<p class="limit-item">当前没有可展示的结构化证据。</p>'}</div>
    <p class="section-label">资金流向模式</p>
    <div class="pattern-list">${evidence.patterns.length ? evidence.patterns.map((pattern) => `
      <div class="pattern-item" data-pattern="${escapeHtml(pattern.pattern_id)}">
        <div class="pattern-head"><strong>${patternLabel(pattern.pattern_type)}</strong><b>${formatScore.format(pattern.score)}</b></div>
        <span>${escapeHtml(pattern.description)} · ${pattern.transaction_ids.length} 笔交易 · ${pattern.related_account_ids.length} 个关联账户</span>
      </div>`).join("") : '<p class="limit-item">未发现满足阈值的汇聚、分散、中转或互转模式。</p>'}</div>
    <p class="section-label">局限性</p>
    ${evidence.limitations.map((item) => `<p class="limit-item">${escapeHtml(localizeWorkbenchNarrative(item))}</p>`).join("")}
  `;

  elements["associations-panel"].innerHTML = `
    <p class="section-label">Top ${associations.length} 关联账户</p>
    <div class="association-list">${associations.length ? associations.map((item) => `
      <button type="button" class="association-item" data-node="${escapeHtml(item.account_id)}">
        <div class="association-head"><strong>${escapeHtml(item.account_id)}</strong><b class="association-score">${formatScore.format(item.association_score)}</b></div>
        <span>${directionLabel(item.direction)} · ${item.min_hops} 跳 · ${item.transaction_ids.length} 笔证据 · 支持 ${item.supporting_path_count} 条路径</span>
        <small>风险 ${formatOptionalScore(item.associated_risk_score)} · 证据 ${formatOptionalScore(item.associated_evidence_score)}${item.pattern_types.length ? ` · ${item.pattern_types.map(patternLabel).join(" / ")}` : ""}</small>
      </button>`).join("") : '<p class="limit-item">未发现满足约束的关联账户。</p>'}</div>
  `;

  elements["paths-panel"].innerHTML = `
    <p class="section-label">可疑资金链路</p>
    <div class="path-list">${evidence.paths.length ? evidence.paths.map((path, index) => `
      <button type="button" class="path-item" data-path="${escapeHtml(path.path_id)}">
        <div class="path-head"><strong>路径 ${index + 1}</strong><b class="path-score">${formatScore.format(path.score)}</b></div>
        <span>${directionLabel(path.direction)} · ${path.steps.length} 跳 · ${escapeHtml(path.evidence_codes.join(" / "))}</span>
      </button>`).join("") : '<p class="limit-item">未发现满足约束的资金链路。</p>'}</div>
  `;

  renderAgentPanel();
}

function renderAgentPanel() {
  elements["agent-panel"].innerHTML = `
    <div class="agent-intro">
      <div>
        <p class="section-label">证据研判助手</p>
        <h3>围绕当前账户提问</h3>
      </div>
      <span class="agent-guardrail">仅辅助人工研判</span>
    </div>
    <section id="agent-provider-card" class="agent-provider-card" aria-label="DeepSeek 连接">
      <div class="agent-provider-head">
        <div>
          <span id="agent-provider-dot" class="agent-provider-dot" aria-hidden="true"></span>
          <strong id="agent-provider-state">DeepSeek 未连接</strong>
        </div>
        <button id="agent-disable-provider" class="agent-text-button" type="button" hidden>停用</button>
      </div>
      <div class="agent-key-row">
        <input id="agent-api-key" class="agent-api-key" type="password" maxlength="512" autocomplete="new-password" spellcheck="false" placeholder="DeepSeek API Key" aria-label="DeepSeek API Key" />
        <select id="agent-model" class="agent-model" aria-label="DeepSeek 模型">
          <option value="deepseek-v4-flash">V4 Flash</option>
          <option value="deepseek-v4-pro">V4 Pro</option>
        </select>
      </div>
      <label class="agent-export-consent">
        <input id="agent-export-authorized" type="checkbox" />
        <span>我确认已获授权向 DeepSeek 发送当前脱敏证据</span>
      </label>
      <div class="agent-provider-actions">
        <button id="agent-validate-key" class="command-button" type="button">验证并启用</button>
        <small id="agent-session-detail">Key 仅在服务端内存保留 30 分钟</small>
      </div>
    </section>
    <div class="agent-domain-field">
      <span class="agent-field-label">知识域</span>
      <div class="agent-domain-switch" role="group" aria-label="研判助手知识域">
        <button type="button" data-agent-domain="account_investigation" aria-pressed="${state.agentDomain === "account_investigation"}">账户研判</button>
        <button type="button" data-agent-domain="system_explanation" aria-pressed="${state.agentDomain === "system_explanation"}">系统解释</button>
      </div>
    </div>
    <label class="agent-field-label" for="agent-question">问题</label>
    <textarea id="agent-question" class="agent-question" rows="4" maxlength="2000" placeholder="${escapeHtml(agentQuestionPresets[state.agentDomain][0])}"></textarea>
    <section class="agent-suggestions" aria-labelledby="agent-suggestions-title">
      <div class="agent-suggestions-head">
        <strong id="agent-suggestions-title">你可以这样问</strong>
        <span id="agent-domain-label">${agentDomainLabel(state.agentDomain)}</span>
      </div>
      <div id="agent-suggestion-list" class="agent-suggestion-list">
        ${renderAgentSuggestionButtons()}
      </div>
    </section>
    <div class="agent-question-row">
      <button id="agent-submit" class="command-button agent-submit" type="button">发送研判</button>
    </div>
    <div id="agent-status" class="agent-status" aria-live="polite">回答将绑定当前账户证据包。</div>
    <div id="agent-result" class="agent-result" aria-live="polite">
      <p class="limit-item">输入问题后，助手会返回带证据引用的中文摘要。</p>
    </div>
  `;
  const question = document.getElementById("agent-question");
  const submit = document.getElementById("agent-submit");
  const validateKey = document.getElementById("agent-validate-key");
  const disableProvider = document.getElementById("agent-disable-provider");
  document.querySelectorAll("[data-agent-domain]").forEach((button) => {
    button.addEventListener("click", () => setAgentDomain(button.dataset.agentDomain));
  });
  bindAgentSuggestionButtons(question);
  submit.addEventListener("click", () => queryAgent(question, submit));
  validateKey.addEventListener("click", () => validateAgentKey(validateKey));
  disableProvider.addEventListener("click", () => disableAgentProvider(disableProvider));
  loadAgentSession();
}

function setAgentDomain(domain) {
  if (!agentQuestionPresets[domain]) return;
  state.agentDomain = domain;
  document.querySelectorAll("[data-agent-domain]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.agentDomain === domain));
  });
  const question = document.getElementById("agent-question");
  const label = document.getElementById("agent-domain-label");
  const list = document.getElementById("agent-suggestion-list");
  if (question) {
    question.value = "";
    question.placeholder = agentQuestionPresets[domain][0];
  }
  if (label) label.textContent = agentDomainLabel(domain);
  if (list) {
    list.innerHTML = renderAgentSuggestionButtons();
    bindAgentSuggestionButtons(question);
  }
}

function renderAgentSuggestionButtons() {
  return agentQuestionPresets[state.agentDomain]
    .map((question) => `<button type="button" class="agent-suggestion" data-agent-question="${escapeHtml(question)}">${escapeHtml(question)}</button>`)
    .join("");
}

function bindAgentSuggestionButtons(questionElement) {
  document.querySelectorAll("[data-agent-question]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!questionElement) return;
      questionElement.value = button.dataset.agentQuestion;
      questionElement.focus();
    });
  });
}

function agentDomainLabel(domain) {
  return {
    account_investigation: "账户研判",
    system_explanation: "系统解释",
  }[domain] || domain;
}

async function loadAgentSession() {
  try {
    updateAgentSession(await api("/v1/agent/session"));
  } catch (error) {
    updateAgentSession({ active: false });
  }
}

async function validateAgentKey(button) {
  const keyInput = document.getElementById("agent-api-key");
  const modelInput = document.getElementById("agent-model");
  const authorizedInput = document.getElementById("agent-export-authorized");
  const key = keyInput.value.trim();
  if (key.length < 8) {
    showToast("请输入有效的 DeepSeek API Key");
    keyInput.focus();
    return;
  }
  if (!authorizedInput.checked) {
    showToast("请先确认已取得脱敏证据外发授权");
    authorizedInput.focus();
    return;
  }
  button.disabled = true;
  button.textContent = "正在验证";
  setAgentProviderStatus("validating", "正在验证 DeepSeek 连接…");
  try {
    const session = await postApi("/v1/agent/session", {
      api_key: key,
      model: modelInput.value,
      data_export_authorized: true,
    });
    keyInput.value = "";
    authorizedInput.checked = false;
    updateAgentSession(session);
    showToast("DeepSeek 已连接");
  } catch (error) {
    updateAgentSession({ active: false });
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "验证并启用";
  }
}

async function disableAgentProvider(button) {
  button.disabled = true;
  try {
    updateAgentSession(await deleteApi("/v1/agent/session"));
    showToast("DeepSeek 临时会话已停用");
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
}

function updateAgentSession(session) {
  const active = Boolean(session?.active);
  const card = document.getElementById("agent-provider-card");
  const disable = document.getElementById("agent-disable-provider");
  const detail = document.getElementById("agent-session-detail");
  if (!card || !disable || !detail) return;
  card.classList.toggle("connected", active);
  disable.hidden = !active;
  if (active) {
    setAgentProviderStatus("connected", `DeepSeek ${session.model || ""} 已连接`);
    const expiresAt = session.expires_at
      ? new Date(session.expires_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
      : "稍后";
    detail.textContent = `临时凭据将在 ${expiresAt} 失效`;
  } else {
    setAgentProviderStatus("disconnected", "DeepSeek 未连接");
    detail.textContent = "Key 仅在服务端内存保留 30 分钟";
  }
}

function setAgentProviderStatus(tone, message) {
  const card = document.getElementById("agent-provider-card");
  const stateLabel = document.getElementById("agent-provider-state");
  if (!card || !stateLabel) return;
  card.dataset.status = tone;
  stateLabel.textContent = message;
}

async function queryAgent(questionElement, submitButton) {
  const question = questionElement.value.trim();
  if (question.length < 2) {
    showToast("请先输入研判问题");
    questionElement.focus();
    return;
  }
  const status = document.getElementById("agent-status");
  const result = document.getElementById("agent-result");
  submitButton.disabled = true;
  status.textContent = "正在读取证据并生成研判摘要…";
  result.innerHTML = '<div class="agent-loading"><span></span><span></span><span></span></div>';
  try {
    const response = await postApi("/v1/agent/query", {
      account_id: state.selectedAccount,
      question,
      knowledge_domain: state.agentDomain,
    });
    status.textContent = response.generation_mode === "deepseek"
      ? `DeepSeek ${response.provider_model || ""} · ${response.citation_repair_applied ? "引用已自动校正" : "已通过证据校验"}`
      : `安全证据摘要 · ${agentFallbackLabel(response.fallback_reason)}`;
    status.title = response.fallback_reason || "";
    result.innerHTML = renderAgentResult(response);
    bindAgentCitations(result);
  } catch (error) {
    status.textContent = "研判请求失败";
    result.innerHTML = `<p class="agent-error">${escapeHtml(error.message)}</p>`;
  } finally {
    submitButton.disabled = false;
  }
}

function renderAgentResult(response) {
  const citations = response.citations || [];
  const citationMarkup = citations.length
    ? citations.map((citation, index) => `
      <button type="button" class="agent-citation" data-citation-kind="${escapeHtml(citation.kind)}" data-citation-id="${escapeHtml(citation.id)}">
        <span class="agent-citation-index">${String(index + 1).padStart(2, "0")}</span>
        <span class="agent-citation-copy">
          <strong>${escapeHtml(citation.label)}</strong>
          <small>${agentCitationKindLabel(citation.kind)} · ${escapeHtml(citation.id)}</small>
        </span>
      </button>`).join("")
    : '<span class="limit-item">无可用引用</span>';
  const traceMarkup = (response.tool_trace || []).map((trace) => `<span class="agent-trace ${escapeHtml(trace.status)}">${escapeHtml(trace.tool)}</span>`).join("");
  const findings = response.key_findings || [];
  const findingMarkup = findings.length
    ? `<ol class="agent-findings">${findings.map((item, index) => `
        <li>
          <span class="agent-finding-index">${index + 1}</span>
          <p>${renderAgentNarrative(item, citations)}</p>
        </li>`).join("")}</ol>`
    : '<p class="limit-item">当前没有可单列的关键依据。</p>';
  const isDeepSeek = response.generation_mode === "deepseek";
  const isSystemExplanation = response.knowledge_domain === "system_explanation";
  const notice = isDeepSeek
    ? `${response.citation_repair_applied ? "遗漏的有效引用已由系统补齐。" : ""}AI 辅助研判结果仅供人工审核参考，不构成涉诈认定或处置依据。`
    : "DeepSeek 回答未通过校验或当前未启用，以下为系统依据已验证证据生成的安全摘要。";
  return `
    <div class="agent-notice ${isDeepSeek ? "ai" : "fallback"}">${notice}</div>
    <div class="agent-assessment"><span>${isSystemExplanation ? "解释状态" : "研判状态"}</span><strong>${escapeHtml(agentAssessmentLabel(response.assessment))}</strong></div>
    <section class="agent-section">
      <h4>${isSystemExplanation ? "系统说明" : "研判结论"}</h4>
      <div class="agent-conclusion">${renderAgentNarrative(response.answer, citations)}</div>
    </section>
    <section class="agent-section">
      <h4>关键依据</h4>
      ${findingMarkup}
    </section>
    <section class="agent-section">
      <h4>${isSystemExplanation ? "版本依据" : "证据引用"}</h4>
      <div class="agent-citations">${citationMarkup}</div>
    </section>
    <section class="agent-section agent-section-split">
      <div>
        <h4>局限性</h4>
        ${renderAgentList(response.limitations, "当前未补充其他局限性。")}
      </div>
      <div>
        <h4>${isSystemExplanation ? "参考建议" : "辅助处置建议"}</h4>
        ${renderAgentList(response.recommended_actions, isSystemExplanation ? "当前没有其他参考建议。" : "当前没有其他辅助核查建议。")}
      </div>
    </section>
    <div class="agent-meta"><span>模型 ${escapeHtml(response.model_version)}</span><span>数据 ${escapeHtml(response.data_version)}</span><span>哈希 ${escapeHtml(response.evidence_packet_sha256.slice(0, 12))}…</span></div>
    <div class="agent-traces" aria-label="工具调用轨迹">${traceMarkup}</div>
  `;
}

function renderAgentNarrative(value, citations) {
  let markup = escapeHtml(value || "");
  citations.forEach((citation, index) => {
    const marker = escapeHtml(agentCitationMarker(citation));
    const reference = `<button type="button" class="agent-inline-citation" data-citation-kind="${escapeHtml(citation.kind)}" data-citation-id="${escapeHtml(citation.id)}" aria-label="查看证据 ${index + 1}">${index + 1}</button>`;
    markup = markup.replaceAll(marker, reference);
  });
  return markup.replaceAll("\n", "<br />");
}

function renderAgentList(items, emptyText) {
  return items?.length
    ? `<ul class="agent-list">${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`
    : `<p class="limit-item">${emptyText}</p>`;
}

function agentCitationMarker(citation) {
  const prefix = { fact: "事实", path: "路径", transaction: "交易", pattern: "模式", system: "系统" }[citation.kind] || citation.kind;
  return `[${prefix}:${citation.id}]`;
}

function agentCitationKindLabel(kind) {
  return { fact: "事实", path: "路径", transaction: "交易", pattern: "模式", system: "系统依据" }[kind] || kind;
}

function agentFallbackLabel(reason) {
  if (!reason || reason.includes("未启用") || reason.includes("未配置")) return "DeepSeek 当前未启用";
  if (reason.includes("引用") || reason.includes("证据")) return "AI 回答未通过证据校验";
  if (reason.includes("JSON") || reason.includes("协议") || reason.includes("格式")) return "AI 回答格式未通过校验";
  if (reason.includes("工具")) return "AI 工具调用未通过校验";
  return "AI 回答未通过安全校验";
}

function agentAssessmentLabel(value) {
  return {
    manual_review_recommended: "建议人工复核",
    insufficient_transaction_evidence: "交易证据不足",
    information_only: "仅供信息参考",
  }[value] || value;
}

function bindAgentCitations(container) {
  container.querySelectorAll("[data-citation-kind]").forEach((button) => {
    button.addEventListener("click", () => {
      const kind = button.dataset.citationKind;
      if (kind === "path") {
        document.querySelector('[data-tab="paths"]')?.click();
        highlightPath(button.dataset.citationId);
      } else if (kind === "transaction") {
        showTransaction(button.dataset.citationId);
      } else if (kind === "fact") {
        focusEvidenceCitation("fact", button.dataset.citationId);
      } else if (kind === "pattern") {
        focusEvidenceCitation("pattern", button.dataset.citationId);
      } else if (kind === "system") {
        showToast("该说明已绑定当前版本的系统资料");
      }
    });
  });
}

function focusEvidenceCitation(kind, id) {
  document.querySelector('[data-tab="evidence"]')?.click();
  const selector = kind === "fact" ? `[data-fact="${CSS.escape(id)}"]` : `[data-pattern="${CSS.escape(id)}"]`;
  const target = document.querySelector(selector);
  if (!target) return;
  target.classList.add("citation-focus");
  target.scrollIntoView({ behavior: "smooth", block: "nearest" });
  window.setTimeout(() => target.classList.remove("citation-focus"), 1800);
}

function directionLabel(direction) {
  return { incoming: "流入", outgoing: "流出", both: "双向" }[direction] || direction;
}

function patternLabel(patternType) {
  return {
    FAN_IN: "资金汇聚",
    FAN_OUT: "资金分散",
    PASS_THROUGH: "快速中转",
    RECIPROCAL: "双向互转",
  }[patternType] || patternType;
}

function buildGraphData(paths) {
  const nodes = new Map();
  const edges = new Map();
  const root = state.selectedAccount;
  nodes.set(root, { id: root, root: true, directions: new Set(), minHop: 0 });

  paths.forEach((path) => {
    path.steps.forEach((step, index) => {
      const payer = nodes.get(step.payer_account_id) || { id: step.payer_account_id, root: false, directions: new Set(), minHop: 99 };
      const payee = nodes.get(step.payee_account_id) || { id: step.payee_account_id, root: false, directions: new Set(), minHop: 99 };
      payer.directions.add(path.direction);
      payee.directions.add(path.direction);
      payer.minHop = Math.min(payer.minHop, path.direction === "outgoing" ? index : path.steps.length - index);
      payee.minHop = Math.min(payee.minHop, path.direction === "outgoing" ? index + 1 : path.steps.length - index - 1);
      nodes.set(payer.id, payer);
      nodes.set(payee.id, payee);
      edges.set(step.transaction_id, { ...step, direction: path.direction, pathId: path.path_id, pathScore: path.score });
    });
  });
  return { nodes: [...nodes.values()], edges: [...edges.values()] };
}

function layoutNodes(nodes) {
  const root = nodes.find((node) => node.root);
  if (!root) return new Map();
  const positions = new Map([[root.id, { x: 500, y: 310 }]]);
  // Keep each ring sparse enough for the account cards and their labels.
  // The graph is intentionally deterministic so the same case is easy to compare.
  const others = nodes.filter((node) => !node.root).sort((a, b) => a.minHop - b.minHop || a.id.localeCompare(b.id));
  const ringCapacity = 8;
  const rings = [];
  others.forEach((node, index) => {
    const ring = Math.floor(index / ringCapacity);
    if (!rings[ring]) rings[ring] = [];
    rings[ring].push(node);
  });
  rings.forEach((ringNodes, ringIndex) => {
    const ring = ringIndex + 1;
    const radiusX = 148 + ring * 88;
    const radiusY = 98 + ring * 54;
    ringNodes.forEach((node, index) => {
      const angle = -Math.PI / 2 + (Math.PI * 2 * index) / ringNodes.length + ring * 0.22;
      positions.set(node.id, { x: 500 + Math.cos(angle) * radiusX, y: 310 + Math.sin(angle) * radiusY });
    });
  });
  return positions;
}

function nodeGeometry(node) {
  return node.root
    ? { width: 188, height: 68 }
    : { width: 144, height: 56 };
}

function edgePaddingForNode(node, dx, dy) {
  const geometry = nodeGeometry(node);
  const length = Math.max(1, Math.hypot(dx, dy));
  const horizontal = Math.abs(dx) / length;
  const vertical = Math.abs(dy) / length;
  const horizontalHit = horizontal ? geometry.width / 2 / horizontal : Number.POSITIVE_INFINITY;
  const verticalHit = vertical ? geometry.height / 2 / vertical : Number.POSITIVE_INFINITY;
  return Math.min(horizontalHit, verticalHit) + 8;
}

function nodeTone(node) {
  if (node.root) return "root-node";
  if (node.directions.has("incoming") && node.directions.has("outgoing")) return "both-node";
  return node.directions.has("incoming") ? "incoming-node" : "outgoing-node";
}

function nodeFlowLabel(node) {
  if (node.root) return "根账户";
  if (node.directions.has("incoming") && node.directions.has("outgoing")) return `${node.minHop} 跳 · 双向关联`;
  return `${node.minHop} 跳 · ${directionLabel([...node.directions][0])}关联`;
}

function renderGraph() {
  if (!state.workspace) return;
  const filteredPaths = state.workspace.evidence.paths.filter((path) => state.direction === "all" || path.direction === state.direction);
  const graph = buildGraphData(filteredPaths);
  elements["graph-empty"].hidden = graph.edges.length > 0;
  const positions = layoutNodes(graph.nodes);
  const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
  const edgeMarkup = graph.edges.map((edge) => {
    const from = positions.get(edge.payer_account_id);
    const to = positions.get(edge.payee_account_id);
    const fromNode = nodesById.get(edge.payer_account_id);
    const toNode = nodesById.get(edge.payee_account_id);
    if (!from || !to || !fromNode || !toNode) return "";
    const dx = to.x - from.x;
    const dy = to.y - from.y;
    const length = Math.max(1, Math.hypot(dx, dy));
    const startPadding = edgePaddingForNode(fromNode, dx, dy);
    const endPadding = edgePaddingForNode(toNode, -dx, -dy);
    const startX = from.x + (dx / length) * startPadding;
    const startY = from.y + (dy / length) * startPadding;
    const endX = to.x - (dx / length) * endPadding;
    const endY = to.y - (dy / length) * endPadding;
    const color = edge.direction === "incoming" ? "#2563eb" : "#c24145";
    const marker = edge.direction === "incoming" ? "arrow-in" : "arrow-out";
    const midpointX = (startX + endX) / 2;
    const midpointY = (startY + endY) / 2;
    return `
      <g class="edge-group" data-transaction="${escapeHtml(edge.transaction_id)}">
        <line class="graph-edge-hit" x1="${startX}" y1="${startY}" x2="${endX}" y2="${endY}"></line>
        <line class="graph-edge" x1="${startX}" y1="${startY}" x2="${endX}" y2="${endY}" stroke="${color}" marker-end="url(#${marker})"></line>
        <text class="edge-label" x="${midpointX}" y="${midpointY - 8}">${formatAmount.format(Number(edge.amount))}</text>
      </g>`;
  }).join("");
  const nodeMarkup = graph.nodes.map((node) => {
    const position = positions.get(node.id);
    const geometry = nodeGeometry(node);
    const x = -geometry.width / 2;
    const y = -geometry.height / 2;
    const scoreText = node.root ? `风险 ${formatScore.format(state.workspace.evidence.score.risk_score)}` : nodeFlowLabel(node);
    return `
      <g class="graph-node ${nodeTone(node)}" data-node="${escapeHtml(node.id)}" transform="translate(${position.x} ${position.y})">
        <title>${escapeHtml(node.id)} · ${escapeHtml(scoreText)}</title>
        <rect class="node-halo" x="${x - 6}" y="${y - 6}" width="${geometry.width + 12}" height="${geometry.height + 12}" rx="${node.root ? 18 : 15}"></rect>
        <rect class="node-card" x="${x}" y="${y}" width="${geometry.width}" height="${geometry.height}" rx="${node.root ? 14 : 11}"></rect>
        <circle class="node-avatar" cx="${x + 22}" cy="0" r="${node.root ? 17 : 13}"></circle>
        <text class="node-avatar-label" x="${x + 22}" y="4">${node.root ? "根" : escapeHtml(shortId(node.id).slice(0, 2))}</text>
        <text class="node-id" x="${x + (node.root ? 49 : 43)}" y="-4">${escapeHtml(shortId(node.id))}</text>
        <text class="node-meta" x="${x + (node.root ? 49 : 43)}" y="15">${escapeHtml(scoreText)}</text>
      </g>`;
  }).join("");
  elements["graph-viewport"].innerHTML = edgeMarkup + nodeMarkup;
  updateGraphTransform();
}

function shortId(value) {
  const text = String(value);
  return text.length > 10 ? `${text.slice(0, 4)}…${text.slice(-4)}` : text;
}

function showTransaction(transactionId) {
  const steps = state.workspace.evidence.paths.flatMap((path) => path.steps.map((step) => ({ ...step, path })));
  const item = steps.find(({ transaction_id }) => transaction_id === transactionId);
  if (!item) return;
  elements["transaction-inspector"].innerHTML = `
    <strong>${escapeHtml(item.transaction_id)}</strong>
    <span>${escapeHtml(item.payer_account_id)} → ${escapeHtml(item.payee_account_id)}</span>
    <strong class="amount">${formatAmount.format(Number(item.amount))}</strong>
    <span>${new Date(item.event_time_utc).toLocaleString("zh-CN", { hour12: false })}</span>
    <span>${item.gap_seconds == null ? "首笔" : `间隔 ${formatNumber.format(item.gap_seconds)} 秒`}</span>
  `;
  document.querySelectorAll(".graph-edge").forEach((edge) => edge.classList.remove("selected"));
  document.querySelector(`[data-transaction="${CSS.escape(transactionId)}"] .graph-edge`)?.classList.add("selected");
}

function highlightNode(accountId) {
  document.querySelectorAll(".graph-node").forEach((node) => node.classList.toggle("selected", node.dataset.node === accountId));
}

function highlightPath(pathId) {
  const path = state.workspace.evidence.paths.find((item) => item.path_id === pathId);
  if (!path) return;
  const ids = new Set(path.steps.map((step) => step.transaction_id));
  document.querySelectorAll(".graph-edge").forEach((edge) => {
    const transactionId = edge.closest(".edge-group")?.dataset.transaction;
    edge.classList.toggle("selected", ids.has(transactionId));
  });
  if (path.steps[0]) showTransaction(path.steps[0].transaction_id);
}

function updateGraphTransform() {
  elements["graph-viewport"].setAttribute("transform", `translate(${state.graphPan.x} ${state.graphPan.y}) scale(${state.graphScale})`);
}

function setGraphScale(value) {
  state.graphScale = Math.min(2.2, Math.max(0.55, value));
  updateGraphTransform();
}

function bindEvents() {
  elements["account-list"].addEventListener("click", (event) => {
    const row = event.target.closest("[data-account]");
    if (row) selectAccount(row.dataset.account);
  });
  let searchTimer;
  elements["account-search"].addEventListener("input", (event) => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.query = event.target.value;
      state.offset = 0;
      loadQueue({ preserveSelection: false }).catch((error) => showToast(error.message));
    }, 220);
  });
  elements["active-only"].addEventListener("change", (event) => {
    state.activeOnly = event.target.checked;
    state.offset = 0;
    loadQueue({ preserveSelection: false }).catch((error) => showToast(error.message));
  });
  elements["refresh-button"].addEventListener("click", () => loadQueue().catch((error) => showToast(error.message)));
  elements["previous-page"].addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.limit);
    loadQueue({ preserveSelection: false }).catch((error) => showToast(error.message));
  });
  elements["next-page"].addEventListener("click", () => {
    state.offset += state.limit;
    loadQueue({ preserveSelection: false }).catch((error) => showToast(error.message));
  });
  document.querySelectorAll("[data-direction]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-direction]").forEach((item) => item.classList.toggle("active", item === button));
    state.direction = button.dataset.direction;
    renderGraph();
  }));
  document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-tab]").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `${button.dataset.tab}-panel`));
  }));
  elements["associations-panel"].addEventListener("click", (event) => {
    const item = event.target.closest("[data-node]");
    if (item) highlightNode(item.dataset.node);
  });
  elements["paths-panel"].addEventListener("click", (event) => {
    const item = event.target.closest("[data-path]");
    if (item) highlightPath(item.dataset.path);
  });
  elements["graph-viewport"].addEventListener("click", (event) => {
    const edge = event.target.closest("[data-transaction]");
    const node = event.target.closest("[data-node]");
    if (edge) showTransaction(edge.dataset.transaction);
    if (node) highlightNode(node.dataset.node);
  });
  elements["zoom-in"].addEventListener("click", () => setGraphScale(state.graphScale + 0.15));
  elements["zoom-out"].addEventListener("click", () => setGraphScale(state.graphScale - 0.15));
  elements["fit-graph"].addEventListener("click", () => {
    state.graphScale = 1;
    state.graphPan = { x: 0, y: 0 };
    updateGraphTransform();
  });
  elements["fund-graph"].addEventListener("wheel", (event) => {
    event.preventDefault();
    setGraphScale(state.graphScale + (event.deltaY < 0 ? 0.08 : -0.08));
  }, { passive: false });
  let drag = null;
  elements["fund-graph"].addEventListener("pointerdown", (event) => {
    if (event.target.closest(".graph-node, .edge-group")) return;
    drag = { x: event.clientX, y: event.clientY, panX: state.graphPan.x, panY: state.graphPan.y };
    elements["fund-graph"].setPointerCapture(event.pointerId);
    elements["fund-graph"].classList.add("dragging");
  });
  elements["fund-graph"].addEventListener("pointermove", (event) => {
    if (!drag) return;
    state.graphPan.x = drag.panX + (event.clientX - drag.x);
    state.graphPan.y = drag.panY + (event.clientY - drag.y);
    updateGraphTransform();
  });
  elements["fund-graph"].addEventListener("pointerup", () => {
    drag = null;
    elements["fund-graph"].classList.remove("dragging");
  });
}

async function initialize() {
  bindEvents();
  try {
    await Promise.all([loadSummary(), loadQueue({ preserveSelection: false })]);
  } catch (error) {
    showToast(error.message);
  }
}

initialize();
