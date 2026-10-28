"""Persist complete or partial hunt results. The full HTML design comes later."""

from __future__ import annotations

import html
import json
from pathlib import Path


def write_report(data: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    culprit = data.get("culprit", {})
    commit = culprit.get("commit", {})
    explanation = data.get("explanation", {})
    fixes = data.get("fixes", {})
    result = (f"Found {commit.get('short')}: {commit.get('subject')}"
              if data.get("ok") else f"Hunt incomplete: {data.get('reason', '')}")
    offline = any(model.get("model") == "offline-script" for model in data.get("models", {}).values())
    mode = "Offline stand-in: scripted answers, local execution, no model API calls." if offline else ""
    markdown = ["# Culprit report", "", result, "", mode, "", "## Bug", data.get("bug", ""),
                "", "## Culprit", json.dumps(commit, ensure_ascii=False),
                f"Confirmed: {culprit.get('confirmed', False)}",
                f"Files: {', '.join(culprit.get('files', []))}",
                f"Ambiguous commits: {', '.join(culprit.get('ambiguous', []))}", "", "## Why it broke",
                explanation.get("headline", ""), explanation.get("root_cause", ""),
                explanation.get("why_tests_missed_it", ""), explanation.get("fix_idea", ""),
                f"Explained by: {explanation.get('model', '')}",
                f"Confidence: {explanation.get('confidence', '')}",
                "", "Before:", "```python", explanation.get("guilty_change", {}).get("before", ""), "```",
                "After:", "```python", explanation.get("guilty_change", {}).get("after", ""), "```",
                "", "## Hunt replay"]
    for entry in data.get("scout", []):
        markdown.append(f"Scout {entry['short']}: {entry['status']} ({entry['reason']})")
    for entry in data.get("rounds", []):
        markdown.append(f"Round {entry['n']}: tested {len(entry['probes'])} commits; "
                        f"{entry['hi_after'] - entry['lo_after']} suspects left.")
    comparison = data.get("comparison", {})
    markdown.extend([f"Git bisect: about {comparison.get('binary_rounds', 0)} rounds. "
                     f"Culprit: {comparison.get('rounds', 0)} rounds after scout.",
                     "", "## Fix candidates", "| Name | Model | Repro | Suite | Lines | Passed | Chosen |",
                     "| --- | --- | --- | --- | --- | --- | --- |"])
    for candidate in fixes.get("candidates", []):
        markdown.append("| " + " | ".join(str(candidate.get(key, "")) for key in (
            "name", "model", "repro_code", "suite_code", "lines", "passed", "chosen")) + " |")
    chosen = next((candidate for candidate in fixes.get("candidates", []) if candidate.get("chosen")), None)
    patch_path = out_dir / "fix.patch"
    if chosen and chosen.get("patch"):
        patch_path.write_bytes(chosen["patch"].encode("utf-8"))
        markdown.extend(["", "```diff", chosen["patch"], "```"])
    elif patch_path.exists():
        patch_path.unlink()
    test_code = data.get("repro", {}).get("test_code", "")
    (out_dir / "test_culprit_repro.py").write_bytes(test_code.encode("utf-8"))
    markdown.extend(["", "## Regression test", "```python", test_code, "```", "", "## Models",
                     json.dumps(data.get("models", {}), ensure_ascii=False),
                     json.dumps(data.get("usage", []), ensure_ascii=False),
                     f"Model cost: ${data.get('llm_cost_usd', 0):.5f}", "", "## Runner",
                     json.dumps(data.get("runner", {}), ensure_ascii=False), "", "## Warnings"])
    markdown.extend(data.get("warnings", []))
    markdown.append("Python/pytest repositories; first-parent history; review any patch before merging.")
    text = "\n".join(markdown) + "\n"
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    page = ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Culprit report</title><style>body{font-family:system-ui;max-width:960px;'
            'margin:2rem auto;padding:1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
            f'<h1>Culprit report</h1><p>{html.escape(result)}</p><p>{html.escape(mode)}</p>'
            f'<pre>{html.escape(text)}</pre></html>')
    (out_dir / "report.html").write_text(page, encoding="utf-8")
