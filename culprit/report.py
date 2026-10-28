"""Deterministic, self-contained reports for successful and partial hunts."""
from __future__ import annotations

import html
import json
from pathlib import Path
from culprit.security import redact_data

CSS = """
:root{--pass:#0ca30c;--fail:#d03b3b;--skip:#eda100;--untested:#d3dae3;--accent:#1baf7a;--ink:#17202e;--paper:#f5f7f9}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.6 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1120px;margin:auto;padding:36px 24px 64px}header{border-top:5px solid var(--accent);padding:26px 0}h1{font-size:38px;letter-spacing:-1px;line-height:1.2;margin:12px 0}h2{font-size:23px;margin:0 0 16px}h3{font-size:17px;margin:18px 0 8px}p{margin:8px 0 16px;overflow-wrap:anywhere}section{background:white;border:1px solid #dfe4e9;border-radius:12px;padding:26px;margin:0 0 22px;break-inside:avoid}code,pre{font:13px/1.6 ui-monospace,SFMono-Regular,Consolas,monospace}pre{background:#f5f7f9;border:1px solid #e0e5eb;border-radius:6px;padding:16px;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;margin:8px 0}small,.muted{color:#4d5b6b}.eyebrow{text-transform:uppercase;letter-spacing:2px;font-size:12px;font-weight:700}.lead{font-size:21px}.badge{display:inline-block;border:1px solid currentColor;border-radius:20px;padding:2px 10px;font-size:12px;font-weight:650}.confirmed{color:#087b08}.flaky,.error{color:#a22626}.notice{border-left:4px solid var(--accent);padding:12px 16px;background:#eaf9f3}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.grid>*{min-width:0}.table-wrap{overflow-x:auto;max-width:100%}table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;background:#f5f7f9}td,th{padding:10px;border-bottom:1px solid #e0e5eb;vertical-align:top;overflow-wrap:anywhere}tr.chosen{background:#eaf9f3}details{margin:12px 0}summary{cursor:pointer;font-weight:600}.strip{display:flex;flex-wrap:wrap;gap:3px;margin:12px 0}.cell{width:8px;height:22px;border-radius:2px;background:var(--untested)}.cell.pass{background:var(--pass)}.cell.fail{background:var(--fail)}.cell.skip{background:var(--skip)}.cell.dim{opacity:.24}.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px}.legend .cell{display:inline-block;vertical-align:middle;width:8px;height:12px;margin-right:4px}.round{border-top:1px solid #e0e5eb;padding:16px 0}.diff .add{background:#e6f8e6;display:block}.diff .del{background:#fdeaea;display:block}.diff .meta{color:#466180;display:block}ul{padding-left:20px}li{overflow-wrap:anywhere}.metrics{display:flex;gap:24px;flex-wrap:wrap}.metrics b{font-size:24px;display:block}footer{color:#4d5b6b;font-size:13px}
@media(max-width:600px){main{padding:16px 12px 32px}section{padding:18px 14px;border-radius:8px}h1{font-size:30px}.lead{font-size:18px}.grid{grid-template-columns:1fr}td,th{padding:8px 6px;overflow-wrap:normal}table{min-width:600px}.table-wrap:after{content:"Scroll the table horizontally to see all columns.";display:block;font-size:12px;color:#4d5b6b;margin-top:8px}pre{padding:12px}.cell{width:6px;height:18px}.metrics{gap:16px}}
@media print{body{background:white}main{padding:0;max-width:none}section{border-radius:0;break-inside:auto}details>*{display:block}.table-wrap{overflow:visible}header{padding-top:0}*{print-color-adjust:exact}}
"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else "—"), quote=True)


def code(value, css="") -> str:
    return f'<pre class="{css}"><code>{esc(value)}</code></pre>'


def table(headers, rows) -> str:
    return '<div class="table-wrap" tabindex="0"><table><thead><tr>' + ''.join(
        f'<th scope="col">{esc(h)}</th>' for h in headers
    ) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in row) + '</tr>' for row in rows
    ) + '</tbody></table></div>'


def section(title, body) -> str:
    return f'<section><h2>{esc(title)}</h2>{body}</section>'


def replay(data: dict) -> str:
    commits = data.get("range", [])
    n, states, rows = len(commits), {}, []
    by_short = {c.get("short"): i for i, c in enumerate(commits)}

    def strip(lo, hi):
        cells = []
        for i, commit in enumerate(commits):
            status = states.get(i, "untested")
            status = status if status in ("pass", "fail", "skip") else "untested"
            dim = " dim" if i <= lo or i > hi else ""
            title = f"{i + 1} · {commit.get('short', '')} · {status} · {commit.get('subject', '')}"
            cells.append(f'<span class="cell {status}{dim}" title="{esc(title)}" aria-label="{esc(title)}"></span>')
        return '<div class="strip" role="img" aria-label="Commit results, oldest to newest">' + ''.join(cells) + '</div>'

    scout = data.get("scout", [])
    if scout:
        for p in scout:
            for short, i in by_short.items():
                if short and p.get("short", "").startswith(short):
                    states[i] = p.get("status")
        notes = ''.join(f'<li>{esc(p.get("short"))}: {esc(p.get("status"))} — {esc(p.get("reason"))}</li>' for p in scout)
        boundary = data.get("repo", {}).get("good", {}).get("short")
        rows.append(f'<div class="round" data-round="0"><strong>Scout · round 0</strong><p>Tested {len(scout)} commits at once; good boundary: {esc(boundary)}.</p>' + strip(-1, n - 1) + '<details><summary>Scout results</summary><ul>' + notes + '</ul></details></div>')
    for r in data.get("rounds", []):
        for p in r.get("probes", []):
            states[p["i"]] = p.get("status")
        notes = ''.join(f'<li>{esc(p.get("short"))}: {esc(p.get("status"))} — {esc(p.get("reason"))} ({esc(p.get("seconds"))} s)</li>' for p in r.get("probes", []))
        rows.append(f'<div class="round" data-round="{esc(r.get("n"))}"><strong>Round {esc(r.get("n"))}</strong><p>Tested {len(r.get("probes", []))} commits at once → {r.get("hi_after", 0) - r.get("lo_after", -1)} suspects left.</p>' + strip(r.get("lo_after", -1), r.get("hi_after", n - 1)) + '<details><summary>Probe results</summary><ul>' + notes + '</ul></details></div>')
    legend = '<div class="legend">' + ''.join(f'<span><i class="cell {s}"></i>{s}</span>' for s in ("pass", "fail", "skip", "untested")) + '<span>Faded: ruled out · oldest → newest</span></div>'
    c = data.get("comparison", {})
    return legend + ''.join(rows) + f'<p>git bisect would need about {esc(c.get("binary_rounds", 0))} rounds; Culprit needed {esc(c.get("rounds", 0))} after scout.</p>'


def render_html(data: dict) -> str:
    repo, runner, culprit = (data.get(k, {}) for k in ("repo", "runner", "culprit"))
    commit = culprit.get("commit", {})
    explanation, fixes, repro = (data.get(k, {}) for k in ("explanation", "fixes", "repro"))
    offline = any(m.get("model") == "offline-script" for m in data.get("models", {}).values())
    mode = '<p class="notice">Offline stand-in: scripted answers, local execution, no model API calls.</p>' if offline else ''
    result = (f'Found the commit that broke it in {data.get("comparison", {}).get("rounds", 0)} rounds '
              f'({runner.get("runs", 0)} {"local" if "local" in runner.get("name", "").lower() else "sandbox"} runs, {data.get("seconds", 0):.1f} s).'
              if data.get("ok") else f'Hunt incomplete: {data.get("reason", "No result recorded.")}')
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Culprit report</title><style>' + CSS + '</style></head><body><main>',
             f'<header><div class="eyebrow">{esc(repo.get("name", "Repository"))} · {esc(str(data.get("started_at", ""))[:10])}</div><h1>Culprit report</h1><p class="lead">{esc(result)}</p>{mode}</header>',
             section("The bug report", f'<p>{esc(data.get("bug", ""))}</p>')]
    badge = "confirmed" if culprit.get("confirmed") else "flaky" if commit else "not identified"
    parts.append(section("Culprit", f'<span class="badge {"confirmed" if culprit.get("confirmed") else "flaky"}">{badge}</span><h3><code>{esc(commit.get("short", "—"))}</code> {esc(commit.get("subject", "Not identified"))}</h3><p>{esc(commit.get("author", ""))} · {esc(commit.get("date", ""))}</p><p>Files: {esc(", ".join(culprit.get("files", [])))}</p><p>Ambiguous commits: {esc(", ".join(culprit.get("ambiguous", [])) or "none")}</p><details><summary>Confirmation output</summary>' + code(culprit.get("output", "No confirmation recorded.")) + '</details>'))
    change = explanation.get("guilty_change", {})
    parts.append(section("Why it broke", f'<p class="muted">Explained by {esc(explanation.get("model", "unavailable"))} · Confidence: {esc(explanation.get("confidence", "unavailable"))}</p><h3>{esc(explanation.get("headline", "Explanation unavailable"))}</h3><p>{esc(explanation.get("root_cause", ""))}</p><p>{esc(change.get("file", ""))}</p><div class="grid"><div><h3>Before</h3>{code(change.get("before", ""))}</div><div><h3>After</h3>{code(change.get("after", ""))}</div></div><h3>Why tests missed it</h3><p>{esc(explanation.get("why_tests_missed_it", ""))}</p><h3>Fix idea</h3><p>{esc(explanation.get("fix_idea", ""))}</p>'))
    attempts = [[a.get(k, "—") for k in ("n", "bad_status", "good_status", "bad_reason", "good_reason", "problem")] for a in repro.get("attempts", [])]
    reproduction = f'<h3>Reproduction validation</h3><p>{esc(repro.get("why", "No reproduction recorded."))}</p>' + table(["Attempt", "Bad", "Good", "Bad reason", "Good reason", "Problem"], attempts)
    parts.append(section("Hunt replay", reproduction + replay(data)))
    candidates, logs = [], []
    for c in fixes.get("candidates", []):
        candidates.append('<tr' + (' class="chosen"' if c.get("chosen") else '') + '>' + ''.join(f'<td>{esc(v)}</td>' for v in (c.get("name"), c.get("model"), c.get("repro_code"), c.get("suite_code"), c.get("lines"), "passed" if c.get("passed") else "failed", "chosen" if c.get("chosen") else "—")) + '</tr>')
        tail = '\n'.join(c.get("output", "").splitlines()[-30:])
        logs.append(f'<details><summary>{esc(c.get("name"))}: {esc(c.get("summary", ""))}</summary><p class="error">{esc(c.get("error") or "")}</p>' + code(tail or "No log output recorded.") + '</details>')
    candidate_table = '<div class="table-wrap" tabindex="0"><table><thead><tr>' + ''.join(f'<th scope="col">{h}</th>' for h in ("Candidate", "Model", "Repro", "Suite", "Lines", "Result", "Selected")) + '</tr></thead><tbody>' + ''.join(candidates) + '</tbody></table></div>'
    selected = next((c for c in fixes.get("candidates", []) if c.get("chosen") and c.get("passed")), None)
    diff = ''
    if selected:
        lines = []
        for line in (selected.get("patch") or "").splitlines():
            style = "add" if line.startswith("+") and not line.startswith("+++") else "del" if line.startswith("-") and not line.startswith("---") else "meta"
            lines.append(f'<span class="{style}">{esc(line)}\n</span>')
        diff = '<h3>Selected patch · ' + esc(selected.get("name")) + '</h3><pre class="diff"><code>' + ''.join(lines) + '</code></pre>'
    baseline = fixes.get("baseline", {})
    parts.append(section("Fix candidates", f'<p>Selected fix: <strong>{esc(fixes.get("chosen") or "none")}</strong>. Baseline suite exit: {esc(baseline.get("suite_code"))}.</p><p>Baseline failures: {esc(", ".join(baseline.get("failures", [])) or "none recorded")}. {esc(baseline.get("error") or "")}</p>' + candidate_table + ''.join(logs) + (diff or '<p>No verified patch selected. Fix generation may have been skipped or failed.</p>')))
    parts.append(section("Regression test", code(repro.get("test_code", "No test recorded."))))
    model_rows = []
    for role, m in data.get("models", {}).items():
        u = next((u for u in data.get("usage", []) if u.get("role") == role), {})
        model_rows.append((role, m.get("label"), m.get("model"), u.get("calls", 0), f'{u.get("prompt_tokens", 0)} / {u.get("completion_tokens", 0)}', f'{u.get("seconds", 0):.2f} s', f'${u.get("cost_usd", 0):.5f}'))
    parts.append(section("Models", table(["Role", "Label", "Model", "Calls", "Tokens in / out", "Time", "Cost USD"], model_rows) + f'<p>Total model cost: <strong>${data.get("llm_cost_usd", 0):.5f}</strong>.</p>'))
    wall, summed = data.get("seconds", 0), runner.get("sandbox_seconds", 0)
    ratio = f'{summed / wall:.2f}×' if wall else 'unavailable'
    parts.append(section("Runner and timing", f'<p>{esc(runner.get("name", "Not started"))}</p><div class="metrics"><div><b>{esc(runner.get("runs", 0))}</b>runs</div><div><b>{esc(runner.get("parallel", 0))}</b>at once</div><div><b>{wall:.1f} s</b>wall time</div><div><b>{summed:.1f} s</b>summed execution time</div></div><p>Serial execution estimate: {ratio} the whole hunt wall time (sum of run times / wall time; includes different work and is not a measured speedup).</p><p>Errors: {esc(runner.get("errors", 0))} · Checkpoint: {esc(runner.get("checkpoint"))} · API-reported cost: {esc(runner.get("reported_cost", 0))} (unit unspecified).</p><p>Started: {esc(data.get("started_at"))}<br>Finished: {esc(data.get("finished_at"))}</p>'))
    parts.append(section("Warnings and limits", '<ul>' + ''.join(f'<li>{esc(w)}</li>' for w in data.get("warnings", [])) + '</ul><p>Python/pytest repositories; first-parent history. Flaky bugs and skipped commits can leave ambiguity. Repositories requiring external services may not run. Review every suggested patch before merging.</p>'))
    return ''.join(parts) + '<footer>Culprit · Evidence, explanation, and a verified fix.</footer></main></body></html>'


def render_markdown(data: dict) -> str:
    """The same evidence in short, readable form for issues and pull requests."""
    culprit = data.get("culprit", {})
    commit = culprit.get("commit", {})
    explanation = data.get("explanation", {})
    change = explanation.get("guilty_change", {})
    fixes = data.get("fixes", {})
    runner = data.get("runner", {})
    comparison = data.get("comparison", {})
    parts = ["# Culprit report", f'{data.get("repo", {}).get("name", "")} · {data.get("started_at", "")}',
             "Found " + commit.get("short", "") + ": " + commit.get("subject", "") if data.get("ok") else "Hunt incomplete: " + data.get("reason", ""),
             "Offline stand-in: scripted answers, local execution, no model API calls." if any(m.get("model") == "offline-script" for m in data.get("models", {}).values()) else "",
             "## Bug", data.get("bug", ""), "## Culprit",
             f'{commit.get("short", "—")} {commit.get("subject", "Not identified")} · {commit.get("author", "")} · {commit.get("date", "")}',
             f'Confirmed: {culprit.get("confirmed", False)}. Ambiguous: {", ".join(culprit.get("ambiguous", [])) or "none"}.',
             "Files: " + ", ".join(culprit.get("files", [])), "## Why it broke",
             explanation.get("headline", "Explanation unavailable"),
             f'Explained by {explanation.get("model", "unavailable")} · Confidence: {explanation.get("confidence", "unavailable")}',
             explanation.get("root_cause", ""), "Before · " + change.get("file", ""),
             "```python\n" + change.get("before", "") + "\n```", "After",
             "```python\n" + change.get("after", "") + "\n```",
             "Why tests missed it: " + explanation.get("why_tests_missed_it", ""),
             "Fix idea: " + explanation.get("fix_idea", ""), "## Hunt replay"]
    for a in data.get("repro", {}).get("attempts", []):
        parts.append(f'Reproduction {a.get("n")}: bad={a.get("bad_status")} ({a.get("bad_reason", "")}); good={a.get("good_status")} ({a.get("good_reason", "")}). {a.get("problem", "")}')
    for p in data.get("scout", []):
        parts.append(f'Scout {p.get("short")}: {p.get("status")} — {p.get("reason")}')
    for r in data.get("rounds", []):
        parts.append(f'Round {r["n"]}: tested {len(r.get("probes", []))} commits at once; {r["hi_after"] - r["lo_after"]} suspects left.')
    parts.extend([f'git bisect: about {comparison.get("binary_rounds", 0)} rounds; Culprit: {comparison.get("rounds", 0)} after scout.', "## Fix candidates"])
    rows = ["| Name | Model | Repro | Suite | Lines | Passed | Chosen |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for c in fixes.get("candidates", []):
        rows.append("| " + " | ".join(str(c.get(k, "")).replace("|", "\\|").replace("\n", " ") for k in ("name", "model", "repro_code", "suite_code", "lines", "passed", "chosen")) + " |")
    parts.append("\n".join(rows))
    baseline = fixes.get("baseline", {})
    parts.append(f'Baseline suite exit: {baseline.get("suite_code")}. Failures: {", ".join(baseline.get("failures", [])) or "none recorded"}. {baseline.get("error") or ""}')
    parts.append("Selected fix: " + str(fixes.get("chosen") or "none"))
    for c in fixes.get("candidates", []):
        if c.get("error") or not c.get("passed"):
            parts.extend([c.get("name", "") + ": " + str(c.get("error") or "verification failed"), "```text\n" + "\n".join(c.get("output", "").splitlines()[-30:]) + "\n```"])
        if c.get("chosen") and c.get("passed"):
            parts.append("```diff\n" + (c.get("patch") or "") + "\n```")
    parts.extend(["## Regression test", "```python\n" + data.get("repro", {}).get("test_code", "") + "\n```", "## Models"])
    rows = ["| Role | Model | Calls | Tokens in / out | Time | Cost USD |", "| --- | --- | --- | --- | --- | --- |"]
    for role, m in data.get("models", {}).items():
        u = next((u for u in data.get("usage", []) if u.get("role") == role), {})
        rows.append(f'| {role} | {m.get("label")} ({m.get("model")}) | {u.get("calls", 0)} | {u.get("prompt_tokens", 0)} / {u.get("completion_tokens", 0)} | {u.get("seconds", 0)} s | {u.get("cost_usd", 0):.5f} |')
    parts.extend(["\n".join(rows), f'Total model cost: ${data.get("llm_cost_usd", 0):.5f}', "## Runner and timing",
                  f'{runner.get("name", "Not started")}: {runner.get("runs", 0)} runs, {runner.get("parallel", 0)} at once; {runner.get("sandbox_seconds", 0):.1f} s summed execution, {data.get("seconds", 0):.1f} s wall time; {runner.get("errors", 0)} errors.',
                  f'Checkpoint: {runner.get("checkpoint")}. API-reported cost: {runner.get("reported_cost", 0)} (unit unspecified).',
                  "## Warnings and limits", *data.get("warnings", []), "Python/pytest repositories; first-parent history; review patches before merging."])
    return "\n\n".join(parts) + "\n"


def write_report(data: dict, out_dir: Path) -> None:
    data = redact_data(data)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "report.html").write_text(render_html(data), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(data), encoding="utf-8")
    (out_dir / "test_culprit_repro.py").write_bytes(data.get("repro", {}).get("test_code", "").encode("utf-8"))
    chosen = next((c for c in data.get("fixes", {}).get("candidates", []) if c.get("chosen") and c.get("passed")), None)
    patch_path = out_dir / "fix.patch"
    if chosen and chosen.get("patch"):
        patch_path.write_bytes(chosen["patch"].encode("utf-8"))
    elif patch_path.exists():
        patch_path.unlink()
