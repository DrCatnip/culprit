"""The hunt pipeline, using the existing git, runner, model and event interfaces."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from culprit import __version__, bisect, context, gitops, prompts
from culprit.config import Settings
from culprit.demo_repo import is_demo_repo
from culprit.events import EventLog
from culprit.fixer import find_fix
from culprit.llm import LLMError, OfflineDemoLLM, TokenFactoryLLM
from culprit.report import write_report
from culprit.runners.base import RunResult, Runner, Status, classify, failure_types
from culprit.runners.local_runner import LocalRunner
from culprit.runners import make_runner
from culprit.tasks import gather, run_blocking
from culprit.security import redact, redact_data


class HuntStopped(RuntimeError):
    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Hunt:
    def __init__(self, repo: str, bug: str, settings: Settings, out_dir: Path, events: EventLog,
                 bad: str, good: str | None, runner: Runner | None, llm: Any,
                 offline: bool, no_fix: bool, runner_kind: str, trust_repo: bool):
        self.source, self.bug, self.settings = repo, bug, settings
        self.out_dir, self.events = out_dir, events
        events.secrets += (settings.api_key or "", settings.project_id or "")
        self.bad_ref, self.good_ref = bad, good
        self.runner, self.llm = runner, llm
        self.offline, self.no_fix = offline, no_fix
        self.runner_kind, self.trust_repo = runner_kind, trust_repo
        self.repo = Path(repo)
        self.bad, self.good = "", ""
        self.test_code = ""
        self.bad_types: set[str] = set()
        self.results: dict[str, RunResult] = {}
        self.commits: list[gitops.Commit] = []
        self.t0 = time.monotonic()
        self.data: dict = {
            "version": __version__, "started_at": _now(), "finished_at": None,
            "seconds": 0.0, "ok": False, "reason": "",
            "repo": {"name": Path(repo).name, "source": repo, "bad": {}, "good": {},
                     "good_found_by": "user" if good else "scout"},
            "bug": bug, "runner": {}, "models": {}, "usage": [], "llm_cost_usd": 0.0,
            "context": {"files": [], "picked_by": ""},
            "repro": {"test_code": "", "why": "", "attempts": []},
            "scout": [], "range": [], "rounds": [], "culprit": {}, "explanation": {},
            "fixes": {"baseline": {}, "candidates": [], "chosen": None},
            "comparison": {"commits": 0, "rounds": 0, "binary_rounds": 0, "probes": 0,
                           "parallel": settings.parallel}, "warnings": [],
        }

    @asynccontextmanager
    async def stage(self, name: str):
        self.events.emit("stage", name=name, status="running", detail="")
        try:
            yield
        except BaseException as exc:
            detail = "Interrupted." if isinstance(exc, asyncio.CancelledError) else redact(str(exc), (self.settings.api_key or "", self.settings.project_id or ""))
            self.events.emit("stage", name=name, status="failed", detail=detail)
            raise
        else:
            self.events.emit("stage", name=name, status="done", detail="")

    def warn(self, msg: str) -> None:
        self.data["warnings"].append(msg)
        self.events.emit("warn", msg=msg)

    async def prepare(self) -> None:
        if self.offline and not is_demo_repo(self.repo):
            raise HuntStopped("Offline mode only works with the bundled demo repo (run: culprit demo-repo).", 1)
        if not self.offline and self.llm is None and not self.settings.api_key:
            raise HuntStopped("NEBIUS_API_KEY is not set. Configure it in the environment or .env.", 1)
        self.repo = await run_blocking(gitops.ensure_local_repo, self.source, self.settings.cache_dir)
        local = self.offline or self.runner_kind == "local" or getattr(self.runner, "short", None) == "local"
        if local and not is_demo_repo(self.repo) and not self.trust_repo:
            raise HuntStopped("Local execution of this repository requires --i-trust-this-repo.", 1)
        self.bad = gitops.rev_parse(self.repo, self.bad_ref)
        if self.good_ref:
            self.good = gitops.rev_parse(self.repo, self.good_ref)
            history = gitops.first_parent_history(self.repo, self.bad, limit=2**31 - 1)
            if self.good == self.bad or self.good not in history:
                raise HuntStopped("--good must be older than --bad on its first-parent history.", 1)
        if self.llm is None:
            self.llm = (OfflineDemoLLM(self.repo, self.events) if self.offline
                        else TokenFactoryLLM(self.settings, self.events))
        if self.runner is None:
            self.runner = (LocalRunner(parallel=self.settings.parallel) if local
                           else make_runner(self.runner_kind, self.settings))
        self.data["models"] = self.llm.describe()
        self.data["repo"].update(name=self.repo.name, bad=gitops.commit_info(self.repo, self.bad).to_dict())
        if self.good:
            self.data["repo"]["good"] = gitops.commit_info(self.repo, self.good).to_dict()
        bundle = self.out_dir / "repo.bundle"
        try:
            await run_blocking(gitops.make_bundle, self.repo, bundle)
            detail = await self.runner.prepare(bundle, self.bad)
        finally:
            bundle.unlink(missing_ok=True)
        self.events.emit("info", msg=detail)
        if self.offline:
            self.events.emit("info", msg="Offline stand-in: scripted answers; local execution; no API calls.")

    async def probe(self, sha: str, *, round_number: int, index: int | None = None,
                    offset: int | None = None) -> tuple[RunResult, Status, str]:
        assert self.runner is not None
        fields = {"round": round_number, "short": sha[:8]}
        fields.update({"i": index} if index is not None else {"offset": offset})
        self.events.emit("probe_started", **fields)
        result = await self.runner.probe(
            sha, self.test_code, gitops.deps_changed(self.repo, self.bad, sha), self.settings.probe_timeout,
        )
        status, reason = classify(result, self.bad_types or None)
        self.results[sha] = result
        self.data["comparison"]["probes"] += 1
        self.events.emit("probe_finished", **fields, status=status.value, reason=reason, seconds=result.seconds)
        return result, status, reason

    async def scout(self) -> tuple[bool, str, str]:
        """Return (found_good, feedback, output) so all-SKIP scouts can retry the test."""
        history = gitops.first_parent_history(self.repo, self.bad, limit=2**31 - 1)
        offsets = sorted({2**power for power in range(10) if 2**power < len(history)} |
                         ({len(history) - 1} if len(history) > 1 else set()))
        outcomes = await gather(*(self.probe(history[offset], round_number=0, offset=offset) for offset in offsets))
        self.data["scout"] = [{"offset": offset, "short": history[offset][:8],
                               "status": status.value, "reason": reason}
                              for offset, (_, status, reason) in zip(offsets, outcomes)]
        for offset, (_, status, _) in zip(offsets, outcomes):
            if status == Status.PASS:
                self.good = history[offset]
                self.data["repo"]["good"] = gitops.commit_info(self.repo, self.good).to_dict()
                return True, "", ""
        if outcomes and all(status == Status.SKIP for _, status, _ in outcomes):
            return False, "The test could not run on any older commit; use older, basic APIs.", outcomes[-1][0].tail()
        raise HuntStopped("The bug is older than the history I checked; pass --good.")

    async def reproduce(self, ctx: context.Context) -> None:
        original_user = prompts.REPRO_USER.format(
            bug=self.bug, repo_map=ctx.repo_map, files=ctx.files_block(), example_test=ctx.example_test,
        )
        feedback, output = "", ""
        for n in range(1, self.settings.max_test_attempts + 1):
            async with self.stage("repro"):
                user = original_user
                if feedback:
                    user += "\n\n" + prompts.REPRO_RETRY.format(problem=feedback, test_code=self.test_code, output=output)
                reply = await run_blocking(self.llm.chat_json, "super", prompts.REPRO_SYSTEM,
                                                user, purpose="write_repro_test")
                self.test_code = reply.get("test_code", "")
                if not isinstance(self.test_code, str) or not self.test_code.strip():
                    raise HuntStopped("The reproduction reply contained no test code.", 1)
                self.data["repro"].update(test_code=self.test_code, why=reply.get("why", ""))
                self.bad_types = set()
                shas = [self.bad] + ([self.good] if self.good_ref else [])
                outcomes = await gather(*(self.probe(sha, round_number=0, offset=0 if sha == self.bad else None)
                                                  for sha in shas))
                bad_result, bad_status, bad_reason = outcomes[0]
                self.bad_types = failure_types(bad_result.output)
                attempt = {"n": n, "bad_status": bad_status.value, "bad_reason": bad_reason,
                           "good_status": None, "good_reason": None, "problem": ""}
                self.data["repro"]["attempts"].append(attempt)
                feedback = ""
                output = bad_result.tail()
                if bad_result.exit_code != 1 or bad_result.error:
                    feedback = ("The test PASSED on the buggy commit; it must fail there."
                                if bad_status == Status.PASS else "The test could not run on the buggy commit.")
                elif self.good_ref:
                    good_result = outcomes[1][0]
                    good_status, good_reason = classify(good_result, self.bad_types)
                    attempt.update(good_status=good_status.value, good_reason=good_reason)
                    if good_status == Status.FAIL:
                        raise HuntStopped("The bug is already present at the good commit; pick an older --good.")
                    if good_status != Status.PASS:
                        feedback = "The test fails on the OLD version; use older, basic APIs."
                        output = good_result.tail()
                attempt["problem"] = feedback
                if feedback:
                    self.events.emit("info", msg=f"Retrying reproduction: {feedback}")
                    if n == self.settings.max_test_attempts:
                        raise HuntStopped("Could not validate a reproduction test within the allowed attempts.")
                    continue
            if self.good_ref:
                self.events.emit("stage", name="scout", status="skipped", detail="--good supplied")
                return
            async with self.stage("scout"):
                found, feedback, output = await self.scout()
                if found:
                    return
                self.data["repro"]["attempts"][-1]["problem"] = feedback
                if n == self.settings.max_test_attempts:
                    raise HuntStopped("Could not validate a reproduction test within the allowed attempts.")
        raise HuntStopped("Could not validate a reproduction test within the allowed attempts.")

    async def search(self) -> None:
        self.commits = gitops.first_parent_range(self.repo, self.good, self.bad)
        self.data["range"] = [commit.to_dict() for commit in self.commits]
        self.events.emit("history", commits=[dict(i=i, **commit.to_dict()) for i, commit in enumerate(self.commits)],
                         good=self.data["repo"]["good"], bad=self.data["repo"]["bad"])
        n = len(self.commits)
        self.data["comparison"].update(commits=n, binary_rounds=bisect.expected_rounds(n, 1))
        lo, hi = -1, n - 1
        skipped: set[int] = set()
        cached = {i: classify(self.results[commit.sha], self.bad_types)[0]
                  for i, commit in enumerate(self.commits) if commit.sha in self.results and i < hi}
        skipped.update(i for i, status in cached.items() if status == Status.SKIP)
        lo, hi, inconsistent = bisect.update_bounds(lo, hi, cached)
        if inconsistent:
            self.warn("The bug comes and goes (flaky test, or it was reverted and re-added).")
        while hi > lo + 1:
            probes = bisect.pick_probes(lo, hi, self.settings.parallel, skipped)
            if not probes:
                ambiguous = [self.commits[i].short for i in range(lo + 1, hi + 1)]
                self.data["culprit"] = {"index": hi, "commit": self.commits[hi].to_dict(),
                                        "confirmed": False, "ambiguous": ambiguous}
                self.events.emit("culprit_found", i=hi, commit=self.commits[hi].to_dict(), confirmed=False, ambiguous=ambiguous)
                raise HuntStopped("Skipped commits leave the first bad commit ambiguous.")
            number = len(self.data["rounds"]) + 1
            entry = {"n": number, "lo_before": lo, "hi_before": hi, "probes": [], "lo_after": lo, "hi_after": hi}
            self.data["rounds"].append(entry)
            self.events.emit("round_started", round=number, lo=lo, hi=hi, probes=probes)
            outcomes = await gather(*(self.probe(self.commits[i].sha, round_number=number, index=i) for i in probes))
            statuses = {}
            for i, (result, status, reason) in zip(probes, outcomes):
                statuses[i] = status
                if status == Status.SKIP:
                    skipped.add(i)
                entry["probes"].append({"i": i, "short": self.commits[i].short, "status": status.value,
                                         "reason": reason, "seconds": result.seconds})
            lo, hi, inconsistent = bisect.update_bounds(lo, hi, statuses)
            if inconsistent:
                self.warn("The bug comes and goes (flaky test, or it was reverted and re-added).")
            entry.update(lo_after=lo, hi_after=hi)
            self.data["comparison"]["rounds"] = number
            self.events.emit("round_finished", round=number, lo=lo, hi=hi, suspects=hi - lo)
        self.lo, self.hi = lo, hi

    async def confirm(self) -> None:
        guilty = self.commits[self.hi]
        previous = self.commits[self.lo].sha if self.lo >= 0 else self.good
        outcomes = await gather(*(self.probe(sha, round_number=-1, index=i)
            for sha, i in [(guilty.sha, self.hi), (previous, self.lo)] * 2))
        confirmed = [status for _, status, _ in outcomes] == [Status.FAIL, Status.PASS] * 2
        if not confirmed:
            self.warn("Flaky: confirmation did not reproduce the same boundary twice.")
        self.data["culprit"] = {"index": self.hi, "commit": guilty.to_dict(),
                                "files": gitops.files_touched(self.repo, guilty.sha),
                                "diff": gitops.diff_of(self.repo, guilty.sha), "confirmed": confirmed,
                                "ambiguous": [], "output": outcomes[0][0].output}
        self.events.emit("culprit_found", i=self.hi, commit=guilty.to_dict(), confirmed=confirmed, ambiguous=[])

    async def explain(self) -> None:
        culprit = self.data["culprit"]
        user = prompts.EXPLAIN_USER.format(bug=self.bug, test_code=self.test_code,
                                           test_output=culprit["output"], diff=culprit["diff"])
        role = "ultra"
        try:
            reply = await run_blocking(self.llm.chat_json, role, prompts.EXPLAIN_SYSTEM, user, purpose="explain_culprit")
        except LLMError:
            role = "super"
            self.warn("Ultra explanation failed; using Super for the explanation.")
            reply = await run_blocking(self.llm.chat_json, role, prompts.EXPLAIN_SYSTEM, user, purpose="explain_culprit")
        self.data["explanation"] = dict(reply, model=self.llm.describe()[role]["label"])

    async def run(self) -> tuple[dict, int]:
        code = 0
        try:
            async with self.stage("prepare"):
                await self.prepare()
            async with self.stage("context"):
                ctx = await run_blocking(context.build_context, self.repo, self.bad, self.bug, self.llm)
                self.data["context"] = {"files": list(ctx.files), "picked_by": ctx.picked_by}
                if self.offline and ctx.picked_by == "Nemotron 3 Nano":
                    self.data["context"]["picked_by"] += " (offline stand-in)"
            await self.reproduce(ctx)
            async with self.stage("bisect"):
                await self.search()
            async with self.stage("confirm"):
                await self.confirm()
            async with self.stage("explain"):
                await self.explain()
            if self.no_fix:
                self.events.emit("stage", name="fix", status="skipped", detail="--no-fix supplied")
            else:
                async with self.stage("fix"):
                    assert self.runner is not None
                    self.data["fixes"] = await find_fix(
                        self.repo, self.bad, self.commits[self.hi].sha, self.bug, self.test_code,
                        self.data["explanation"], self.data["culprit"]["diff"], self.llm,
                        self.runner, self.events, self.settings.probe_timeout,
                    )
                    if not self.data["fixes"]["chosen"]:
                        self.warn("No verified fix passed; review the candidate logs.")
            self.data.update(ok=True, reason="Culprit found.")
        except asyncio.CancelledError:
            code = 1
            self.data["reason"] = "Interrupted; partial report written."
        except HuntStopped as exc:
            code = exc.code
            self.data["reason"] = str(exc)
        except Exception as exc:
            code = 1
            self.data["reason"] = str(exc)
        finally:
            if self.runner:
                try:
                    await self.runner.close()
                except Exception:
                    self.warn("Could not fully close the runner.")
                if getattr(self.runner, "cleanup_note", ""):
                    self.warn(self.runner.cleanup_note)
                if any(detail.get("truncated") for detail in self.runner.stats.details):
                    self.warn("Some Sandbox output was truncated; review probe classifications and candidate logs.")
                stats = self.runner.stats
                self.data["runner"] = {"name": self.runner.name, "parallel": self.settings.parallel,
                                       "runs": stats.runs, "sandbox_seconds": stats.seconds,
                                       "errors": stats.errors, "checkpoint": getattr(self.runner, "checkpoint_id", None),
                                       "reported_cost": stats.reported_cost}
            if self.llm:
                self.data.update(usage=self.llm.usage.rows(), llm_cost_usd=self.llm.usage.total_cost())
                close = getattr(self.llm, "close", None)
                if close:
                    try:
                        await run_blocking(close)
                    except Exception:
                        self.warn("Could not fully close the model client.")
            self.data.update(finished_at=_now(), seconds=round(time.monotonic() - self.t0, 3))
            self.data = redact_data(self.data, (self.settings.api_key or "", self.settings.project_id or ""))
            try:
                async with self.stage("report"):
                    write_report(self.data, self.out_dir)
            except Exception as exc:
                code = 1
                self.data.update(ok=False, reason=redact(
                    f"Could not write report: {exc}",
                    (self.settings.api_key or "", self.settings.project_id or ""),
                ))
            self.events.emit("run_finished", ok=self.data["ok"], reason=self.data["reason"],
                             seconds=self.data["seconds"], out_dir=str(self.out_dir))
        return self.data, code


async def hunt(
    repo: str | Path, bug: str, settings: Settings, out_dir: Path, *,
    bad: str = "HEAD", good: str | None = None, offline: bool = False, no_fix: bool = False,
    runner: Runner | None = None, llm: Any = None, events: EventLog | None = None,
    runner_kind: str = "contree", trust_repo: bool = False,
) -> tuple[dict, int]:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    events = events if events is not None else EventLog(out_dir / "events.jsonl")
    return await _Hunt(str(repo), bug, settings, out_dir, events, bad, good, runner, llm,
                       offline, no_fix, runner_kind, trust_repo).run()
