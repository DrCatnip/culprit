from culprit.runners.base import (
    REGRESSION_TEST,
    RunResult,
    Runner,
    Status,
    VerifyResult,
    classify,
    failed_tests,
    failure_types,
    safe_relpath,
)

__all__ = [
    "REGRESSION_TEST", "RunResult", "Runner", "Status", "VerifyResult",
    "classify", "failed_tests", "failure_types", "make_runner", "safe_relpath",
]


def make_runner(kind: str, settings) -> Runner:
    if kind == "local":
        from culprit.runners.local_runner import LocalRunner

        return LocalRunner(parallel=min(settings.parallel, 6))
    if kind == "contree":
        from culprit.runners.contree_runner import ContreeRunner

        return ContreeRunner(settings)
    raise ValueError(f"Unknown runner: {kind} (use 'contree' or 'local')")
