"""Reports remain readable, escaped, redacted and reproducible without services."""
import copy
import json
from html.parser import HTMLParser

import pytest

from culprit.cli import main
from culprit.report import write_report


@pytest.fixture
def run():
    commits = [dict(short=f"commit{i}", subject=f"Subject {i}") for i in range(4)]
    return {
        "ok": True, "started_at": "2026-10-28", "seconds": 12.0, "bug": "Original <bug>",
        "repo": {"name": "demo", "good": {"short": "good"}},
        "runner": {"name": "Local runner", "runs": 8, "parallel": 4, "sandbox_seconds": 20.0},
        "models": {"super": {"model": "offline-script", "label": "Super (offline stand-in)"}},
        "range": commits, "scout": [{"short": "commit3a", "status": "fail", "reason": "AssertionError"}],
        "rounds": [{"n": n, "probes": [{"i": n, "short": f"commit{n}", "status": "pass"}],
                    "lo_after": n, "hi_after": 3} for n in (1, 2)],
        "comparison": {"binary_rounds": 2, "rounds": 2},
        "culprit": {"commit": commits[3], "confirmed": True},
        "explanation": {"headline": "Rounding", "model": "Super (offline stand-in)", "confidence": "high"},
        "repro": {"test_code": "def test_bug():\n    assert True\n", "attempts": [{"n": 1, "bad_status": "fail", "good_status": "pass"}]},
        "fixes": {"chosen": "minimal", "candidates": [{"name": "minimal", "model": "Super",
            "chosen": True, "passed": True, "patch": "-old\n+new\n", "repro_code": 0, "suite_code": 0, "lines": 2},
            {"name": "revert", "passed": False, "output": "ImportError: to_paise"}]},
    }


class InspectHTML(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.rounds, self.cells, self.external, self.scripts = [], 0, [], 0
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "data-round" in a:
            self.rounds.append(a["data-round"])
        if tag == "span" and a.get("class", "").startswith("cell "):
            assert a.get("title") and a.get("aria-label")
            self.cells += 1
        if tag == "script":
            self.scripts += 1
        if "src" in a or "href" in a:
            self.external.append(a)


def test_complete_report_and_rebuild_are_deterministic(run, tmp_path):
    original = copy.deepcopy(run)
    write_report(run, tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    page = (tmp_path / "report.html").read_text(encoding="utf-8")
    parsed = InspectHTML(page)
    assert parsed.rounds == ["0", "1", "2"]
    assert parsed.cells == 3 * len(run["range"])
    assert 'commit3 · fail' in page  # probe shorthands can be longer than history shorthands
    assert not parsed.external and not parsed.scripts
    for text in ("Subject 3", "Reproduction validation", "Scout", "confirmed", "ImportError: to_paise", "offline stand-in", "Selected patch", "Tokens in / out", "summed execution time"):
        assert text in page
    assert "Original &lt;bug&gt;" in page
    assert "#0ca30c" in page and "max-width:600px" in page and "@media print" in page
    assert main(["report", str(tmp_path)]) == 0
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert run == original


@pytest.mark.parametrize("variant", ["empty", "partial", "flaky", "no_fix", "retry"])
def test_partial_and_alternative_outcomes(run, tmp_path, variant):
    if variant == "empty":
        run = {}
    elif variant == "partial":
        run.update(ok=False, reason="Could not reproduce")
        run["culprit"], run["explanation"], run["fixes"] = {}, {}, {}
    elif variant == "flaky":
        run["culprit"].update(confirmed=False, ambiguous=["commit2"])
    elif variant == "no_fix":
        run["fixes"] = {}
    elif variant == "retry":
        run["fixes"]["candidates"][0]["name"] = "minimal_retry"
    (tmp_path / "fix.patch").write_text("stale")
    write_report(run, tmp_path)
    assert (tmp_path / "report.html").is_file()
    assert json.loads((tmp_path / "run.json").read_text()) == run
    if variant in ("empty", "partial", "no_fix"):
        assert not (tmp_path / "fix.patch").exists()
    if variant == "flaky":
        assert "flaky" in (tmp_path / "report.html").read_text(encoding="utf-8")


def test_untrusted_text_and_unknown_credentials_never_escape(run, tmp_path, monkeypatch):
    monkeypatch.setenv("CUSTOM_PASSWORD", "synthetic-env-secret")
    run["bug"] = '<script>alert(1)</script>\nAuthorization: Bearer synthetic-header-secret\nAPI_KEY="synthetic-assignment-secret"\nsynthetic-env-secret'
    run["diagnostic"] = {"NEBIUS_PROJECT_ID": "synthetic-project-secret", "Authorization": "synthetic-map-secret",
                         "project_id": "synthetic-sdk-project", "apiKey": "synthetic-camel-key"}
    write_report(run, tmp_path)
    for p in tmp_path.iterdir():
        text = p.read_text(encoding="utf-8")
        for secret in ("synthetic-env-secret", "synthetic-header-secret", "synthetic-assignment-secret", "synthetic-project-secret", "synthetic-map-secret", "synthetic-sdk-project", "synthetic-camel-key"):
            assert secret not in text, p.name
    page = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert '<script>' not in page and '&lt;script&gt;' in page


def test_invalid_rebuild_returns_failure(tmp_path):
    assert main(["report", str(tmp_path)]) == 1
    (tmp_path / "run.json").write_text("[]")
    assert main(["report", str(tmp_path)]) == 1
