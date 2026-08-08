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
  try {
    const workspace = await api(`/v1/accounts/${encodeURIComponent(accountId)}/workspace?association_limit=20`);
    if (state.selectedAccount !== accountId) return;
    state.workspace = workspace;
    state.graphScale = 1;
    state.graphPan = { x: 0, y: 0 };
    elements["risk-badge"].textContent = `风险 ${formatScore.format(workspace.evidence.score.risk_score)}`;
    elements["report-link"].href = `/v1/accounts/${encodeURIComponent(accountId)}/report`;
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
      <div class="summary-cell"><span>风险排序</span><strong>#${formatNumber.format(score.risk_rank)}</strong></div>
      <div class="summary-cell"><span>证据等级</span><strong>${escapeHtml(evidence.grade)}</strong></div>
      <div class="summary-cell"><span>训练模式</span><strong>${escapeHtml(score.model_training_mode)}</strong></div>
    </div>
    <p class="section-label">上线阻断项</p>
    <div class="blocker-list">${score.eligibility_blockers.map((item) => `<div class="blocker">${escapeHtml(item)}</div>`).join("")}</div>
    <p class="section-label">结构化证据</p>
    <div class="evidence-list">${evidence.facts.map((fact) => `
      <div class="evidence-item">
        <span>${escapeHtml(fact.code)}</span>
        <strong>${escapeHtml(fact.description)}</strong>
        <span class="evidence-value">${escapeHtml(fact.value)}</span>
      </div>`).join("")}</div>
    <p class="section-label">局限性</p>
    ${evidence.limitations.map((item) => `<p class="limit-item">${escapeHtml(item)}</p>`).join("")}
  `;

  elements["associations-panel"].innerHTML = `
    <p class="section-label">Top ${associations.length} 关联账户</p>
    <div class="association-list">${associations.length ? associations.map((item) => `
      <button type="button" class="association-item" data-node="${escapeHtml(item.account_id)}">
        <div class="association-head"><strong>${escapeHtml(item.account_id)}</strong><b class="association-score">${formatScore.format(item.association_score)}</b></div>
        <span>${directionLabel(item.direction)} · ${item.min_hops} 跳 · ${item.transaction_ids.length} 笔证据</span>
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
}

function directionLabel(direction) {
  return { incoming: "流入", outgoing: "流出", both: "双向" }[direction] || direction;
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
