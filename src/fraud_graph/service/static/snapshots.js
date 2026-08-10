"use strict";

const snapshotState = {
  manifest: null,
  index: 0,
  detail: null,
  requestToken: 0,
  playTimer: null,
  graphScale: 1,
  graphPan: { x: 0, y: 0 },
  selectedNode: null,
  selectedEdge: null,
};

const snapshotElements = Object.fromEntries(
  [
    "snapshot-position",
    "snapshot-active-nodes",
    "snapshot-edge-count",
    "snapshot-event-count",
    "snapshot-total-amount",
    "snapshot-previous",
    "snapshot-play",
    "snapshot-next",
    "snapshot-anchor",
    "snapshot-window",
    "snapshot-slider",
    "snapshot-picker",
    "snapshot-sample-badge",
    "snapshot-graph",
    "snapshot-graph-viewport",
    "snapshot-graph-empty",
    "snapshot-inspector",
    "snapshot-window-start",
    "snapshot-window-end",
    "snapshot-window-days",
    "snapshot-step-days",
    "snapshot-components",
    "snapshot-largest-component",
    "snapshot-suspects",
    "snapshot-victims",
    "trend-node-value",
    "trend-event-value",
    "trend-amount-value",
    "trend-nodes",
    "trend-events",
    "trend-amount",
    "snapshot-zoom-out",
    "snapshot-fit",
    "snapshot-zoom-in",
    "snapshot-toast",
  ].map((id) => [id, document.getElementById(id)])
);

const snapshotNumber = new Intl.NumberFormat("zh-CN");
const compactNumber = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

function snapshotEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function snapshotApi(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `请求失败 (${response.status})`);
  }
  return response.json();
}

function snapshotToast(message) {
  snapshotElements["snapshot-toast"].textContent = message;
  snapshotElements["snapshot-toast"].classList.add("visible");
  window.clearTimeout(snapshotToast.timer);
  snapshotToast.timer = window.setTimeout(
    () => snapshotElements["snapshot-toast"].classList.remove("visible"),
    3500
  );
}

function formatSnapshotDate(value, includeTime = false) {
  return new Date(value).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(includeTime ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}),
  });
}

function formatSnapshotAmount(value) {
  const number = Number(value);
  if (number >= 1e12) return `¥${(number / 1e12).toFixed(2)} 万亿`;
  if (number >= 1e8) return `¥${(number / 1e8).toFixed(2)} 亿`;
  if (number >= 1e4) return `¥${(number / 1e4).toFixed(1)} 万`;
  return `¥${snapshotNumber.format(number)}`;
}

function labelTone(labelType) {
  if (labelType === "涉诈账户") return "suspect";
  if (labelType === "受害人") return "victim";
  return "other";
}

function labelName(labelType) {
  return labelType === "其它" ? "其他账户" : labelType;
}

async function loadSnapshotManifest() {
  const manifest = await snapshotApi("/v1/graph-snapshots");
  snapshotState.manifest = manifest;
  snapshotState.index = Math.max(0, manifest.snapshots.length - 1);
  snapshotElements["snapshot-slider"].max = String(snapshotState.index);
  snapshotElements["snapshot-picker"].innerHTML = manifest.snapshots
    .map(
      (item, index) =>
        `<option value="${index}">${String(index + 1).padStart(2, "0")} · ${snapshotEscape(formatSnapshotDate(item.anchor_at))}</option>`
    )
    .join("");
  await selectSnapshot(snapshotState.index);
}

async function selectSnapshot(index) {
  if (!snapshotState.manifest?.snapshots.length) return;
  const boundedIndex = Math.max(0, Math.min(index, snapshotState.manifest.snapshots.length - 1));
  const token = ++snapshotState.requestToken;
  snapshotState.index = boundedIndex;
  snapshotState.selectedNode = null;
  snapshotState.selectedEdge = null;
  syncSnapshotControls();
  snapshotElements["snapshot-sample-badge"].textContent = "加载中";
  snapshotElements["snapshot-graph-viewport"].classList.add("snapshot-loading");
  try {
    const item = snapshotState.manifest.snapshots[boundedIndex];
    const detail = await snapshotApi(
      `/v1/graph-snapshots/${encodeURIComponent(item.snapshot_id)}?node_limit=70&edge_limit=150`
    );
    if (token !== snapshotState.requestToken) return;
    snapshotState.detail = detail;
    snapshotState.graphScale = 1;
    snapshotState.graphPan = { x: 0, y: 0 };
    renderSnapshot();
  } catch (error) {
    snapshotToast(error.message);
  } finally {
    if (token === snapshotState.requestToken) {
      snapshotElements["snapshot-graph-viewport"].classList.remove("snapshot-loading");
    }
  }
}

function syncSnapshotControls() {
  const count = snapshotState.manifest?.snapshots.length || 0;
  snapshotElements["snapshot-slider"].value = String(snapshotState.index);
  snapshotElements["snapshot-picker"].value = String(snapshotState.index);
  snapshotElements["snapshot-previous"].disabled = snapshotState.index === 0;
  snapshotElements["snapshot-next"].disabled = snapshotState.index >= count - 1;
}

function renderSnapshot() {
  const { manifest, detail, index } = snapshotState;
  if (!manifest || !detail) return;
  const item = detail.snapshot;
  snapshotElements["snapshot-position"].textContent = `${index + 1} / ${manifest.snapshot_count}`;
  snapshotElements["snapshot-active-nodes"].textContent = snapshotNumber.format(item.active_node_count);
  snapshotElements["snapshot-edge-count"].textContent = snapshotNumber.format(item.directed_edge_count);
  snapshotElements["snapshot-event-count"].textContent = snapshotNumber.format(item.event_count);
  snapshotElements["snapshot-total-amount"].textContent = formatSnapshotAmount(item.total_amount);
  snapshotElements["snapshot-anchor"].textContent = `${formatSnapshotDate(item.anchor_at)} 观察时点`;
  snapshotElements["snapshot-window"].textContent = `${formatSnapshotDate(item.window_start)} 至 ${formatSnapshotDate(item.anchor_at)}（左闭右开）`;
  snapshotElements["snapshot-window-start"].textContent = formatSnapshotDate(item.window_start, true);
  snapshotElements["snapshot-window-end"].textContent = `${formatSnapshotDate(item.anchor_at, true)} 前`;
  snapshotElements["snapshot-window-days"].textContent = `${manifest.observation_days} 天`;
  snapshotElements["snapshot-step-days"].textContent = `${manifest.anchor_step_days} 天`;
  snapshotElements["snapshot-components"].textContent = snapshotNumber.format(item.weak_component_count);
  snapshotElements["snapshot-largest-component"].textContent = snapshotNumber.format(item.largest_component_size);
  snapshotElements["snapshot-suspects"].textContent = snapshotNumber.format(item.active_suspect_count);
  snapshotElements["snapshot-victims"].textContent = snapshotNumber.format(item.active_victim_count);
  snapshotElements["snapshot-sample-badge"].textContent = `${detail.sample.node_count} 节点 · ${detail.sample.edge_count} 边`;
  snapshotElements["snapshot-inspector"].innerHTML = "<span>选择节点或资金边查看当前窗口内的聚合信息</span>";
  renderSnapshotTrends();
  renderSnapshotGraph();
}

function renderSnapshotTrends() {
  const snapshots = snapshotState.manifest.snapshots;
  const current = snapshots[snapshotState.index];
  snapshotElements["trend-node-value"].textContent = snapshotNumber.format(current.active_node_count);
  snapshotElements["trend-event-value"].textContent = snapshotNumber.format(current.event_count);
  snapshotElements["trend-amount-value"].textContent = formatSnapshotAmount(current.total_amount).replace("¥", "");
  renderSparkline("trend-nodes", snapshots.map((item) => Number(item.active_node_count)));
  renderSparkline("trend-events", snapshots.map((item) => Number(item.event_count)));
  renderSparkline("trend-amount", snapshots.map((item) => Number(item.total_amount)));
}

function renderSparkline(elementId, values) {
  const width = 280;
  const height = 54;
  const padX = 4;
  const padY = 7;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = Math.max(1, max - min);
  const point = (value, index) => ({
    x: padX + (index / Math.max(1, values.length - 1)) * (width - padX * 2),
    y: height - padY - ((value - min) / span) * (height - padY * 2),
  });
  const points = values.map(point);
  const selected = points[snapshotState.index];
  const area = `M ${points[0].x} ${height - padY} L ${points.map((item) => `${item.x} ${item.y}`).join(" L ")} L ${points.at(-1).x} ${height - padY} Z`;
  snapshotElements[elementId].innerHTML = `
    <path class="snapshot-spark-area" d="${area}"></path>
    <polyline class="snapshot-spark-line" points="${points.map((item) => `${item.x},${item.y}`).join(" ")}"></polyline>
    <line class="snapshot-spark-guide" x1="${selected.x}" y1="4" x2="${selected.x}" y2="50"></line>
    <circle class="snapshot-spark-point" cx="${selected.x}" cy="${selected.y}" r="3.6"></circle>`;
}

function calculateSnapshotLayout(nodes, edges) {
  const width = 1100;
  const height = 660;
  const positions = new Map();
  const ordered = [...nodes].sort((a, b) => String(a.account_id).localeCompare(String(b.account_id)));
  ordered.forEach((node, index) => {
    const angle = index * 2.399963229728653;
    const radius = 44 + Math.sqrt(index / Math.max(1, ordered.length - 1)) * 250;
    positions.set(String(node.account_id), {
      x: width / 2 + Math.cos(angle) * radius * 1.55,
      y: height / 2 + Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
    });
  });

  for (let iteration = 0; iteration < 130; iteration += 1) {
    const cooling = 1 - iteration / 150;
    for (let i = 0; i < ordered.length; i += 1) {
      const a = positions.get(String(ordered[i].account_id));
      for (let j = i + 1; j < ordered.length; j += 1) {
        const b = positions.get(String(ordered[j].account_id));
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distanceSquared = Math.max(144, dx * dx + dy * dy);
        const force = 1550 / distanceSquared;
        a.vx += dx * force;
        a.vy += dy * force;
        b.vx -= dx * force;
        b.vy -= dy * force;
      }
    }
    edges.forEach((edge) => {
      const from = positions.get(String(edge.payer_account_id));
      const to = positions.get(String(edge.payee_account_id));
      if (!from || !to || from === to) return;
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const force = (distance - 118) * 0.0035;
      from.vx += dx * force;
      from.vy += dy * force;
      to.vx -= dx * force;
      to.vy -= dy * force;
    });
    positions.forEach((position) => {
      position.vx += (width / 2 - position.x) * 0.0018;
      position.vy += (height / 2 - position.y) * 0.0018;
      position.vx *= 0.72;
      position.vy *= 0.72;
      position.x = Math.max(34, Math.min(width - 34, position.x + position.vx * cooling));
      position.y = Math.max(34, Math.min(height - 34, position.y + position.vy * cooling));
    });
  }
  return positions;
}

function snapshotNodeRadius(node, maximum) {
  const throughput = Number(node.in_amount_abs_sum) + Number(node.out_amount_abs_sum);
  return 7 + (Math.log1p(throughput) / Math.max(1, Math.log1p(maximum))) * 11;
}

function renderSnapshotGraph() {
  const { nodes, edges } = snapshotState.detail;
  const viewport = snapshotElements["snapshot-graph-viewport"];
  snapshotElements["snapshot-graph-empty"].hidden = edges.length > 0;
  if (!nodes.length) {
    viewport.innerHTML = "";
    return;
  }
  const positions = calculateSnapshotLayout(nodes, edges);
  const nodeById = new Map(nodes.map((node) => [String(node.account_id), node]));
  const maximumNodeAmount = Math.max(
    1,
    ...nodes.map((node) => Number(node.in_amount_abs_sum) + Number(node.out_amount_abs_sum))
  );
  const maximumEdgeAmount = Math.max(1, ...edges.map((edge) => Number(edge.amount_abs_sum)));
  const labeled = new Set(
    [...nodes]
      .sort(
        (a, b) =>
          Number(b.in_amount_abs_sum) + Number(b.out_amount_abs_sum) -
          (Number(a.in_amount_abs_sum) + Number(a.out_amount_abs_sum))
      )
      .slice(0, 12)
      .map((node) => String(node.account_id))
  );
  nodes.forEach((node) => {
    if (node.label_type === "涉诈账户") labeled.add(String(node.account_id));
  });

  const edgeMarkup = edges
    .map((edge, edgeIndex) => {
      const from = positions.get(String(edge.payer_account_id));
      const to = positions.get(String(edge.payee_account_id));
      const fromNode = nodeById.get(String(edge.payer_account_id));
      const toNode = nodeById.get(String(edge.payee_account_id));
      if (!from || !to || !fromNode || !toNode) return "";
      const width = 0.8 + (Math.log1p(Number(edge.amount_abs_sum)) / Math.log1p(maximumEdgeAmount)) * 3.2;
      if (from === to) {
        const radius = snapshotNodeRadius(fromNode, maximumNodeAmount) + 14;
        return `<g class="snapshot-edge-group" data-edge="${edgeIndex}">
          <path class="snapshot-edge-hit" d="M ${from.x} ${from.y - radius / 2} C ${from.x + radius * 2} ${from.y - radius * 2}, ${from.x + radius * 2} ${from.y + radius * 2}, ${from.x} ${from.y + radius / 2}"></path>
          <path class="snapshot-edge" style="stroke-width:${width}" marker-end="url(#snapshot-arrow)" d="M ${from.x} ${from.y - radius / 2} C ${from.x + radius * 2} ${from.y - radius * 2}, ${from.x + radius * 2} ${from.y + radius * 2}, ${from.x} ${from.y + radius / 2}"></path>
        </g>`;
      }
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const length = Math.max(1, Math.hypot(dx, dy));
      const fromRadius = snapshotNodeRadius(fromNode, maximumNodeAmount) + 2;
      const toRadius = snapshotNodeRadius(toNode, maximumNodeAmount) + 8;
      const x1 = from.x + (dx / length) * fromRadius;
      const y1 = from.y + (dy / length) * fromRadius;
      const x2 = to.x - (dx / length) * toRadius;
      const y2 = to.y - (dy / length) * toRadius;
      return `<g class="snapshot-edge-group" data-edge="${edgeIndex}">
        <line class="snapshot-edge-hit" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"></line>
        <line class="snapshot-edge" style="stroke-width:${width}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" marker-end="url(#snapshot-arrow)"></line>
      </g>`;
    })
    .join("");

  const nodeMarkup = nodes
    .map((node) => {
      const id = String(node.account_id);
      const position = positions.get(id);
      const radius = snapshotNodeRadius(node, maximumNodeAmount);
      const tone = labelTone(node.label_type);
      const label = labeled.has(id)
        ? `<text class="snapshot-node-label" x="${position.x + radius + 5}" y="${position.y + 4}">${snapshotEscape(id)}</text>`
        : "";
      return `<g class="snapshot-node snapshot-node-${tone}" data-node="${snapshotEscape(id)}">
        <circle class="snapshot-node-halo" cx="${position.x}" cy="${position.y}" r="${radius + 5}"></circle>
        <circle class="snapshot-node-core" cx="${position.x}" cy="${position.y}" r="${radius}"></circle>
        ${label}
      </g>`;
    })
    .join("");
  viewport.innerHTML = edgeMarkup + nodeMarkup;
  updateSnapshotGraphTransform();
}

function inspectSnapshotNode(accountId) {
  const node = snapshotState.detail.nodes.find((item) => String(item.account_id) === accountId);
  if (!node) return;
  snapshotState.selectedNode = accountId;
  snapshotState.selectedEdge = null;
  document.querySelectorAll(".snapshot-node").forEach((item) => {
    item.classList.toggle("selected", item.dataset.node === accountId);
  });
  document.querySelectorAll(".snapshot-edge").forEach((item) => item.classList.remove("selected"));
  snapshotElements["snapshot-inspector"].innerHTML = `
    <strong>账户 ${snapshotEscape(accountId)}</strong>
    <span class="snapshot-label-${labelTone(node.label_type)}">${snapshotEscape(labelName(node.label_type))}</span>
    <span>入 ${snapshotNumber.format(node.in_transaction_count)} 笔 / ${formatSnapshotAmount(node.in_amount_abs_sum)}</span>
    <span>出 ${snapshotNumber.format(node.out_transaction_count)} 笔 / ${formatSnapshotAmount(node.out_amount_abs_sum)}</span>
    <span>净流 ${formatSnapshotAmount(node.net_flow_amount)}</span>`;
}

function inspectSnapshotEdge(edgeIndex) {
  const edge = snapshotState.detail.edges[edgeIndex];
  if (!edge) return;
  snapshotState.selectedNode = null;
  snapshotState.selectedEdge = edgeIndex;
  document.querySelectorAll(".snapshot-node").forEach((item) => item.classList.remove("selected"));
  document.querySelectorAll(".snapshot-edge-group").forEach((item, index) => {
    const selected = index === edgeIndex;
    item.querySelector(".snapshot-edge")?.classList.toggle("selected", selected);
    item.querySelector(".snapshot-edge")?.setAttribute(
      "marker-end",
      selected ? "url(#snapshot-arrow-selected)" : "url(#snapshot-arrow)"
    );
  });
  snapshotElements["snapshot-inspector"].innerHTML = `
    <strong>${snapshotEscape(edge.payer_account_id)} → ${snapshotEscape(edge.payee_account_id)}</strong>
    <strong class="amount">${formatSnapshotAmount(edge.amount_abs_sum)}</strong>
    <span>${snapshotNumber.format(edge.transaction_count)} 笔聚合交易</span>
    <span>${formatSnapshotDate(edge.first_event_utc, true)} 至 ${formatSnapshotDate(edge.last_event_utc, true)}</span>`;
}

function updateSnapshotGraphTransform() {
  snapshotElements["snapshot-graph-viewport"].setAttribute(
    "transform",
    `translate(${snapshotState.graphPan.x} ${snapshotState.graphPan.y}) scale(${snapshotState.graphScale})`
  );
}

function setSnapshotScale(value) {
  snapshotState.graphScale = Math.min(2.4, Math.max(0.55, value));
  updateSnapshotGraphTransform();
}

function toggleSnapshotPlayback() {
  if (snapshotState.playTimer) {
    window.clearInterval(snapshotState.playTimer);
    snapshotState.playTimer = null;
    snapshotElements["snapshot-play"].textContent = "▶";
    snapshotElements["snapshot-play"].setAttribute("aria-label", "播放快照");
    snapshotElements["snapshot-play"].title = "播放快照";
    return;
  }
  snapshotElements["snapshot-play"].textContent = "Ⅱ";
  snapshotElements["snapshot-play"].setAttribute("aria-label", "暂停播放");
  snapshotElements["snapshot-play"].title = "暂停播放";
  snapshotState.playTimer = window.setInterval(() => {
    const count = snapshotState.manifest.snapshots.length;
    selectSnapshot((snapshotState.index + 1) % count);
  }, 1800);
}

function bindSnapshotEvents() {
  snapshotElements["snapshot-previous"].addEventListener("click", () => selectSnapshot(snapshotState.index - 1));
  snapshotElements["snapshot-next"].addEventListener("click", () => selectSnapshot(snapshotState.index + 1));
  snapshotElements["snapshot-play"].addEventListener("click", toggleSnapshotPlayback);
  snapshotElements["snapshot-slider"].addEventListener("change", (event) => selectSnapshot(Number(event.target.value)));
  snapshotElements["snapshot-picker"].addEventListener("change", (event) => selectSnapshot(Number(event.target.value)));
  snapshotElements["snapshot-zoom-in"].addEventListener("click", () => setSnapshotScale(snapshotState.graphScale + 0.15));
  snapshotElements["snapshot-zoom-out"].addEventListener("click", () => setSnapshotScale(snapshotState.graphScale - 0.15));
  snapshotElements["snapshot-fit"].addEventListener("click", () => {
    snapshotState.graphScale = 1;
    snapshotState.graphPan = { x: 0, y: 0 };
    updateSnapshotGraphTransform();
  });
  snapshotElements["snapshot-graph-viewport"].addEventListener("click", (event) => {
    const node = event.target.closest("[data-node]");
    const edge = event.target.closest("[data-edge]");
    if (node) inspectSnapshotNode(node.dataset.node);
    else if (edge) inspectSnapshotEdge(Number(edge.dataset.edge));
  });
  snapshotElements["snapshot-graph"].addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      setSnapshotScale(snapshotState.graphScale + (event.deltaY < 0 ? 0.08 : -0.08));
    },
    { passive: false }
  );
  let drag = null;
  snapshotElements["snapshot-graph"].addEventListener("pointerdown", (event) => {
    if (event.target.closest(".snapshot-node, .snapshot-edge-group")) return;
    drag = {
      x: event.clientX,
      y: event.clientY,
      panX: snapshotState.graphPan.x,
      panY: snapshotState.graphPan.y,
    };
    snapshotElements["snapshot-graph"].setPointerCapture(event.pointerId);
    snapshotElements["snapshot-graph"].classList.add("dragging");
  });
  snapshotElements["snapshot-graph"].addEventListener("pointermove", (event) => {
    if (!drag) return;
    snapshotState.graphPan.x = drag.panX + (event.clientX - drag.x);
    snapshotState.graphPan.y = drag.panY + (event.clientY - drag.y);
    updateSnapshotGraphTransform();
  });
  snapshotElements["snapshot-graph"].addEventListener("pointerup", () => {
    drag = null;
    snapshotElements["snapshot-graph"].classList.remove("dragging");
  });
}

async function initializeSnapshots() {
  bindSnapshotEvents();
  try {
    await loadSnapshotManifest();
  } catch (error) {
    snapshotToast(error.message);
    snapshotElements["snapshot-anchor"].textContent = "动态图快照暂不可用";
  }
}

initializeSnapshots();
