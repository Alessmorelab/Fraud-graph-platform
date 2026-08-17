"use strict";

const snapshotState = {
  manifest: null,
  index: 0,
  detail: null,
  requestToken: 0,
  playTimer: null,
  graphScale: 1,
  graphPan: { x: 0, y: 0 },
  graphMode: "focus",
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
    "snapshot-view-focus",
    "snapshot-view-all",
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
  if (["嫌疑人", "嫌疑账户", "涉诈账户"].includes(labelType)) return "suspect";
  if (["受害人", "受害账户"].includes(labelType)) return "victim";
  return "other";
}

function labelName(labelType) {
  const tone = labelTone(labelType);
  if (tone === "suspect") return "嫌疑账户";
  if (tone === "victim") return "受害账户";
  return "其他账户";
}

function formatGraphAmount(value) {
  const number = Math.abs(Number(value));
  if (number >= 1e8) return `${(number / 1e8).toFixed(number >= 1e10 ? 0 : 1)}亿`;
  if (number >= 1e4) return `${(number / 1e4).toFixed(number >= 1e6 ? 0 : 1)}万`;
  return snapshotNumber.format(number);
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

function snapshotEdgeRank(edge) {
  return Number(edge.amount_abs_sum) + Number(edge.transaction_count) * 1000;
}

function snapshotFocusedGraph(nodes, edges) {
  const indexedEdges = edges.map((edge, index) => ({ ...edge, _snapshotIndex: index }));
  if (snapshotState.graphMode === "all") return { nodes, edges: indexedEdges };

  const nodeLimit = 44;
  const edgeLimit = 70;
  const riskIds = new Set(
    nodes
      .filter((node) => labelTone(node.label_type) !== "other")
      .map((node) => String(node.account_id))
  );
  const selectedIds = new Set(riskIds);
  const selectedEdges = [];
  const selectedEdgeIndexes = new Set();
  const rankedEdges = [...indexedEdges].sort((a, b) => snapshotEdgeRank(b) - snapshotEdgeRank(a));

  function addEdge(edge) {
    if (selectedEdgeIndexes.has(edge._snapshotIndex)) return false;
    const endpoints = [String(edge.payer_account_id), String(edge.payee_account_id)];
    const missing = endpoints.filter((accountId) => !selectedIds.has(accountId));
    if (selectedIds.size + new Set(missing).size > nodeLimit) return false;
    endpoints.forEach((accountId) => selectedIds.add(accountId));
    selectedEdgeIndexes.add(edge._snapshotIndex);
    selectedEdges.push(edge);
    return true;
  }

  [...riskIds].forEach((accountId) => {
    const strongest = rankedEdges.find(
      (edge) =>
        String(edge.payer_account_id) === accountId ||
        String(edge.payee_account_id) === accountId
    );
    if (strongest) addEdge(strongest);
  });
  rankedEdges
    .filter(
      (edge) =>
        riskIds.has(String(edge.payer_account_id)) ||
        riskIds.has(String(edge.payee_account_id))
    )
    .forEach(addEdge);
  rankedEdges.forEach((edge) => {
    if (selectedEdges.length < edgeLimit) addEdge(edge);
  });
  rankedEdges.forEach((edge) => {
    if (selectedEdges.length >= edgeLimit) return;
    const endpointsSelected =
      selectedIds.has(String(edge.payer_account_id)) &&
      selectedIds.has(String(edge.payee_account_id));
    if (endpointsSelected) addEdge(edge);
  });

  return {
    nodes: nodes.filter((node) => selectedIds.has(String(node.account_id))),
    edges: selectedEdges.slice(0, edgeLimit),
  };
}

function calculateSnapshotLayout(nodes, edges) {
  const width = 1100;
  const height = 660;
  const positions = new Map();
  const ordered = [...nodes].sort((a, b) => {
    const toneOrder = { suspect: 0, victim: 1, other: 2 };
    const toneDifference = toneOrder[labelTone(a.label_type)] - toneOrder[labelTone(b.label_type)];
    if (toneDifference) return toneDifference;
    return snapshotEdgeRank({
      amount_abs_sum: Number(b.in_amount_abs_sum) + Number(b.out_amount_abs_sum),
      transaction_count: Number(b.in_transaction_count) + Number(b.out_transaction_count),
    }) - snapshotEdgeRank({
      amount_abs_sum: Number(a.in_amount_abs_sum) + Number(a.out_amount_abs_sum),
      transaction_count: Number(a.in_transaction_count) + Number(a.out_transaction_count),
    });
  });
  const toneIndexes = { suspect: 0, victim: 0, other: 0 };
  const toneCounts = Object.fromEntries(
    Object.keys(toneIndexes).map((tone) => [
      tone,
      ordered.filter((node) => labelTone(node.label_type) === tone).length,
    ])
  );
  ordered.forEach((node) => {
    const tone = labelTone(node.label_type);
    const index = toneIndexes[tone]++;
    const count = Math.max(1, toneCounts[tone]);
    const angle = index * 2.399963229728653 + (tone === "victim" ? 0.8 : 0);
    const progress = count <= 1 ? 0 : index / (count - 1);
    const radius =
      tone === "suspect"
        ? 45 + progress * 35
        : tone === "victim"
          ? 145 + progress * 80
          : 245 + Math.sqrt(progress) * 55;
    positions.set(String(node.account_id), {
      x: width / 2 + Math.cos(angle) * radius * 1.55,
      y: height / 2 + Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
    });
  });

  for (let iteration = 0; iteration < 150; iteration += 1) {
    const cooling = 1 - iteration / 170;
    for (let i = 0; i < ordered.length; i += 1) {
      const a = positions.get(String(ordered[i].account_id));
      for (let j = i + 1; j < ordered.length; j += 1) {
        const b = positions.get(String(ordered[j].account_id));
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const distanceSquared = Math.max(144, dx * dx + dy * dy);
        const force = 1850 / distanceSquared;
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
      const force = (distance - 128) * 0.0042;
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
      position.x = Math.max(54, Math.min(width - 76, position.x + position.vx * cooling));
      position.y = Math.max(44, Math.min(height - 44, position.y + position.vy * cooling));
    });
  }
  return positions;
}

function snapshotNodeRadius(node, maximum) {
  const throughput = Number(node.in_amount_abs_sum) + Number(node.out_amount_abs_sum);
  return 7 + (Math.log1p(throughput) / Math.max(1, Math.log1p(maximum))) * 11;
}

function snapshotEdgeGeometry(from, to, fromRadius, toRadius, curved) {
  if (from === to) {
    const radius = fromRadius + 14;
    return {
      d: `M ${from.x} ${from.y - radius / 2} C ${from.x + radius * 2} ${from.y - radius * 2}, ${from.x + radius * 2} ${from.y + radius * 2}, ${from.x} ${from.y + radius / 2}`,
      labelX: from.x + radius * 1.75,
      labelY: from.y - radius * 1.25,
    };
  }
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const length = Math.max(1, Math.hypot(dx, dy));
  const x1 = from.x + (dx / length) * (fromRadius + 2);
  const y1 = from.y + (dy / length) * (fromRadius + 2);
  const x2 = to.x - (dx / length) * (toRadius + 8);
  const y2 = to.y - (dy / length) * (toRadius + 8);
  const curve = curved ? 24 : 0;
  const controlX = (x1 + x2) / 2 + (-dy / length) * curve;
  const controlY = (y1 + y2) / 2 + (dx / length) * curve;
  return {
    d: `M ${x1} ${y1} Q ${controlX} ${controlY} ${x2} ${y2}`,
    labelX: (x1 + 2 * controlX + x2) / 4,
    labelY: (y1 + 2 * controlY + y2) / 4 - 5,
  };
}

function renderSnapshotGraph() {
  const graph = snapshotFocusedGraph(snapshotState.detail.nodes, snapshotState.detail.edges);
  const { nodes, edges } = graph;
  const viewport = snapshotElements["snapshot-graph-viewport"];
  snapshotElements["snapshot-graph-empty"].hidden = edges.length > 0;
  const fullNodeCount = snapshotState.detail.sample.node_count;
  const fullEdgeCount = snapshotState.detail.sample.edge_count;
  snapshotElements["snapshot-sample-badge"].textContent =
    snapshotState.graphMode === "focus"
      ? `${nodes.length}/${fullNodeCount} 节点 · ${edges.length}/${fullEdgeCount} 边`
      : `${nodes.length} 节点 · ${edges.length} 边`;
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
  const persistentNodeLabels = new Set(
    [...nodes]
      .sort(
        (a, b) =>
          Number(b.in_amount_abs_sum) + Number(b.out_amount_abs_sum) -
          (Number(a.in_amount_abs_sum) + Number(a.out_amount_abs_sum))
      )
      .slice(0, snapshotState.graphMode === "focus" ? 8 : 5)
      .map((node) => String(node.account_id))
  );
  nodes.forEach((node) => {
    if (labelTone(node.label_type) === "suspect") {
      persistentNodeLabels.add(String(node.account_id));
    }
  });
  const persistentEdgeLabels = new Set(
    [...edges]
      .sort((a, b) => snapshotEdgeRank(b) - snapshotEdgeRank(a))
      .slice(0, snapshotState.graphMode === "focus" ? 6 : 3)
      .map((edge) => edge._snapshotIndex)
  );
  const directedPairs = new Set(
    edges.map((edge) => `${edge.payer_account_id}|${edge.payee_account_id}`)
  );

  const edgeMarkup = edges
    .map((edge) => {
      const edgeIndex = edge._snapshotIndex;
      const from = positions.get(String(edge.payer_account_id));
      const to = positions.get(String(edge.payee_account_id));
      const fromNode = nodeById.get(String(edge.payer_account_id));
      const toNode = nodeById.get(String(edge.payee_account_id));
      if (!from || !to || !fromNode || !toNode) return "";
      const width =
        0.9 +
        (Math.log1p(Number(edge.amount_abs_sum)) / Math.log1p(maximumEdgeAmount)) * 3.3;
      const fromTone = labelTone(fromNode.label_type);
      const toTone = labelTone(toNode.label_type);
      const riskLinked = fromTone !== "other" || toTone !== "other";
      const marker = riskLinked ? "snapshot-arrow-risk" : "snapshot-arrow";
      const geometry = snapshotEdgeGeometry(
        from,
        to,
        snapshotNodeRadius(fromNode, maximumNodeAmount),
        snapshotNodeRadius(toNode, maximumNodeAmount),
        directedPairs.has(`${edge.payee_account_id}|${edge.payer_account_id}`)
      );
      const edgeLabelClass = persistentEdgeLabels.has(edgeIndex) ? " persistent" : "";
      return `<g class="snapshot-edge-group${riskLinked ? " risk-linked" : ""}" data-edge="${edgeIndex}" data-source="${snapshotEscape(edge.payer_account_id)}" data-target="${snapshotEscape(edge.payee_account_id)}" data-marker="${marker}">
        <path class="snapshot-edge-hit" d="${geometry.d}"></path>
        <path class="snapshot-edge" style="stroke-width:${width}" d="${geometry.d}" marker-end="url(#${marker})"></path>
        <text class="snapshot-edge-label${edgeLabelClass}" x="${geometry.labelX}" y="${geometry.labelY}">${snapshotEscape(formatGraphAmount(edge.amount_abs_sum))}</text>
      </g>`;
    })
    .join("");

  const nodeMarkup = nodes
    .map((node) => {
      const id = String(node.account_id);
      const position = positions.get(id);
      const radius = snapshotNodeRadius(node, maximumNodeAmount);
      const tone = labelTone(node.label_type);
      const labelClass = persistentNodeLabels.has(id) ? " persistent" : "";
      const glyph = tone === "suspect" ? "嫌" : tone === "victim" ? "受" : "";
      return `<g class="snapshot-node snapshot-node-${tone}" data-node="${snapshotEscape(id)}">
        <circle class="snapshot-node-halo" cx="${position.x}" cy="${position.y}" r="${radius + 5}"></circle>
        <circle class="snapshot-node-core" cx="${position.x}" cy="${position.y}" r="${radius}"></circle>
        ${glyph ? `<text class="snapshot-node-glyph" x="${position.x}" y="${position.y + 3.5}">${glyph}</text>` : ""}
        <text class="snapshot-node-label${labelClass}" x="${position.x + radius + 6}" y="${position.y + 4}">${snapshotEscape(id)}</text>
      </g>`;
    })
    .join("");
  viewport.innerHTML = edgeMarkup + nodeMarkup;
  updateSnapshotGraphTransform();
  applySnapshotGraphFocus(snapshotState.selectedNode, snapshotState.selectedEdge);
}

function applySnapshotGraphFocus(accountId = null, edgeIndex = null) {
  const nodeElements = [...document.querySelectorAll(".snapshot-node")];
  const edgeElements = [...document.querySelectorAll(".snapshot-edge-group")];
  const focusedIds = new Set();
  let hasFocus = false;

  if (accountId) {
    hasFocus = true;
    focusedIds.add(String(accountId));
    edgeElements.forEach((item) => {
      if (item.dataset.source === accountId || item.dataset.target === accountId) {
        focusedIds.add(item.dataset.source);
        focusedIds.add(item.dataset.target);
      }
    });
  } else if (edgeIndex !== null && edgeIndex !== undefined) {
    const selectedGroup = edgeElements.find(
      (item) => Number(item.dataset.edge) === Number(edgeIndex)
    );
    if (selectedGroup) {
      hasFocus = true;
      focusedIds.add(selectedGroup.dataset.source);
      focusedIds.add(selectedGroup.dataset.target);
    }
  }

  nodeElements.forEach((item) => {
    const related = focusedIds.has(item.dataset.node);
    item.classList.toggle("related", hasFocus && related);
    item.classList.toggle("dimmed", hasFocus && !related);
    item.classList.toggle("selected", Boolean(accountId) && item.dataset.node === accountId);
  });
  edgeElements.forEach((item) => {
    const isSelected =
      edgeIndex !== null && Number(item.dataset.edge) === Number(edgeIndex);
    const related = accountId
      ? item.dataset.source === accountId || item.dataset.target === accountId
      : isSelected;
    item.classList.toggle("related", hasFocus && related);
    item.classList.toggle("dimmed", hasFocus && !related);
    item.querySelector(".snapshot-edge")?.classList.toggle("selected", isSelected);
    item
      .querySelector(".snapshot-edge")
      ?.setAttribute(
        "marker-end",
        isSelected
          ? "url(#snapshot-arrow-selected)"
          : `url(#${item.dataset.marker || "snapshot-arrow"})`
      );
  });
}

function clearSnapshotSelection() {
  snapshotState.selectedNode = null;
  snapshotState.selectedEdge = null;
  applySnapshotGraphFocus();
  snapshotElements["snapshot-inspector"].innerHTML =
    "<span>选择节点或资金边查看当前窗口内的聚合信息</span>";
}

function inspectSnapshotNode(accountId) {
  const node = snapshotState.detail.nodes.find((item) => String(item.account_id) === accountId);
  if (!node) return;
  snapshotState.selectedNode = accountId;
  snapshotState.selectedEdge = null;
  applySnapshotGraphFocus(accountId, null);
  const counterparties = new Set();
  snapshotState.detail.edges.forEach((edge) => {
    const payer = String(edge.payer_account_id);
    const payee = String(edge.payee_account_id);
    if (payer === accountId) counterparties.add(payee);
    if (payee === accountId) counterparties.add(payer);
  });
  snapshotElements["snapshot-inspector"].innerHTML = `
    <strong>账户 ${snapshotEscape(accountId)}</strong>
    <span class="snapshot-label-${labelTone(node.label_type)}">${snapshotEscape(labelName(node.label_type))}</span>
    <span>${snapshotNumber.format(counterparties.size)} 个抽样对手方</span>
    <span>入 ${snapshotNumber.format(node.in_transaction_count)} 笔 / ${formatSnapshotAmount(node.in_amount_abs_sum)}</span>
    <span>出 ${snapshotNumber.format(node.out_transaction_count)} 笔 / ${formatSnapshotAmount(node.out_amount_abs_sum)}</span>
    <span>净流 ${formatSnapshotAmount(node.net_flow_amount)}</span>`;
}

function inspectSnapshotEdge(edgeIndex) {
  const edge = snapshotState.detail.edges[edgeIndex];
  if (!edge) return;
  snapshotState.selectedNode = null;
  snapshotState.selectedEdge = edgeIndex;
  applySnapshotGraphFocus(null, edgeIndex);
  snapshotElements["snapshot-inspector"].innerHTML = `
    <strong>${snapshotEscape(edge.payer_account_id)} → ${snapshotEscape(edge.payee_account_id)}</strong>
    <strong class="amount">${formatSnapshotAmount(edge.amount_abs_sum)}</strong>
    <span>${snapshotNumber.format(edge.transaction_count)} 笔聚合交易</span>
    <span>${formatSnapshotDate(edge.first_event_utc, true)} 至 ${formatSnapshotDate(edge.last_event_utc, true)}</span>`;
}

function setSnapshotGraphMode(mode) {
  if (!snapshotState.detail || !["focus", "all"].includes(mode)) return;
  snapshotState.graphMode = mode;
  snapshotElements["snapshot-view-focus"].classList.toggle("active", mode === "focus");
  snapshotElements["snapshot-view-all"].classList.toggle("active", mode === "all");
  clearSnapshotSelection();
  snapshotState.graphScale = 1;
  snapshotState.graphPan = { x: 0, y: 0 };
  renderSnapshotGraph();
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
  snapshotElements["snapshot-view-focus"].addEventListener("click", () =>
    setSnapshotGraphMode("focus")
  );
  snapshotElements["snapshot-view-all"].addEventListener("click", () =>
    setSnapshotGraphMode("all")
  );
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
    else clearSnapshotSelection();
  });
  snapshotElements["snapshot-graph-viewport"].addEventListener("pointerover", (event) => {
    if (snapshotState.selectedNode || snapshotState.selectedEdge !== null) return;
    const node = event.target.closest("[data-node]");
    const edge = event.target.closest("[data-edge]");
    if (node) applySnapshotGraphFocus(node.dataset.node, null);
    else if (edge) applySnapshotGraphFocus(null, Number(edge.dataset.edge));
  });
  snapshotElements["snapshot-graph"].addEventListener("pointerleave", () => {
    applySnapshotGraphFocus(snapshotState.selectedNode, snapshotState.selectedEdge);
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
