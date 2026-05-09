// Live job page. Three jobs:
//   1. Tick the elapsed-time counter every 250ms from the server-supplied
//      started_at timestamp (the source of truth — clocks drift).
//   2. Subscribe to /jobs/{id}/events (SSE) and update step rows + the
//      event log as phase events arrive.
//   3. When the SSE stream closes with `event: done`, redirect to the
//      job's redirect_url (trajectory or report) after a 3s pause.
//
// Vanilla JS, no framework. Runs on every modern browser.

(function () {
  "use strict";

  const jobIdMatch = window.location.pathname.match(/\/jobs\/([^/]+)/);
  if (!jobIdMatch) return;
  const jobId = jobIdMatch[1];

  // --- timer -------------------------------------------------------------
  const timerEl = document.getElementById("job-timer");
  const startedAt = timerEl && timerEl.dataset.startedAt
    ? Date.parse(timerEl.dataset.startedAt)
    : null;
  let timerStop = null;
  let frozenAt = null;

  function fmtElapsed(ms) {
    if (ms < 0) ms = 0;
    const total = Math.floor(ms / 1000);
    const mins = Math.floor(total / 60);
    const secs = total % 60;
    const tenths = Math.floor((ms % 1000) / 100);
    return `${mins}:${String(secs).padStart(2, "0")}.${tenths}`;
  }
  function tickTimer() {
    if (!timerEl) return;
    if (frozenAt) {
      timerEl.textContent = fmtElapsed(frozenAt);
      return;
    }
    const ref = startedAt ?? Date.now();
    timerEl.textContent = fmtElapsed(Date.now() - ref);
  }
  if (timerEl) {
    tickTimer();
    timerStop = setInterval(tickTimer, 250);
  }

  // --- step list + event log --------------------------------------------
  const stepList = document.getElementById("step-list");
  const eventLog = document.getElementById("event-log");
  const statusEl = document.getElementById("job-status");
  const progressEl = document.getElementById("job-progress");
  const totalSteps = stepList ? stepList.children.length : 0;

  function setStepStatus(stepId, status, detail) {
    if (!stepList) return;
    const row = stepList.querySelector(`[data-step-id="${cssEscape(stepId)}"]`);
    if (!row) return;
    row.classList.remove(
      "step--pending",
      "step--running",
      "step--done",
      "step--error",
    );
    row.classList.add(`step--${status}`);
    if (detail) {
      const detailEl = row.querySelector(".step-detail");
      if (detailEl) detailEl.textContent = detail;
    }
  }

  function updateProgress() {
    if (!stepList || !progressEl) return;
    const done = stepList.querySelectorAll(".step--done").length;
    progressEl.textContent = `${done} / ${totalSteps}`;
  }

  function appendLogLine(event) {
    if (!eventLog) return;
    const li = document.createElement("li");
    li.className = `log-entry log-entry--${event.status}`;
    const ts = event.timestamp
      ? new Date(event.timestamp).toLocaleTimeString()
      : "";
    li.innerHTML = `
      <code class="log-time">${escapeHtml(ts)}</code>
      <span class="log-step">${escapeHtml(event.step)}</span>
      <span class="log-status status-${event.status}">${escapeHtml(event.status)}</span>
      ${event.detail ? `<span class="log-detail">${escapeHtml(event.detail)}</span>` : ""}
    `;
    eventLog.appendChild(li);
    eventLog.scrollTop = eventLog.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function cssEscape(s) {
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  // --- SSE subscription --------------------------------------------------
  const source = new EventSource(`/jobs/${encodeURIComponent(jobId)}/events`);

  source.addEventListener("message", (e) => {
    let event;
    try {
      event = JSON.parse(e.data);
    } catch (err) {
      console.warn("bad SSE payload", err, e.data);
      return;
    }
    if (event.step === "__job__") return; // job-level meta event; ignore
    setStepStatus(event.step, event.status, event.detail || "");
    appendLogLine(event);
    updateProgress();
  });

  source.addEventListener("done", (e) => {
    let snap;
    try {
      snap = JSON.parse(e.data);
    } catch (err) {
      console.warn("bad done payload", err, e.data);
      snap = {};
    }
    finalizePage(snap);
    source.close();
  });

  source.addEventListener("error", () => {
    // EventSource auto-retries; surface only after explicit job termination.
    // No-op here keeps the UI stable on transient network blips.
  });

  function finalizePage(snap) {
    if (statusEl) {
      statusEl.textContent = snap.status || "done";
      statusEl.dataset.status = snap.status || "done";
    }
    if (timerStop) clearInterval(timerStop);
    if (snap.started_at && snap.finished_at) {
      frozenAt = Date.parse(snap.finished_at) - Date.parse(snap.started_at);
      tickTimer();
    }
    if (snap.status === "error") {
      const errSection = document.getElementById("job-error");
      const errDetail = document.getElementById("job-error-detail");
      if (errSection) errSection.hidden = false;
      if (errDetail) errDetail.textContent = snap.error || "Unknown error";
      return;
    }
    if (snap.redirect_url) {
      const notice = document.getElementById("job-redirect-notice");
      const link = document.getElementById("job-redirect-link");
      const countdown = document.getElementById("job-redirect-countdown");
      if (notice) notice.hidden = false;
      if (link) {
        link.href = snap.redirect_url;
        link.textContent = snap.redirect_url;
      }
      let n = 3;
      if (countdown) countdown.textContent = String(n);
      const cd = setInterval(() => {
        n -= 1;
        if (countdown) countdown.textContent = String(n);
        if (n <= 0) {
          clearInterval(cd);
          window.location.href = snap.redirect_url;
        }
      }, 1000);
    }
  }
})();
