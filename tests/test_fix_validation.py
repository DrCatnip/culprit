"""Malformed model replies and failed workers cannot approve or escape a fix."""
import asyncio
import subprocess

import pytest

from culprit import gitops
from culprit.events import EventLog
from culprit.fixer import candidate_files, find_fix, passes_baseline
from culprit.runners.base import REGRESSION_TEST, VerifyResult, safe_relpath


@pytest.mark.parametrize("reply", [None, [], {}, {"files": None}, {"files": {}},
                                      {"files": [None]}, {"files": [{"path": 1, "content": "x"}]},
                                      {"files": [{"path": "a.py", "content": None}]}])
def test_invalid_schema(reply):
    with pytest.raises(ValueError):
        candidate_files(reply, {"a.py"})


@pytest.mark.parametrize("path", ["a.py:stream", "tests/CON.py", "tests/a. /x.py", ".", "a\x00.py", "x/.GiT/config", "tests/LPT1", "C:a.py"])
def test_portable_path_rejection(path):
    with pytest.raises(ValueError):
        safe_relpath(path)


def test_duplicate_and_case_aliases():
    with pytest.raises(ValueError):
        candidate_files({"files": [{"path": name, "content": "x"} for name in ("a.py", "A.py")]}, {"a.py", "A.py"})


def test_only_exact_regression_file_is_ignored():
    baseline = VerifyResult(1, 1, suite_failures={REGRESSION_TEST + "::test_bug"})
    result = VerifyResult(0, 1, suite_failures={REGRESSION_TEST + "_extra.py::test_new"})
    assert not passes_baseline(result, baseline)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.py").write_bytes(b"original\n")
    gitops.git(tmp_path, "add", "a.py")
    gitops.git(tmp_path, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    return tmp_path


def test_tree_symlinks_rejected_without_host_symlink_privileges(repo):
    blob = gitops.git(repo, "rev-parse", "HEAD:a.py").strip()
    gitops.git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},tests")
    gitops.git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "link")
    with pytest.raises(ValueError, match="non-regular"):
        gitops.validate_patch_paths(repo, "HEAD", [REGRESSION_TEST])
    patch, error = gitops.patch_for(repo, "HEAD", extra={REGRESSION_TEST: "test"})
    assert patch is None and "non-regular" in error


def test_new_files_cannot_use_case_alias_of_tracked_directory(repo):
    (repo / "Tests").mkdir()
    (repo / "Tests/existing.py").write_text("test")
    gitops.git(repo, "add", "Tests")
    gitops.git(repo, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-qm", "directory")
    with pytest.raises(ValueError, match="case alias"):
        gitops.validate_patch_paths(repo, "HEAD", ["tests/new.py"])
    gitops.validate_patch_paths(repo, "HEAD", ["Tests/new.py"])


def test_worker_failure_and_minimal_retry_are_recorded(repo):
    class LLM:
        count = 0
        def describe(self):
            return {"super": {"label": "mock Super"}}
        def chat_json(self, role, system, user, **kwargs):
            self.count += 1
            if self.count <= 2:
                return {"files": [{"path": "a.py", "content": "broken"}]}
            assert "verify" in user.lower() or "failed" in user.lower()
            return {"files": [{"path": "a.py", "content": "fixed"}]}

    class Runner:
        async def verify(self, sha, code, timeout, patch_files=None, revert_sha=None):
            if patch_files and patch_files["a.py"] == "broken":
                raise RuntimeError("worker stopped")
            if revert_sha:
                return VerifyResult(0, 2, output="export import failed")
            return VerifyResult(0 if patch_files else 1, 0 if patch_files else 1)

    result = asyncio.run(find_fix(repo, "HEAD", "HEAD", "bug", "test", {}, "diff", LLM(), Runner(), EventLog(), 60))
    assert result["chosen"] == "minimal_retry"
    assert all(not c["passed"] and c["error"] for c in result["candidates"][:2])
    assert not result["candidates"][2]["passed"]
    assert (repo / "a.py").read_text() == "original\n"


@pytest.mark.parametrize("minimal_pass,robust_pass,revert_pass,chosen", [
    (True, True, True, "robust"), (True, False, True, "minimal"),
    (False, False, True, "revert"), (False, False, False, None),
])
def test_smallest_model_fix_precedes_revert_and_none_is_honest(repo, minimal_pass, robust_pass, revert_pass, chosen):
    class LLM:
        def describe(self):
            return {"super": {"label": "mock Super"}}
        def chat_json(self, *args, purpose, **kwargs):
            content = "robust\n" if purpose == "fix_robust" else "minimal\nsecond line\n"
            return {"files": [{"path": "a.py", "content": content}]}

    class Runner:
        async def verify(self, sha, code, timeout, patch_files=None, revert_sha=None):
            passed = revert_pass if revert_sha else (
                robust_pass if patch_files and patch_files["a.py"].startswith("robust") else minimal_pass if patch_files else False)
            return VerifyResult(0 if passed else 1, 0 if passed else 1, output="candidate test failed")

    # Keep host patch construction real. The revert of this root commit is one line,
    # smaller than either model patch; model fixes still take precedence.
    result = asyncio.run(find_fix(repo, "HEAD", "HEAD", "bug", "test", {}, "diff", LLM(), Runner(), EventLog(), 60))
    assert result["chosen"] == chosen
    assert sum(c["chosen"] for c in result["candidates"]) == int(chosen is not None)
    if not minimal_pass and not robust_pass:
        assert result["candidates"][-1]["name"] == "minimal_retry"


def test_export_keeps_verified_content_bytes_without_newline(repo):
    patch, error = gitops.patch_for(repo, "HEAD", files={"a.py": "no newline"})
    assert error is None and "No newline at end of file" in patch
    assert (repo / "a.py").read_bytes() == b"original\n"
