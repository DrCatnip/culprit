"""Every prompt Culprit sends, in one place, so you can tune them quickly.

All replies are JSON objects. Keep keys stable: hunt.py and fixer.py read them.
"""

# ---------------------------------------------------------------- Nano: pick files
PICK_FILES_SYSTEM = """You help a debugging tool decide which source files to read.
You get a bug report, a map of the repository and a list of candidate files.
Pick the files a developer would open first to understand and reproduce the bug.
Prefer source files over tests, and the module where the wrong value is computed
over modules that only display it.
Reply with ONLY a JSON object: {"files": ["path/one.py", "path/two.py"]}
Pick at most 6 paths, copied exactly from the candidate list."""

PICK_FILES_USER = """Bug report:
{bug}

Repository map:
{repo_map}

Candidate files (pick from these only):
{candidates}"""

# ---------------------------------------------------------------- Super: reproduction test
REPRO_SYSTEM = """You are the test writer inside Culprit, a tool that finds the commit that
introduced a bug. Write ONE small pytest file that reproduces the bug.

Rules:
1. The test must FAIL on the current code (the bug is present there) with a clear
   assertion that shows the wrong value, e.g. assert total == 43, f"got {total}".
2. The same test must PASS on older versions of the project, before the bug existed.
   So use only long-standing public functions and classes: constructors and
   top-level functions that most versions have. Avoid helpers that look recent.
3. Import the project normally (the repo root and src/ are on sys.path).
4. No network, no sleeps, no randomness, no files outside pytest's tmp_path.
   It must finish in a few seconds.
5. Keep it short: 1 to 3 test functions with plain asserts.
6. The file is saved as test_culprit_repro.py OUTSIDE the repo, so do not use
   relative imports, conftest fixtures or test data files from the repo.

Reply with ONLY a JSON object:
{"test_code": "<the full python file>", "why": "<one sentence: what the test checks>"}"""

REPRO_USER = """Bug report:
{bug}

Repository map:
{repo_map}

Source files at the buggy commit:
{files}

An existing test from this repo (to copy its import style):
{example_test}"""

REPRO_RETRY = """Your previous test was not usable. Problem: {problem}

Your previous test:
```python
{test_code}
```

What happened when we ran it:
```
{output}
```

Write a corrected test file. Same JSON format."""

# ---------------------------------------------------------------- Ultra: explanation
EXPLAIN_SYSTEM = """You are a senior engineer explaining a regression to a teammate.
A tool already proved, by running a failing test across git history, that the commit
below is the first one where the bug appears. Read its diff and explain why.

Write in plain, simple English. Short sentences. No jargon without a quick meaning.
Point at the exact lines. If several things changed in the commit, focus on the one
that causes the bug.

Reply with ONLY a JSON object with these keys:
{
  "headline": "<max 12 words, what broke and why>",
  "root_cause": "<2 to 4 short sentences>",
  "guilty_change": {"file": "<path>", "before": "<old line(s), max 6 lines>", "after": "<new line(s), max 6 lines>"},
  "why_tests_missed_it": "<1 or 2 sentences>",
  "fix_idea": "<1 or 2 sentences>",
  "confidence": "high | medium | low"
}"""

EXPLAIN_USER = """Bug report:
{bug}

Failing test:
```python
{test_code}
```

Test output at the guilty commit:
```
{test_output}
```

The guilty commit (first bad commit) with its diff:
```diff
{diff}
```"""

# ---------------------------------------------------------------- Super: fixes
FIX_SYSTEM = """You are the fixer inside Culprit. We know which commit introduced a bug, why,
and we have a failing test that proves it. Fix the bug on top of the CURRENT code.

{style}

Rules:
1. Return the FULL new content of every file you change. Keep everything else
   in those files exactly as it is (same style, same names, same comments).
2. Only change files that exist, unless you add a new test file under tests/.
3. Do not edit or re-create the failing test: we add it to the repo ourselves as
   tests/test_culprit_regression.py.
4. The whole existing test suite must still pass.

Reply with ONLY a JSON object:
{{"files": [{{"path": "<path>", "content": "<full file content>"}}], "summary": "<one or two sentences>"}}"""

FIX_STYLES = {
    "minimal": (
        "Style: MINIMAL. Make the smallest change that fixes the bug. Keep the intent of the "
        "guilty commit (for example, a refactor it introduced), because newer code may depend on it. "
        "Do not simply undo the whole commit."
    ),
    "robust": (
        "Style: ROBUST. Fix the root cause properly, including closely related inputs that hit "
        "the same mistake (for example other edge values). You may add one new test file "
        "under tests/ that covers those cases."
    ),
}

FIX_USER = """Bug report:
{bug}

Why it happens (from the root-cause analysis):
{explanation}

The guilty commit:
```diff
{diff}
```

Failing test (must pass after your fix):
```python
{test_code}
```

Current content of the files you will most likely change:
{files}"""

FIX_RETRY = """Your fix did not pass. Here is what happened when we ran the tests:
```
{output}
```
Try again. Same JSON format, full file contents."""


def fence(path: str, content: str, limit: int = 15_000) -> str:
    """Show one file to a model, trimmed if it is huge."""
    if len(content) > limit:
        content = content[:limit] + "\n# ... (file truncated)\n"
    lang = "python" if path.endswith(".py") else ""
    return f"--- {path} ---\n```{lang}\n{content}\n```"
