"""Collect just enough code for the models: a repo map plus the most relevant files.

Step 1: rank files by how often words from the bug report appear in them.
Step 2: Nemotron 3 Nano picks the best few from that short list (cheap and fast).
Step 3: read those files at the buggy commit, within a size budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from culprit import prompts
from culprit.gitops import file_at, git, ls_files

SOURCE_EXT = (".py", ".pyi", ".toml", ".cfg", ".ini", ".txt", ".md", ".csv", ".json", ".yaml", ".yml")
SKIP_DIRS = ("node_modules/", ".venv/", "venv/", "dist/", "build/", "site-packages/", ".git/")
STOP = set(
    "the a an and or but if then else when while for to of in on at by with from into is are was were be been "
    "it its this that these those there here as not no yes do does did done have has had can could should "
    "would will shall may might must i we you they he she our your their my me us them sometimes always "
    "never show shows shown get gets got make makes made one two use used using also just only very more "
    "less than some any all each every should bug bugs issue error wrong expected actual instead".split()
)


@dataclass
class Context:
    repo_map: str
    files: dict[str, str] = field(default_factory=dict)
    example_test: str = ""
    example_test_path: str = ""
    picked_by: str = ""

    def files_block(self) -> str:
        return "\n\n".join(prompts.fence(p, c) for p, c in self.files.items()) or "(no files)"


def keywords(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
    seen, out = set(), []
    for w in words:
        lw = w.lower()
        if lw in STOP or lw in seen:
            continue
        seen.add(lw)
        out.append(lw)
    return out[:40]


def render_map(paths: list[str], limit: int = 300) -> str:
    shown = paths[:limit]
    more = f"\n... and {len(paths) - limit} more files" if len(paths) > limit else ""
    return "\n".join(shown) + more


def is_test(path: str) -> bool:
    name = Path(path).name
    return path.startswith(("tests/", "test/")) or "/tests/" in path or name.startswith("test_") or name.endswith("_test.py")


def rank_files(repo: Path, sha: str, paths: list[str], bug: str, top: int = 14) -> list[tuple[str, int]]:
    """Score source files: bug words in the path count a lot, matching lines count a little."""
    kws = keywords(bug)
    counts: dict[str, int] = {}
    if kws:
        args = ["grep", "-c", "-i", "-F"]
        for kw in kws:
            args += ["-e", kw]
        out = git(repo, *args, sha, "--", "*.py", check=False)
        prefix = sha + ":"
        for line in out.splitlines():
            rest = line[len(prefix):] if line.startswith(prefix) else line
            path, _, n = rest.rpartition(":")
            if n.isdigit():
                counts[path] = int(n)
    scored = []
    for path in paths:
        if not path.endswith(".py") or is_test(path):
            continue
        low = path.lower()
        score = sum(6 for kw in kws if kw in low) + min(counts.get(path, 0), 20)
        scored.append((path, score))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:top]


def pick_example_test(paths: list[str], chosen: list[str]) -> str | None:
    tests = [p for p in paths if p.endswith(".py") and is_test(p) and "conftest" not in p]
    for src in chosen:
        stem = Path(src).stem
        for t in tests:
            if Path(t).stem in (f"test_{stem}", f"{stem}_test"):
                return t
    return tests[0] if tests else None


def build_context(repo: Path, sha: str, bug: str, llm, budget_chars: int = 60_000) -> Context:
    all_paths = [p for p in ls_files(repo, sha) if not p.startswith(SKIP_DIRS)]
    shown = [p for p in all_paths if p.endswith(SOURCE_EXT)]
    repo_map = render_map(shown)
    ranked = rank_files(repo, sha, all_paths, bug)
    candidates = [p for p, _ in ranked]

    chosen: list[str] = []
    picked_by = "keyword ranking"
    if candidates:
        try:
            data = llm.chat_json(
                "nano",
                prompts.PICK_FILES_SYSTEM,
                prompts.PICK_FILES_USER.format(bug=bug, repo_map=repo_map, candidates="\n".join(candidates)),
                purpose="pick_files",
                max_tokens=4000,
            )
            chosen = [p for p in data.get("files", []) if isinstance(p, str) and p in candidates][:6]
            if chosen:
                picked_by = "Nemotron 3 Nano"
        except Exception:
            chosen = []
    if not chosen:
        chosen = candidates[:5]

    files: dict[str, str] = {}
    used = 0
    for path in chosen:
        content = file_at(repo, sha, path)
        if content is None:
            continue
        if used + len(content) > budget_chars:
            content = content[: max(budget_chars - used, 2_000)] + "\n# ... (truncated)\n"
        files[path] = content
        used += len(content)
        if used >= budget_chars:
            break

    example_path = pick_example_test(all_paths, chosen)
    example = (file_at(repo, sha, example_path) or "")[:6_000] if example_path else ""
    return Context(repo_map, files, example, example_path or "", picked_by)
