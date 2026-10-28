"""Shell scripts that run inside each sandbox (and in local mode).

Paths come from environment variables so the same scripts work in Nebius
Sandboxes (defaults /repo and /culprit) and on a laptop (temp folders).
Exit codes 90-93 mean "this commit could not be tested", not "bug present".
"""

SETUP_SH = r"""#!/bin/sh
# usage: setup.sh <sha-to-check-out>   (runs once; the result is saved as a checkpoint)
if ! command -v git >/dev/null 2>&1; then
  (apt-get update -qq && apt-get install -y -qq --no-install-recommends git ca-certificates) >/dev/null 2>&1 \
    || apk add --no-cache git >/dev/null 2>&1 \
    || { echo "CULPRIT: could not install git (is network access on?)"; exit 1; }
fi
rm -rf /repo
git clone -q /tmp/repo.bundle /repo 2>/dev/null || { echo "CULPRIT: could not clone the repo bundle"; exit 1; }
cd /repo || exit 1
git -c advice.detachedHead=false checkout -q --force "$1" || { echo "CULPRIT: checkout of $1 failed"; exit 1; }
python -m pip install -q --disable-pip-version-check --no-input pytest >/dev/null 2>&1
python -c "import pytest" >/dev/null 2>&1 \
  || { echo "CULPRIT: pytest is missing and pip could not install it (is network access on?)"; exit 1; }
if [ -f requirements.txt ]; then
  python -m pip install -q --disable-pip-version-check --no-input -r requirements.txt >/dev/null 2>&1 \
    || echo "CULPRIT: requirements.txt install failed"
fi
if [ -f pyproject.toml ] || [ -f setup.py ]; then
  python -m pip install -q --disable-pip-version-check --no-input -e . >/dev/null 2>&1 \
    || echo "CULPRIT: editable install skipped"
fi
echo "CULPRIT_READY $(git rev-parse --short HEAD) $(python --version 2>&1)"
"""

PROBE_SH = r"""#!/bin/sh
# usage: probe.sh <sha> <reinstall: 0|1>
REPO="${CULPRIT_REPO:-/repo}"
WORK="${CULPRIT_WORK:-/culprit}"
PY="${CULPRIT_PYTHON:-python}"
cd "$REPO" || exit 90
git -c advice.detachedHead=false checkout -q --force "$1" >/dev/null 2>&1 || { echo "CULPRIT: checkout of $1 failed"; exit 91; }
if [ "$2" = "1" ] && [ -f requirements.txt ]; then
  "$PY" -m pip install -q --disable-pip-version-check --no-input -r requirements.txt >/dev/null 2>&1 \
    || { echo "CULPRIT: dependency install failed"; exit 92; }
fi
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$REPO:$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
# Verbose mode preserves exception names in the short summary for classification.
"$PY" -m pytest -vv -rfE --tb=short -p no:cacheprovider "$WORK/test_culprit_repro.py" 2>&1
"""

VERIFY_SH = r"""#!/bin/sh
# usage: verify.sh <sha> <files|revert> [guilty-sha]
# Applies a fix (files from patch.tar, or a git revert), adds the repro test as a
# regression test, then runs that test and the whole suite.
REPO="${CULPRIT_REPO:-/repo}"
WORK="${CULPRIT_WORK:-/culprit}"
PY="${CULPRIT_PYTHON:-python}"
cd "$REPO" || exit 90
git -c advice.detachedHead=false checkout -q --force "$1" >/dev/null 2>&1 || { echo "CULPRIT: checkout failed"; exit 91; }
if [ "$2" = "revert" ]; then
  git -c user.name=culprit -c user.email=culprit@localhost revert --no-commit "$3" >/dev/null 2>&1 \
    || { echo "=== CULPRIT REVERT CONFLICT ==="; git status --short | head -n 40; exit 93; }
elif [ -f "$WORK/patch.tar" ]; then
  tar -xf - < "$WORK/patch.tar" || { echo "CULPRIT: could not unpack the patch"; exit 90; }
fi
mkdir -p tests
cp "$WORK/test_culprit_repro.py" tests/test_culprit_regression.py
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$REPO:$REPO/src${PYTHONPATH:+:$PYTHONPATH}"
"$PY" -m pytest -vv -rfE --tb=short -p no:cacheprovider tests/test_culprit_regression.py > "$WORK/repro.log" 2>&1
R1=$?
"$PY" -m pytest -vv -rfE --tb=short -p no:cacheprovider > "$WORK/suite.log" 2>&1
R2=$?
echo "=== CULPRIT CODES $R1 $R2 ==="
echo "=== CULPRIT REPRO ==="
tail -n 60 "$WORK/repro.log"
echo "=== CULPRIT SUITE ==="
tail -n 100 "$WORK/suite.log"
echo "=== CULPRIT CHANGES ==="
printf '*.egg-info/\n__pycache__/\n.pytest_cache/\n*.pyc\n' >> .git/info/exclude 2>/dev/null
git add -A >/dev/null 2>&1
git -c core.quotepath=off diff --cached --stat --no-color | tail -n 30
echo "=== CULPRIT END ==="
exit 0
"""

SCRIPTS = {"setup.sh": SETUP_SH, "probe.sh": PROBE_SH, "verify.sh": VERIFY_SH}
