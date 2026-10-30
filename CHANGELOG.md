# Changelog

## Unreleased — optional Render web adapter

- Add a Flask API and responsive vanilla HTML/CSS/JavaScript interface for fresh
  bundled offline investigations using the existing hunt engine and event log.
- Display actual progress, culprit, explanation, candidate verification, test
  counts, and allowlisted evidence downloads; reuse the existing HTML renderer.
- Restrict browser requests to the fixed demo, redact diagnostics, bound the
  in-process job worker/history, and sandbox the report preview.
- Add optional web dependencies, packaged static assets, focused API/integration
  tests, and a Render Blueprint with Gunicorn and `/health`.
- Keep the core algorithm, CLI, model IDs, and provider/runner integrations intact.
  Live G2 acceptance remains blocked by unavailable Nebius credentials. A Render
  deployment is not claimed until it is actually performed and verified.

## Unreleased — Phase 8 hardening

- Drain sibling probes and verification tasks after failure or cancellation before
  closing runners. Wait for blocking Git/model workers before releasing resources;
  in-flight synchronous service calls and retries may take until their configured
  timeouts.
- Remove temporary patch clones with the same Windows read-only-file handling as
  LocalRunner. Surface cleanup failures instead of silently retaining files.
- Separate URL clone caches by source URL so equally named repositories cannot
  reuse each other's history.
- Ignore inherited Git repository/index locations and disable global hooks,
  filters, and external diff helpers during host patch construction/inspection.
  Candidate verification and patch export leave the source checkout unchanged.
  URL clones skip checkout and Git templates; target code runs in the selected
  runner rather than through host checkout hooks or filters.
- Filter host credentials from Git and local setup subprocess environments. Redact
  both API key and project ID supplied through settings in diagnostics and events;
  reject either credential in Sandbox upload payloads.
- Redact argument errors and demo paths, handle configuration errors and doctor
  interruption without tracebacks, and give explicit inactive-Sandbox guidance.
- Render rebuilt reports before replacing saved evidence. Return a clear failure
  for malformed input, and emit failed completion when report writing fails.
- Add offline regression checks for these failure paths, including real local
  subprocess cancellation and timeout cleanup. Keep the reference tests unchanged.

Live verification: **G2 BLOCKED — live Nebius credentials unavailable.** The three
live demo runs and their Sandbox/model timing and cost averages have not been
measured. Offline runs and SDK/HTTP mocks do not satisfy G2.

## 0.1.0 — Phases 0–7 checkpoint

- Python CLI, deterministic 200-commit demo repository, and credential-free offline
  hunt with reproduction, scout, parallel bisect, and repeated confirmation.
- Nebius Token Factory/NVIDIA Nemotron client and Contree SDK 0.3.6 runner,
  implemented and mock-tested; live end-to-end verification remains pending.
- Isolated candidate verification, minimal passing model-fix selection, and
  regression-test/patch export.
- JSON, Markdown, self-contained HTML reports, and Rich/plain terminal views.
- Installation, architecture, evaluator walkthrough, and labelled offline report.
