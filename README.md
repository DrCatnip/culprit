# Culprit

**Describe a bug. Get the commit, the reason, and a tested fix.**

Culprit turns a plain-English bug report into a reproducible pytest test, searches Git history in parallel, explains the guilty change, and checks candidate fixes against both the reproduction and the repository's suite. NVIDIA Nemotron models through Nebius Token Factory handle code selection, tests, explanations, and fixes; Nebius Sandboxes provide isolated execution. The result is a shareable HTML report, a regression test, and a patch when a candidate passes.

[View the offline example report](docs/index.html) · [Architecture](docs/ARCHITECTURE.md) · [MIT license](LICENSE)

**Verification status:** the implementation has 165 passing tests and a working offline workflow. **G2 remains BLOCKED only because live Nebius credentials are unavailable.** Live model quality, Sandbox execution, cloud latency, and cloud cost have not been verified. The example report uses an **offline stand-in**, not real model responses. Public GitHub, YouTube, and hosted-demo links are pending publication.

## Why

“It worked last month” usually means writing a failing test, running `git bisect`, reading the diff, and fixing the regression by hand. Culprit connects those steps and tests several commits at once. Its evidence distinguishes a reproduced bug from old code that cannot run the test, and distinguishes a passing fix from a plausible suggestion.

## Quick start: no credentials

### Interactive browser demo

The optional Flask adapter runs the existing offline engine and serves its real
progress, result, report, and downloadable evidence. Install and start it locally:

```bash
python -m pip install -e ".[dev,web]"
python -m culprit.web
```

Open **http://127.0.0.1:5000**, then click **Run Demo Investigation**. The server
performs a fresh hunt; it does not replay a saved result. Repository and bug inputs
are fixed to the bundled example because offline model replies are scripted for it.
The public adapter accepts no arbitrary paths, shell commands, uploads, repositories,
or live credentials. Existing CLI commands continue to work independently.

**Render:** create a Python Web Service from this repository using `render.yaml`,
or enter these settings manually. The repository root contains `pyproject.toml`;
leave Render's Root Directory blank for the GitHub repository.

- Build: `pip install -e ".[dev,web]" && culprit demo-repo --force`
- Start: `gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT 'culprit.web:create_app()'`
- Health check: `/health`
- Environment: `PYTHON_VERSION=3.12.10`, `CULPRIT_WEB_PARALLEL=2`; Render supplies `PORT`.
- No `.env`, API credentials, GPU, database, or frontend build pipeline is required.

The single worker holds job state in memory and runs one investigation at a time.
Concurrent visitors join the active demo. Two local probes run at once by default
(the CLI default is unchanged); `CULPRIT_WEB_PARALLEL` is limited to 1–4. Five jobs
are retained. Artifacts and jobs are ephemeral: restarting or redeploying loses
them, and the browser can start again. A small/free instance can take longer than
the local CLI and can have a cold start. Do not enable multiple Gunicorn workers
or instance autoscaling for this in-process adapter.

Routes: `GET /`, `GET /health`, `GET /api/demo` (configuration), `POST /api/demo`
(start/join), `GET /api/demo/<id>` (events/result), `GET /reports/<id>` (the existing
HTML report), and `GET /api/demo/<id>/artifacts/<filename>` (allowlisted evidence).

This is an interactive **offline demonstration**: scripted model replies and real
local Git/pytest execution. Live Nebius acceptance remains pending; **G2 is BLOCKED**.
Deployment requires publishing these changes and connecting Render; no public
Render URL or successful deployment is claimed here.

### CLI demo

Requirements: **Python 3.11+**, Git, and `sh` and `tar` for local execution. Use macOS, Linux, WSL, or **Git Bash on Windows**. Installation downloads Python dependencies; the bundled offline hunt itself needs no network or credentials. Run commands in the checkout directory containing `pyproject.toml` (in the supplied workspace, that is `culprit/`).

macOS / Linux / WSL:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
culprit demo --offline --plain
```

Windows: install Git for Windows and **native Python 3.12 with the `py` launcher**, then open **Git Bash** in the checkout. Select native Python explicitly so an MSYS2 `python` earlier on PATH cannot create an incompatible environment layout:

```bash
py -3.12 -m venv .venv
source .venv/Scripts/activate
python -m pip install -e ".[dev]"
culprit demo --offline --plain
```

Keep that environment active for subsequent commands. A native PowerShell terminal also needs Git Bash's `sh` and `tar` on its PATH; the Git Bash instructions above avoid that extra setup. The offline command creates the deterministic demo repository and `demo/BUG.txt` if needed. To supply that bug explicitly:

```bash
culprit hunt --repo demo/kirana-billing --bug-file demo/BUG.txt --offline --plain
```

Expected final output, abbreviated to omit the generated SHA and run path:

```text
Culprit: … refactor(money): keep amounts in integer paise
Paise refactor switched to Python round(), which rounds .5 to even
Chosen fix: minimal
Report: runs/…/report.html
```

For a predictable output directory and the Rich live view, omit `--plain` in an interactive terminal:

```bash
culprit demo --offline --out runs/presentation
culprit report runs/presentation
```

Open `runs/presentation/report.html` directly in a browser. The report command rebuilds saved artifacts without executing the repository or calling a model. Redirected output automatically uses the plain event printer.

## How it works

1. **Prepare and reproduce:** read history, let Nano select context, and have Super write a test. Require failure on the bad revision and success on older code.
2. **Scout → bisect:** scout exponentially older commits to find a good boundary, then probe up to eight commits per round. PASS, FAIL, and SKIP are kept separate.
3. **Confirm → explain:** repeat both sides of the boundary twice. Ultra explains the diff; Super is the explicit fallback if explanation generation fails. Flaky or ambiguous results carry warnings.
4. **Fix → verify:** Super proposes minimal and robust fixes. Compare both and a Git revert against an isolated baseline, the reproduction, and the full suite. Retry the minimal fix once if neither model fix passes.
5. **Choose and report:** prefer the smallest passing model fix, excluding regression-test lines from its size; otherwise choose a passing revert, or no patch. Export the evidence and suggested patch for human review.

The reproduction must pass. The suite must pass or introduce no new failures relative to the baseline, ignoring the injected regression-test ID. Infrastructure and collection errors cannot pass by an empty failure comparison. A successful hunt can still produce **no verified fix**.

## The 200-commit demo

`kirana-billing` models a grocery billing library. Fixed files, authors, dates, and tags produce the same 200-commit history. Commit #112 replaces Decimal half-up rounding with Python's ties-to-even `round()`: a ₹42.50 bill becomes ₹42 instead of ₹43. Existing tests miss that even-rupee half case. Commit #118 adds export code depending on `to_paise()`, so reverting #112 breaks `tests/test_export.py` while the targeted fix preserves that API. The repository's own suite passes at every original commit.

Offline mode uses scripted answers only for this bundled repository and real local Git/pytest execution in temporary clones. It is not a general-purpose offline model.

One recorded verification on **2026-10-28, Windows/Git Bash, Python 3.12.10** took **24.688 seconds**, with **2 rounds after scout, 33 local runner operations, and $0 model API cost**. It selected the 3-line minimal fix; the robust fix also passed, and the revert failed collection. This single offline observation is reproduced in the example report; it is not a cloud benchmark or a measured Sandbox speedup.

## Which model does what

| Role | Default model ID | Job |
| --- | --- | --- |
| Nemotron 3 Nano | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | Select relevant files |
| Nemotron 3 Super | `nvidia/nemotron-3-super-120b-a12b` | Write the reproduction and candidate fixes; explanation fallback |
| Nemotron 3 Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | Explain the guilty change |

### How we use Nebius Token Factory

The OpenAI-compatible Python client is configured for `https://api.tokenfactory.nebius.com/v1/`. All live model calls use NVIDIA Nemotron through Nebius. Replies request JSON, with a bounded parsing retry and a fallback when JSON response mode is unsupported. Usage tracks calls, input/output tokens, elapsed model time, and estimated cost per role. Offline labels and zero API usage are displayed separately. Model IDs are case-sensitive; `doctor` checks the configured IDs against the live catalog rather than assuming availability.

### How we use Nebius Sandboxes

The default `contree` runner uses **contree-sdk 0.3.6** against Nebius Sandboxes. It uploads a Git bundle and setup/probe/verify scripts, installs dependencies in a `python:3.12-slim` image, and retains the prepared state as a checkpoint. Each probe or verification branches from it with `disposable=True`; a semaphore allows eight at a time by default, configurable from 1 to 40. Host credentials stay in SDK authentication, not guest environments. A Sandbox failure does **not** silently switch to local execution. SDK-reported cost has an unspecified unit, so it is not presented as USD billing. The SDK has no image-deletion method; checkpoint retention is reported honestly.

## Configure live mode when credentials are available

Copy the provided example locally; edit the copy in your editor. Do not paste credentials into shell history, chat, screenshots, reports, or Git.

```bash
cp .env.example .env
```

Set `NEBIUS_API_KEY` and `NEBIUS_PROJECT_ID` to your own values. `NEBIUS_AI_PROJECT` is the project variable used by the separate Contree CLI; the Python SDK uses `NEBIUS_PROJECT_ID`. Process environment variables take precedence over `.env` in the current directory. `.env.example` contains placeholders only. Model, image, timeout, retry, and parallelism overrides are documented there; `TOKEN_FACTORY_BASE_URL` must remain the supported Nebius endpoint.

The following commands make **live service calls** and require credentials and project access. They are documented for future use; G2 has not passed:

```bash
culprit doctor
culprit demo --plain
```

`doctor` checks Python, Git, credential presence (never values), configured Nemotron IDs with case-sensitive matching/closest suggestions, a tiny Nano JSON call, and Sandbox access. Missing credentials produce a clear failure; doctor success alone does not establish an end-to-end G2 pass.

To use real models with trusted local execution of the bundled demo:

```bash
culprit hunt --repo demo/kirana-billing --bug-file demo/BUG.txt --runner local --plain
```

That command still calls Nebius Token Factory and can incur model charges. Other repositories require `--i-trust-this-repo` for local execution; local clones are **not** a security sandbox. By default, `culprit hunt --repo PATH_OR_URL --bug "description"` uses Contree.

## CLI reference

| Command | Options |
| --- | --- |
| `culprit doctor` | Configuration, model and Sandbox checks |
| `culprit demo-repo` | `--path PATH`, `--force` (replace a marked generated demo) |
| `culprit demo` | `--runner contree\|local`, `--offline`, `--good REF`, `--parallel N`, `--plain`, `--out DIR` |
| `culprit hunt --repo PATH_OR_URL --bug TEXT` | Use exactly one of `--bug TEXT` / `--bug-file FILE`; all demo options plus `--bad REF`, `--i-trust-this-repo`, `--no-fix` |
| `culprit report RUN_DIR` | Rebuild artifacts from `RUN_DIR/run.json` |

Default bad revision: `HEAD`. Exit codes: **0** culprit found, **1** error, **2** not found/inconclusive. Report rebuilding returns 0 on successful rebuilding, including partial-run reports. Each command supports `--help`.

Run folders contain `run.json`, `events.jsonl`, self-contained `report.html`, short-form `report.md`, `test_culprit_repro.py`, and `fix.patch` only when a verified candidate is selected. Reports show reproduction attempts, scout and round replay, confirmation, explanation, candidate results and failure tails, model usage, runner timing, and warnings. The Rich view listens to events with a lock and refreshes about eight times per second; `--plain` emits one JSON-bearing line per event.

## Two-minute-fifty-second walkthrough

| Time | Show and say |
| --- | --- |
| 0:00–0:20 | Show `demo/BUG.txt`: ₹42.50 became ₹42 across 200 commits. Say explicitly: **this is the offline stand-in; Git and tests run locally**. |
| 0:20–0:50 | Run `culprit demo --offline --out runs/presentation`. Explain that live mode assigns Nano context selection and Super reproduction; this run uses scripted answers. |
| 0:50–1:20 | Follow scout and the shrinking history strip. Explain parallel probes and SKIP for untestable old code. Label any accelerated waiting footage. |
| 1:20–1:45 | Show the confirmed refactor and explanation: `round(42.5)` rounds to even, while the old Decimal code rounded half up. |
| 1:45–2:15 | Open `runs/presentation/report.html`. Compare minimal, robust, and revert; show the export import error and chosen patch. |
| 2:15–2:35 | Show replay, the regression test, and usage. Say zero API cost belongs to offline mode; live numbers are not available. |
| 2:35–2:50 | Point to setup, architecture, and MIT license. State that G2 awaits live Nebius credentials. |

## Security and limitations

- Python repositories tested with pytest only; repositories needing databases or external services may not work in the default image. Local mode relies on the active Python environment and does not provision a separate dependency environment for every historical commit.
- First-parent history only: a regression introduced through a merge is attributed to that merge. Flaky/non-monotonic histories and skipped commits can leave uncertainty.
- A useful reproduction is required. Offline success establishes orchestration and local verification, not live model quality or cloud reliability.
- Model paths are validated, regression-test overwrites are dropped, and tracked symlinks/case aliases are rejected before fix uploads. Exported patches are built using Git in temporary clones; they are not automatically applied to your source checkout.
- Credential values, sensitive fields, Authorization headers, and URL userinfo are redacted from events/reports. Known credential variables are removed from local execution environments. Still review repository contents before upload and inspect reports before sharing; a Git bundle includes history.
- Sandboxes are a beta service. Parallelism is capped at 40 in Culprit; actual access, quotas, latency, retention, and billing need live confirmation. Reported summed-run/wall-time ratios are estimates, not measured serial-vs-parallel benchmarks.
- A verified patch remains a suggestion for human review. No live model/Sandbox performance or cost result is claimed; **G2 remains blocked solely by unavailable live credentials**.
- Ctrl+C drains parallel operations, closes the runner, and writes a partial report. An in-flight synchronous model/Git call must finish before its client or temporary files can be released; requests and their retries can delay cancellation until their configured timeouts. Filesystem failures are reported explicitly and can prevent report creation or cleanup.

Phase 8's three live demo measurements remain pending: **G2 BLOCKED — live Nebius credentials unavailable.** No live timing, Sandbox-run, or model-cost averages are published. See the [changelog](CHANGELOG.md) for the offline-tested hardening changes.

## Development and demo-page publication

With the development extra installed:

```bash
pytest -q
culprit demo-repo --force
python scripts/check_demo_repo.py
```

The last command checks the generated repository's own suite at all 200 commits. Generated `demo/` and `runs/`, environments, and caches remain ignored.

`docs/index.html` is a sanitized copy of a recorded offline report, with publication placeholders. It can be opened locally without a server. No public URL is claimed. When a public GitHub repository and video exist, replace the labelled placeholders, then configure GitHub **Settings → Pages → Deploy from a branch → main → /docs**, following [GitHub's publishing-source guide](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site). `docs/.nojekyll` is included. Keep the evaluator demo/test build available through **15 December 2026**. Replace the example with a reviewed real run only after credentials and live verification are available; retain honest provenance.

## Team and license

Built for the Nebius × NVIDIA hackathon, **Coding & Agentic Engineering** track, by **Panshul, Pratyush, Sourin, Akshat, and Khushi**. Licensed under the [MIT License](LICENSE), copyright 2026 the project team.
