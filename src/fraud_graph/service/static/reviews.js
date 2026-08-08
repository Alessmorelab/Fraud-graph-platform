"use strict";

const reviewState = {
  queue: null,
  acceptance: null,
  selectedCase: null,
};

const reviewElements = Object.fromEntries(
  [
    "review-version",
    "human-review-count",
    "resolved-case-count",
    "case-pass-rate",
    "write-status",
    "review-case-total",
    "review-refresh",
    "review-case-list",
    "current-case",
    "current-account",
    "current-grade",
    "review-evidence-body",
    "selected-case-status",
    "primary-reviewers",
    "packet-hash",
    "case-report-link",
    "review-toast",
  ].map((id) => [id, document.getElementById(id)])
);

const scoreFormatter = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 3,
  maximumFractionDigits: 3,
});
const amountFormatter = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

function escapeReviewHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function reviewApi(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `请求失败 (${response.status})`);
  }
  return response.json();
}

function showReviewToast(message) {
  reviewElements["review-toast"].textContent = message;
  reviewElements["review-toast"].classList.add("visible");
  window.setTimeout(() => reviewElements["review-toast"].classList.remove("visible"), 3200);
}

function statusLabel(status) {
  return {
    awaiting_primary_reviews: "待双人主审",
    adjudication_required: "待独立仲裁",
    passed: "已通过",
    failed: "未通过",
  }[status] || status;
}

function renderReviewSummary() {
  const queue = reviewState.queue;
  const acceptance = reviewState.acceptance;
  reviewElements["review-version"].textContent = queue.case_sample_version;
  reviewElements["human-review-count"].textContent = String(acceptance.human_reviews);
  reviewElements["resolved-case-count"].textContent = `${acceptance.resolved_cases} / ${acceptance.required_case_count}`;
  reviewElements["case-pass-rate"].textContent = acceptance.case_pass_rate == null
    ? "待复核"
    : `${(acceptance.case_pass_rate * 100).toFixed(1)}%`;
  reviewElements["write-status"].textContent = queue.write_enabled ? "受控开放" : "只读";
  reviewElements["review-case-total"].textContent = `${queue.items.length} 个`;
}

function renderReviewQueue() {
  const items = reviewState.queue.items;
  reviewElements["review-case-list"].innerHTML = items.map((item) => `
    <button type="button" class="review-case-row ${item.case_id === reviewState.selectedCase ? "active" : ""}" data-case="${escapeReviewHtml(item.case_id)}">
      <span>
        <strong>${escapeReviewHtml(item.case_id)}</strong>
        <small>账户 ${escapeReviewHtml(item.account_id)} · 风险 ${scoreFormatter.format(item.risk_score)}</small>
      </span>
      <b class="review-state review-state-${escapeReviewHtml(item.status)}">${escapeReviewHtml(statusLabel(item.status))}</b>
    </button>
  `).join("");
}

function renderReviewCase(detail) {
  const packet = detail.packet;
  reviewElements["current-case"].textContent = detail.case_id;
  reviewElements["current-account"].textContent = `账户 ${detail.account_id}`;
  reviewElements["current-grade"].textContent = `证据 ${packet.grade} 级`;
  reviewElements["selected-case-status"].textContent = statusLabel(detail.status);
  reviewElements["selected-case-status"].className = `selected-case-status review-state-${detail.status}`;
  reviewElements["primary-reviewers"].textContent = `${detail.primary_reviewer_count} / 2`;
  reviewElements["packet-hash"].textContent = `${detail.packet_sha256.slice(0, 12)}…${detail.packet_sha256.slice(-8)}`;
  reviewElements["packet-hash"].title = detail.packet_sha256;
  reviewElements["case-report-link"].href = `/v1/reviews/cases/${encodeURIComponent(detail.case_id)}/report`;

  const facts = packet.facts.map((fact) => `
    <div class="review-fact">
      <span>${escapeReviewHtml(fact.code)}</span>
      <strong>${escapeReviewHtml(fact.value)}</strong>
      <p>${escapeReviewHtml(fact.description)}</p>
      ${fact.transaction_ids.length ? `<small>${escapeReviewHtml(fact.transaction_ids.join(" · "))}</small>` : ""}
    </div>
  `).join("");
  const paths = packet.paths.map((path, index) => `
    <section class="review-path">
      <div class="review-path-head">
        <strong>路径 ${index + 1}</strong>
        <span>${path.steps.length} 跳 · ${escapeReviewHtml(path.direction)} · ${scoreFormatter.format(path.score)}</span>
      </div>
      <ol>${path.steps.map((step) => `
        <li>
          <div><b>${escapeReviewHtml(step.payer_account_id)}</b><span>→</span><b>${escapeReviewHtml(step.payee_account_id)}</b></div>
          <p>${escapeReviewHtml(step.transaction_id)} · ${amountFormatter.format(Number(step.amount))} · ${new Date(step.event_time_utc).toLocaleString("zh-CN", { hour12: false })}</p>
        </li>
      `).join("")}</ol>
    </section>
  `).join("");

  reviewElements["review-evidence-body"].innerHTML = `
    <section class="review-risk-summary">
      <div><span>风险分数</span><strong>${scoreFormatter.format(packet.score.risk_score)}</strong></div>
      <div><span>风险排序</span><strong>#${packet.score.risk_rank}</strong></div>
      <div><span>路径数量</span><strong>${packet.paths.length}</strong></div>
      <div><span>上线资格</span><strong>${packet.score.production_eligible ? "可用" : "禁止"}</strong></div>
    </section>
    <p class="section-label">结构化证据</p>
    <div class="review-facts">${facts}</div>
    <p class="section-label">资金链路</p>
    <div class="review-paths">${paths || '<p class="limit-item">当前案例没有合格资金链路。</p>'}</div>
    <div class="review-notes-grid">
      <section><p class="section-label">局限性</p>${packet.limitations.map((item) => `<p>${escapeReviewHtml(item)}</p>`).join("")}</section>
      <section><p class="section-label">辅助建议</p>${packet.recommended_actions.map((item) => `<p>${escapeReviewHtml(item)}</p>`).join("")}</section>
    </div>
  `;
}

async function selectReviewCase(caseId) {
  reviewState.selectedCase = caseId;
  renderReviewQueue();
  const detail = await reviewApi(`/v1/reviews/cases/${encodeURIComponent(caseId)}`);
  renderReviewCase(detail);
}

async function loadReviewWorkspace() {
  const [queue, acceptance] = await Promise.all([
    reviewApi("/v1/reviews/cases"),
    reviewApi("/v1/reviews/status"),
  ]);
  reviewState.queue = queue;
  reviewState.acceptance = acceptance;
  renderReviewSummary();
  const selected = queue.items.some((item) => item.case_id === reviewState.selectedCase)
    ? reviewState.selectedCase
    : queue.items[0]?.case_id;
  if (selected) await selectReviewCase(selected);
}

reviewElements["review-case-list"].addEventListener("click", (event) => {
  const row = event.target.closest("[data-case]");
  if (row) selectReviewCase(row.dataset.case).catch((error) => showReviewToast(error.message));
});
reviewElements["review-refresh"].addEventListener("click", () => {
  loadReviewWorkspace().catch((error) => showReviewToast(error.message));
});

loadReviewWorkspace().catch((error) => {
  reviewElements["review-evidence-body"].innerHTML = `<div class="review-empty">${escapeReviewHtml(error.message)}</div>`;
  showReviewToast(error.message);
});
