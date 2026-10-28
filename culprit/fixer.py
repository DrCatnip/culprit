"""Generate candidates and verify them against the reproduction and baseline suite."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path, PureWindowsPath
from typing import Any

from culprit import gitops, prompts
from culprit.events import EventLog
from culprit.llm import LLMError
from culprit.runners.base import REGRESSION_TEST, Runner, VerifyResult, safe_relpath
from culprit.security import redact, redact_data, safe_error


def candidate_files(reply: dict, existing: set[str]) -> dict[str, str]:
    """Validate model paths before uploading or building any patch."""
    files: dict[str, str] = {}
    if not isinstance(reply, dict) or not isinstance(reply.get("files"), list):
        raise ValueError("fix must contain a files array")
    seen = set()
    for item in reply["files"]:
        if not isinstance(item, dict) or "path" not in item or "content" not in item:
            raise ValueError("fix files need path and content fields")
        raw = item["path"]
        path = safe_relpath(raw)
        if PureWindowsPath(path).drive or path == "." or any(part.lower() == ".git" for part in path.split("/")):
            raise ValueError(f"unsafe path in patch: {raw!r}")
        if path.casefold() == REGRESSION_TEST.casefold():
            continue
        if path.casefold() in seen or any(
            path.casefold().startswith(old + "/") or old.startswith(path.casefold() + "/") for old in seen
        ):
            raise ValueError("fix contains overlapping or duplicate paths")
        seen.add(path.casefold())
        if path not in existing and not (path.startswith("tests/") and path.endswith(".py")):
            raise ValueError(f"fix cannot create this file: {path}")
        if not isinstance(item["content"], str):
            raise ValueError(f"fix content must be text: {path}")
        files[path] = item["content"]
    if not files:
        raise ValueError("fix did not contain any changed files")
    return files


def passes_baseline(result: VerifyResult, baseline: VerifyResult) -> bool:
    if result.error or result.repro_code != 0:
        return False
    if result.suite_code == 0:
        return True
    # Only actual pytest failures may be compared; infrastructure and collection
    # failures must never become a passing candidate through an empty set.
    if result.suite_code != 1 or baseline.error or baseline.suite_code != 1:
        return False
    failures = {test for test in result.suite_failures if test.replace("\\", "/").split("::")[0] != REGRESSION_TEST}
    old_failures = {test for test in baseline.suite_failures if test.replace("\\", "/").split("::")[0] != REGRESSION_TEST}
    return bool(result.suite_failures) and failures <= old_failures


async def find_fix(
    repo: Path, bad_sha: str, guilty_sha: str, bug: str, test_code: str,
    explanation: dict, diff: str, llm: Any, runner: Runner, events: EventLog, timeout: int,
) -> dict:
    existing = set(gitops.ls_files(repo, bad_sha))
    likely = gitops.files_touched(repo, guilty_sha)
    block = "\n\n".join(
        prompts.fence(path, content)
        for path in likely if not path.startswith("tests/")
        if (content := gitops.file_at(repo, bad_sha, path)) is not None
    )
    user = prompts.FIX_USER.format(
        bug=bug, explanation=json.dumps(explanation, ensure_ascii=False), diff=diff,
        test_code=test_code, files=block,
    )
    label = llm.describe()["super"]["label"]

    async def generate(style: str, retry: str = "") -> tuple[dict, dict[str, str]]:
        candidate = {"name": style, "model": label, "summary": "", "passed": False,
                     "repro_code": None, "suite_code": None, "lines": 0,
                     "patch": None, "error": None, "chosen": False}
        try:
            reply = await asyncio.to_thread(
                llm.chat_json, "super", prompts.FIX_SYSTEM.format(style=prompts.FIX_STYLES[style]),
                user + retry, purpose=f"fix_{style}", temperature=0.4 if style == "robust" else 0.2,
            )
            files = candidate_files(reply, existing)
            if not isinstance(reply.get("summary", ""), str):
                raise ValueError("fix summary must be text")
            candidate["summary"] = redact(reply.get("summary", ""))
            gitops.validate_patch_paths(repo, bad_sha, [*files, REGRESSION_TEST])
            return candidate, files
        except (LLMError, ValueError, KeyError, TypeError) as exc:
            candidate["error"] = redact(str(exc))
            return candidate, {}

    generated = await asyncio.gather(generate("minimal"), generate("robust"))
    revert = {"name": "revert", "model": "git revert", "summary": "Revert the guilty commit.",
              "passed": False, "repro_code": None, "suite_code": None, "lines": 0,
              "patch": None, "error": None, "chosen": False}
    candidates = [entry[0] for entry in generated] + [revert]

    async def verify(candidate: dict, files: dict[str, str] | None = None) -> VerifyResult:
        if candidate["error"]:
            return VerifyResult(None, None, error=candidate["error"])
        try:
            gitops.validate_patch_paths(repo, bad_sha, [*(files or {}), REGRESSION_TEST])
            return await runner.verify(bad_sha, test_code, timeout, patch_files=files,
                                       revert_sha=guilty_sha if candidate["name"] == "revert" else None)
        except Exception as exc:
            return VerifyResult(None, None, error=safe_error(exc))

    baseline, *results = await asyncio.gather(
        verify({"name": "baseline", "error": None}),
        *(verify(candidate, files) for candidate, files in generated),
        verify(revert),
    )

    async def record(candidate: dict, result: VerifyResult, files: dict[str, str] | None = None) -> None:
        candidate.update(repro_code=result.repro_code, suite_code=result.suite_code,
                         passed=passes_baseline(result, baseline), error=result.error or candidate["error"],
                         output=redact(result.output))
        if not candidate["error"]:
            patch, error = await asyncio.to_thread(
                gitops.patch_for, repo, bad_sha, files=files,
                revert_sha=guilty_sha if candidate["name"] == "revert" else None,
                extra={REGRESSION_TEST: test_code},
            )
            candidate["patch"] = patch
            candidate["error"] = error
            candidate["lines"] = gitops.changed_lines(patch or "", exclude=(REGRESSION_TEST,))
            if error:
                candidate["passed"] = False
        events.emit("candidate_finished", **{key: candidate[key] for key in (
            "name", "model", "passed", "repro_code", "suite_code", "lines", "error")})

    for candidate, result, files in zip(candidates, results, [generated[0][1], generated[1][1], None]):
        await record(candidate, result, files)
    if not any(candidate["passed"] for candidate in candidates[:2]):
        feedback = results[0].output or candidates[0]["error"] or "The fix did not pass."
        candidate, files = await generate("minimal", prompts.FIX_RETRY.format(output=feedback[-6000:]))
        result = await verify(candidate, files)
        candidate["name"] = "minimal_retry"
        await record(candidate, result, files)
        candidates.append(candidate)
    model_fixes = [candidate for candidate in candidates if candidate["name"] != "revert" and candidate["passed"]]
    chosen = min(model_fixes, key=lambda candidate: candidate["lines"]) if model_fixes else revert if revert["passed"] else None
    if chosen:
        chosen["chosen"] = True
    return redact_data({"baseline": {"suite_code": baseline.suite_code, "failures": sorted(baseline.suite_failures),
                                     "error": baseline.error, "output": baseline.output},
                        "candidates": candidates, "chosen": chosen["name"] if chosen else None})
