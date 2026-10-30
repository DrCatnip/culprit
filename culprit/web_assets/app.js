"use strict";
const $ = (selector) => document.querySelector(selector);
const form = $("#demo-form"), button = $("#run-button"), error = $("#error");
let configuration, cursor = 0, reportUrl = null;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

async function api(url, options = {}) {
  const response = await fetch(url, {cache: "no-store", ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "The request could not complete. Please try again.");
  return data;
}

function setStage(name, status) {
  document.querySelectorAll(`[data-stage="${name}"]`).forEach((row) => {
    row.className = status;
    row.querySelector("small").textContent = {running: "In progress", done: "Finished", failed: "Failed", skipped: "Skipped"}[status] || "Waiting";
  });
}

function eventDescription(event) {
  if (event.kind === "stage") return `${event.name}: ${event.status}${event.detail ? " · " + event.detail : ""}`;
  if (event.kind === "round_finished") return `Round ${event.round}: ${event.suspects} suspects remain.`;
  if (event.kind === "round_started") return `Round ${event.round}: checking ${event.probes.length} commits.`;
  if (event.kind === "candidate_finished") return `${event.name}: ${event.passed ? "passed verification" : "did not pass"}; repro ${event.repro_code ?? "—"}, suite ${event.suite_code ?? "—"}.`;
  if (event.kind === "culprit_found") return `${event.commit.short}: ${event.commit.subject} (${event.confirmed ? "confirmed" : "unconfirmed"}).`;
  if (event.kind === "model_started") return `${event.purpose}: scripted offline response.`;
  if (event.kind === "run_finished") return event.reason;
  return event.msg;
}

function consume(events) {
  for (const event of events) {
    if (event.kind === "stage") {
      setStage(event.name, event.status);
      if (event.status === "running") $("#activity-detail").textContent = {prepare: "Preparing the isolated demo runner…", context: "Reading the bundled code context…", repro: "Checking that the regression really reproduces…", scout: "Looking for an older passing commit…", bisect: "Testing commits in parallel and narrowing the boundary…", confirm: "Repeating checks on both sides of the suspected commit…", explain: "Preparing the scripted offline explanation…", fix: "Generating scripted candidates and verifying them in isolated local clones…", report: "Writing the existing Culprit report and evidence…"}[event.name] || event.name;
    }
    const text = eventDescription(event);
    if (!text) continue;
    const row = node("li");
    row.append(node("time", new Date(event.t * 1000).toLocaleTimeString([], {hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit"})), node("span", text));
    const log = $("#event-log"); log.append(row);
    while (log.children.length > 14) log.firstElementChild.remove();
    log.scrollTop = log.scrollHeight;
  }
}

function metric(label, value) {
  const item = node("div"); item.append(node("dt", label), node("dd", value)); return item;
}

function renderResult(job) {
  const data = job.result;
  if (!data) return;
  $("#results").hidden = false;
  const commit = data.culprit?.commit || {};
  const confirmed = Boolean(data.ok && data.culprit?.confirmed);
  $("#culprit-label").textContent = confirmed ? "CULPRIT FOUND" : "INVESTIGATION RESULT";
  $("#confirmation").textContent = confirmed ? "CONFIRMED" : "INCONCLUSIVE / UNCONFIRMED";
  $("#culprit-sha").textContent = commit.short || "—";
  $("#culprit-title").textContent = commit.subject || data.reason || "No culprit identified";
  $("#culprit-meta").textContent = commit.author ? `${commit.author} · ${commit.date}` : "Review the partial report for diagnostics.";
  $("#result-bug").textContent = data.bug;
  $("#metrics").replaceChildren(metric("SEARCH ROUNDS", data.comparison?.rounds ?? "—"), metric("PROBES", data.comparison?.probes ?? "—"), metric("LOCAL OPERATIONS", data.runner?.runs ?? "—"), metric("ELAPSED", `${Number(data.seconds || 0).toFixed(1)} s`));
  const explanation = data.explanation || {}, change = explanation.guilty_change || {};
  $("#explanation-title").textContent = explanation.headline || "Explanation unavailable";
  $("#root-cause").textContent = explanation.root_cause || "The investigation stopped before explanation.";
  $("#explanation-model").textContent = explanation.model || "Offline mode · scripted model responses";
  $("#before-code").textContent = change.before || "Not recorded";
  $("#after-code").textContent = change.after || "Not recorded";
  $("#missed").textContent = explanation.why_tests_missed_it || "Not recorded";
  const fixes = data.fixes || {};
  $("#fix-summary").textContent = fixes.chosen ? `Selected: ${fixes.chosen}. Changed-line counts exclude the injected regression test.` : "No verified fix was selected. Review the candidate evidence.";
  const cards = (fixes.candidates || []).map((candidate) => {
    const card = node("article", null, "candidate-card" + (candidate.chosen ? " chosen" : ""));
    const heading = node("div", null, "candidate-header");
    heading.append(node("span", candidate.name.replaceAll("_", " "), "candidate-name"), node("span", candidate.passed ? (candidate.chosen ? "PASSED · SELECTED" : "PASSED") : "DID NOT PASS", "candidate-tag" + (candidate.passed ? "" : " fail")));
    const values = node("div", null, "candidate-values");
    [candidate.lines + " changed lines", "repro exit " + (candidate.repro_code ?? "—"), "suite exit " + (candidate.suite_code ?? "—"), job.test_counts[candidate.name] !== null && job.test_counts[candidate.name] !== undefined ? job.test_counts[candidate.name] + " suite tests passed" : "No passing test count recorded"].forEach((value) => values.append(node("span", value)));
    const details = node("details"), log = node("pre"); log.textContent = candidate.output || candidate.error || "No log output recorded.";
    details.append(node("summary", "Actual verification output"), log);
    card.append(heading, node("p", candidate.summary || candidate.error || "Candidate verification did not finish."), values, details);
    return card;
  });
  $("#candidates").replaceChildren(...cards);
  reportUrl = job.report_url;
  $("#report-link").hidden = !reportUrl;
  if (reportUrl) $("#report-link").href = reportUrl;
  $("#report-preview").hidden = !reportUrl;
  const labels = {"fix.patch": "Download patch", "test_culprit_repro.py": "Regression test", "run.json": "Run JSON", "report.md": "Markdown report", "report.html": "Download HTML"};
  const links = Object.entries(job.artifacts).map(([name, url]) => { const link = node("a", labels[name] || name); link.href = url; link.download = name; return link; });
  $("#artifact-links").replaceChildren(...links);
}

async function poll(url) {
  while (true) {
    const job = await api(`${url}?after=${cursor}`);
    cursor = job.cursor; consume(job.events);
    if (job.status !== "running") {
      renderResult(job);
      $("#job-status").textContent = job.status === "complete" ? "COMPLETE · OFFLINE" : "STOPPED";
      $("#activity-detail").textContent = job.status === "complete" ? "The investigation finished. Explore the actual result and verification evidence below." : (job.error || "The investigation stopped. Review the available evidence.");
      if (job.status === "failed") { error.textContent = job.error || "Investigation did not complete."; error.hidden = false; }
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault(); if (!configuration || button.disabled) return;
  button.disabled = true; button.firstElementChild.textContent = "Investigation running…";
  error.hidden = true; $("#results").hidden = true; $("#empty-state").hidden = true;
  $("#event-log").hidden = false; $("#event-log").replaceChildren();
  $("#report-preview").open = false; $("#report-frame").removeAttribute("src");
  document.querySelectorAll(".timeline li").forEach((row) => { row.className = ""; row.querySelector("small").textContent = "Waiting"; });
  cursor = 0; $("#job-status").textContent = "RUNNING · OFFLINE";
  $("#activity-detail").textContent = "Starting the real bundled investigation…";
  try {
    const job = await api("/api/demo", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({mode: "offline", repository: configuration.repository, bug: configuration.bug})});
    await poll(job.status_url);
  } catch (failure) {
    error.textContent = `${failure.message} Click Run Demo Investigation to reconnect to an active demo or start again.`; error.hidden = false;
    $("#job-status").textContent = "CONNECTION / REQUEST ERROR";
    $("#activity-detail").textContent = "No successful result is assumed. Existing server work may still be running.";
  } finally { button.disabled = false; button.firstElementChild.textContent = "Run Demo Investigation"; }
});

$("#report-preview").addEventListener("toggle", () => { if ($("#report-preview").open && reportUrl) $("#report-frame").src = reportUrl; });

api("/api/demo").then((data) => {
  configuration = data; $("#repository").value = data.repository; $("#bug").value = data.bug; button.disabled = false;
}).catch((failure) => { error.textContent = `Could not load demo configuration: ${failure.message} Reload this page to retry.`; error.hidden = false; });
