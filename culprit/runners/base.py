"""What a runner does, and how we read pytest results."""

from __future__ import annotations

import io
import time
import re
import tarfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath

TEST_PATH = "/culprit/test_culprit_repro.py"
PATCH_TAR = "/culprit/patch.tar"
REGRESSION_TEST = "tests/test_culprit_regression.py"


class Status(str, Enum):
    PASS = "pass"   # repro test passed: bug not present at this commit
    FAIL = "fail"   # repro test failed the same way as on the buggy commit
    SKIP = "skip"   # could not test this commit (install, import, timeout...)


@dataclass
class RunResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    seconds: float = 0.0
    error: str | None = None  # infrastructure error (sandbox failed to run)

    @property
    def output(self) -> str:
        return (self.stdout or "") + ("\n" + self.stderr if self.stderr else "")

    def tail(self, lines: int = 25) -> str:
        return "\n".join(self.output.strip().splitlines()[-lines:])


@dataclass
class VerifyResult:
    repro_code: int | None
    suite_code: int | None
    output: str = ""
    seconds: float = 0.0
    repro_log: str = ""
    suite_log: str = ""
    changes: str = ""
    suite_failures: set = field(default_factory=set)
    error: str | None = None
    revert_conflict: bool = False

    @property
    def passed(self) -> bool:
        """Repro test passes and the whole suite is green."""
        return self.error is None and self.repro_code == 0 and self.suite_code == 0


_FAILED = re.compile(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$")
_EXC = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b")


def failure_types(output: str) -> set[str]:
    """Exception types from pytest's short summary, e.g. {'AssertionError'}."""
    types: set[str] = set()
    for line in output.splitlines():
        m = _FAILED.match(line.strip())
        if not m:
            continue
        msg = (m.group(3) or "").strip()
        if msg.startswith("assert") or msg.startswith("AssertionError"):
            types.add("AssertionError")
            continue
        e = _EXC.match(msg)
        types.add(e.group(1).split(".")[-1] if e else "UnknownFailure")
    return types


def failed_tests(output: str) -> set[str]:
    """Test ids from pytest's short summary, e.g. {'tests/test_x.py::test_y'}."""
    ids: set[str] = set()
    for line in output.splitlines():
        m = _FAILED.match(line.strip())
        if m:
            ids.add(m.group(2))
    return ids


def safe_relpath(path: str) -> str:
    """Reject absolute paths, '..' and .git: model output must stay inside the repo."""
    if not isinstance(path, str):
        raise ValueError("patch path must be text")
    p = PurePosixPath(path.strip().replace("\\", "/"))
    if (not p.parts or p.is_absolute() or PureWindowsPath(path).drive
            or ".." in p.parts or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(part.casefold() == ".git" or part.endswith((".", " "))
                   or any(c in part for c in ':<>"|?*')
                   or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                   for part in p.parts)):
        raise ValueError(f"unsafe path in patch: {path!r}")
    return str(p)


def make_patch_tar(files: dict[str, str]) -> bytes:
    """Pack changed files into one tar, so a sandbox gets a single upload."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel, content in sorted(files.items()):
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=safe_relpath(rel))
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


INFRA_CODES = {
    90: "could not open the repo",
    91: "checkout failed",
    92: "dependency install failed",
    93: "revert conflict",
}


def classify(res: RunResult, bad_types: set[str] | None = None) -> tuple[Status, str]:
    """Turn a probe run into PASS / FAIL / SKIP with a short human reason."""
    if res.error:
        return Status.SKIP, f"sandbox error: {res.error}"
    code = res.exit_code
    if code == 0:
        return Status.PASS, "test passed"
    if code in INFRA_CODES:
        return Status.SKIP, INFRA_CODES[code]
    if code == 1:
        types = failure_types(res.output)
        label = ", ".join(sorted(types))
        if bad_types and types and not (types & bad_types):
            return Status.SKIP, f"fails differently ({label}); probably older code"
        return Status.FAIL, f"test failed ({label})" if label else "test failed"
    if code == 2:
        return Status.SKIP, "test could not be loaded (import or syntax error)"
    if code == 5:
        return Status.SKIP, "no tests were collected"
    if code in (124, 137, -9) or code < 0:
        return Status.SKIP, "timed out or was killed"
    return Status.SKIP, f"unexpected exit code {code}"


_CODES = re.compile(r"=== CULPRIT CODES (-?\d+) (-?\d+) ===")


def section(output: str, name: str) -> str:
    """Text of one '=== CULPRIT <NAME> ===' block from verify.sh output."""
    m = re.search(rf"^=== CULPRIT {name} ===\n(.*?)(?=^=== CULPRIT |\Z)", output, re.S | re.M)
    return m.group(1).strip() if m else ""


def parse_verify(res: RunResult) -> VerifyResult:
    out = res.output
    if res.error:
        return VerifyResult(None, None, out, res.seconds, error=res.error)
    if "=== CULPRIT REVERT CONFLICT ===" in out or res.exit_code == 93:
        return VerifyResult(None, None, out, res.seconds, error="git revert hit a conflict", revert_conflict=True)
    codes = _CODES.search(out)
    if not codes:
        return VerifyResult(None, None, out, res.seconds, error=f"verify script stopped early (exit {res.exit_code})")
    suite_log = section(out, "SUITE")
    return VerifyResult(
        repro_code=int(codes.group(1)),
        suite_code=int(codes.group(2)),
        output=out,
        seconds=res.seconds,
        repro_log=section(out, "REPRO"),
        suite_log=suite_log,
        changes=section(out, "CHANGES"),
        suite_failures=failed_tests(suite_log),
    )


@dataclass
class RunnerStats:
    runs: int = 0
    seconds: float = 0.0
    errors: int = 0
    reported_cost: float = 0.0  # whatever the Sandboxes API reports per run (unit not documented in beta)
    details: list = field(default_factory=list)


class Runner(ABC):
    """Runs a script against one commit of the repo, isolated from every other run."""

    name: str = "runner"
    short: str = "runner"

    def __init__(self) -> None:
        self.stats = RunnerStats()

    @abstractmethod
    async def prepare(self, bundle: Path, head_sha: str) -> str:
        """Load the repo and install dependencies once. Returns a short status line."""

    @abstractmethod
    async def run_script(self, script: str, args: list[str], files: dict[str, bytes], timeout: int) -> RunResult:
        """Run setup/probe/verify.sh from the prepared state, in a fresh isolated copy."""

    async def probe(self, sha: str, test_code: str, reinstall: bool, timeout: int) -> RunResult:
        files = {TEST_PATH: test_code.encode("utf-8")}
        return await self.run_script("probe.sh", [sha, "1" if reinstall else "0"], files, timeout)

    async def verify(
        self,
        sha: str,
        test_code: str,
        timeout: int,
        patch_files: dict[str, str] | None = None,
        revert_sha: str | None = None,
    ) -> VerifyResult:
        """Apply a fix on top of `sha`, then run the regression test and the whole suite."""
        files = {TEST_PATH: test_code.encode("utf-8")}
        if revert_sha:
            args = [sha, "revert", revert_sha]
        else:
            args = [sha, "files"]
            if patch_files:
                files[PATCH_TAR] = make_patch_tar(patch_files)
        res = await self.run_script("verify.sh", args, files, timeout)
        return parse_verify(res)

    async def close(self) -> None:
        return None
