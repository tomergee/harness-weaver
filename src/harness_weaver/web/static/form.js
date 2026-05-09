// Configuration explainer sidebar. The form pages embed every built-in
// configuration as JSON (a <script type="application/json"> tag); this
// script reads that, watches the configuration <select>(s), and renders
// the picked configuration's tools / model / agents / system-prompt
// excerpt into the right-side panel. No fetch round-trip needed.

(function () {
  "use strict";

  const dataNode = document.getElementById("config-summaries-data");
  if (!dataNode) return;
  let summaries;
  try {
    summaries = JSON.parse(dataNode.textContent || "{}");
  } catch (err) {
    console.error("config summaries JSON parse failed", err);
    return;
  }

  function renderSummary(summary) {
    if (!summary) {
      return '<p class="empty">No summary available.</p>';
    }
    const tools = (summary.allowed_tools || []).map((t) => `<code>${escapeHtml(t)}</code>`).join(", ") || "<em>(none)</em>";
    const agentsBlock = summary.agents.length
      ? renderAgents(summary.agents)
      : "<p><em>Single-agent ReAct loop — no workers.</em></p>";
    const promptExcerpt = (summary.system_prompt || "").trim();
    const promptHtml = promptExcerpt
      ? `<details><summary>System prompt (${promptExcerpt.length} chars)</summary><pre class="explainer-prompt">${escapeHtml(promptExcerpt)}</pre></details>`
      : "";
    return `
      <p>${escapeHtml(summary.description || "")}</p>
      <dl class="summary explainer-meta">
        <dt>Model</dt><dd><code>${escapeHtml(summary.model || "")}</code></dd>
        <dt>Tools</dt><dd>${tools}</dd>
        <dt>Topology</dt><dd>${summary.is_multi_agent ? "Orchestrator + workers" : "Single agent"}</dd>
      </dl>
      ${agentsBlock}
      ${promptHtml}
    `;
  }

  function renderAgents(agents) {
    const items = agents
      .map((a) => {
        const t = (a.allowed_tools || []).map((x) => `<code>${escapeHtml(x)}</code>`).join(", ") || "<em>(none)</em>";
        const excerpt = (a.system_prompt || "").trim();
        return `<li><strong>${escapeHtml(a.id)}</strong><br>tools: ${t}<br><details><summary>system prompt</summary><pre class="explainer-prompt">${escapeHtml(excerpt)}</pre></details></li>`;
      })
      .join("");
    return `<h3 class="explainer-subhead">Workers</h3><ul class="agent-list">${items}</ul>`;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function bind(pickerId, bodyId) {
    const picker = document.getElementById(pickerId);
    const body = document.getElementById(bodyId);
    if (!picker || !body) return;
    function update() {
      const summary = summaries[picker.value];
      body.innerHTML = renderSummary(summary);
    }
    picker.addEventListener("change", update);
    update();
  }

  // run/eval forms have one config picker; compare has two.
  bind("config-picker", "config-explainer-body");
  bind("config-picker-b", "config-explainer-body-b");
})();
