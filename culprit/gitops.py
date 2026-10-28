"""Local git helpers. Culprit reads history on your machine and runs code in sandboxes."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Iterable


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {err}")
    return proc.stdout.decode("utf-8", errors="replace")


@dataclass
class Commit:
    sha: str
    short: str
    subject: str
    author: str
    date: str

    def to_dict(self) -> dict:
        return asdict(self)


_SEP = "\x1f"
_FMT = _SEP.join(["%H", "%h", "%s", "%an", "%ad"])


def _parse(line: str) -> Commit:
    sha, short, subject, author, date = line.split(_SEP)
    return Commit(sha=sha, short=short, subject=subject, author=author, date=date)


def ensure_local_repo(src: str, cache_dir: Path) -> Path:
    """Return a local clone for a folder path or a git URL."""
    path = Path(src).expanduser()
    if path.exists():
        return Path(git(path, "rev-parse", "--show-toplevel").strip())
    if re.match(r"^(https?://|git@|ssh://)", src):
        name = src.rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name) or "repo"
        dest = cache_dir / name
        if dest.exists():
            git(dest, "fetch", "--all", "--tags", "--quiet")
        else:
            cache_dir.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(["git", "clone", "--quiet", src, str(dest)], capture_output=True)
            if proc.returncode != 0:
                raise GitError(proc.stderr.decode("utf-8", errors="replace").strip())
        return dest
    raise GitError(f"Not a git folder or a git URL: {src}")


def rev_parse(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def commit_info(repo: Path, ref: str) -> Commit:
    out = git(repo, "show", "-s", f"--format={_FMT}", "--date=short", ref).strip()
    return _parse(out)


def first_parent_range(repo: Path, good: str, bad: str) -> list[Commit]:
    """Commits after `good` up to and including `bad`, oldest first (first-parent line)."""
    out = git(repo, "log", "--first-parent", "--reverse", f"--format={_FMT}", "--date=short", f"{good}..{bad}")
    return [_parse(line) for line in out.splitlines() if line.strip()]


def first_parent_history(repo: Path, bad: str, limit: int = 1024) -> list[str]:
    """SHAs from `bad` backwards along the first-parent line, newest first."""
    out = git(repo, "rev-list", "--first-parent", f"--max-count={limit}", bad)
    return out.split()


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer], capture_output=True)
    return proc.returncode == 0


def diff_of(repo: Path, sha: str, max_chars: int = 40_000) -> str:
    out = git(repo, "show", "--stat", "--patch", "--no-color", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n%B", sha)
    if len(out) > max_chars:
        return out[:max_chars] + "\n... (diff truncated)\n"
    return out


def files_touched(repo: Path, sha: str) -> list[str]:
    out = git(repo, "show", "--format=", "--name-only", sha)
    return [line for line in out.splitlines() if line.strip()]


def ls_files(repo: Path, ref: str) -> list[str]:
    return [line for line in git(repo, "ls-tree", "-r", "--name-only", ref).splitlines() if line.strip()]


def file_at(repo: Path, ref: str, path: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True)
    if proc.returncode != 0:
        return None
    data = proc.stdout
    if b"\x00" in data[:8000]:
        return None  # binary file
    return data.decode("utf-8", errors="replace")


DEPENDENCY_FILES = ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "setup.cfg")


def deps_changed(repo: Path, a: str, b: str) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), "diff", "--quiet", a, b, "--", *DEPENDENCY_FILES], capture_output=True)
    return proc.returncode == 1


def make_bundle(repo: Path, out: Path) -> Path:
    """Pack the whole history into one file we can upload to a sandbox."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    git(repo, "bundle", "create", str(out), "--all")
    return out


def validate_patch_paths(repo: Path, base: str, paths: Iterable[str]) -> None:
    """Reject writes through tracked symlinks, gitlinks and case aliases on any OS."""
    from culprit.runners.base import safe_relpath

    tree = {}
    for record in git(repo, "ls-tree", "-r", "-t", "-z", base).split("\0"):
        if record:
            meta, name = record.split("\t", 1)
            tree[name.casefold()] = (name, meta.split()[0])
    for raw in paths:
        path = safe_relpath(raw)
        parts = path.split("/")
        for n in range(1, len(parts) + 1):
            prefix = "/".join(parts[:n])
            entry = tree.get(prefix.casefold())
            if entry and (entry[0] != prefix or (
                entry[1] != "040000" if n < len(parts) else entry[1] not in ("100644", "100755")
            )):
                raise ValueError(f"patch path crosses a non-regular file or case alias: {path}")


def patch_for(
    repo: Path,
    base: str,
    files: dict[str, str] | None = None,
    revert_sha: str | None = None,
    extra: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Build the exact patch for a fix on your machine (git only, no repo code runs).

    Returns (patch, error). `files` are full new file contents; `revert_sha`
    means "git revert this commit"; `extra` adds files such as the regression test.
    """
    from culprit.runners.base import safe_relpath

    tmp = Path(tempfile.mkdtemp(prefix="culprit-patch-"))
    try:
        validate_patch_paths(repo, base, list(files or {}) + list(extra or {}))
        wt = tmp / "wt"
        proc = subprocess.run(
            ["git", "clone", "-q", "--shared", "--no-checkout", str(repo), str(wt)], capture_output=True
        )
        if proc.returncode != 0:
            return None, proc.stderr.decode("utf-8", errors="replace").strip()
        git(wt, "-c", "advice.detachedHead=false", "checkout", "-q", "--force", base)
        if revert_sha:
            proc = subprocess.run(
                ["git", "-C", str(wt), "-c", "user.name=culprit", "-c", "user.email=culprit@localhost",
                 "-c", "core.hooksPath=/dev/null", "revert", "--no-commit", revert_sha],
                capture_output=True,
            )
            if proc.returncode != 0:
                return None, "git revert hit a conflict"
        for group in (files or {}, extra or {}):
            for rel, content in group.items():
                dest = wt / safe_relpath(rel)
                if not dest.resolve().is_relative_to(wt.resolve()) or any(
                    parent.is_symlink() for parent in (dest, *dest.parents) if parent.is_relative_to(wt)
                ):
                    raise ValueError(f"patch path crosses a symlink: {rel}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content.encode("utf-8"))
        git(wt, "add", "-A")
        return git(wt, "-c", "core.quotepath=off", "diff", "--cached", "--no-color"), None
    except (GitError, ValueError, OSError) as exc:
        return None, str(exc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def changed_lines(patch: str, exclude: tuple[str, ...] = ()) -> int:
    """Count added + removed lines in a unified diff, skipping excluded files."""
    count, current = 0, ""
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" b/", 1)
            current = parts[1] if len(parts) == 2 else ""
            continue
        if current in exclude or line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            count += 1
    return count


def repo_name(repo: Path) -> str:
    return repo.resolve().name
