#!/bin/sh
# Docker-action entrypoint: review the triggering PR (or all open PRs if PR_NUMBER
# is empty). The repo is mounted at GITHUB_WORKSPACE; run there.
set -eu

REPO_DIR="${GITHUB_WORKSPACE:-$PWD}"
export REPO_DIR

# Docker actions run as root while the checkout is owned by the runner user — tell
# git the mounted workspace is trusted so worktree/fetch don't trip "dubious ownership".
git config --global --add safe.directory "$REPO_DIR" || true

# The action reviews exactly one PR. An empty PR_NUMBER means a non-PR trigger
# (workflow_dispatch/schedule) with no `pr-number` input — fail loudly rather than
# silently sweeping every open PR (which run.py would otherwise do with no --pr).
if [ -z "${PR_NUMBER:-}" ]; then
  echo "[second-opinion] No PR number. Trigger on a 'pull_request' event, or set the" >&2
  echo "[second-opinion] 'pr-number' input. (To sweep all open PRs, run the CLI:" >&2
  # -P for the same reason as the exec below: run from inside a checkout that ships its
  # own second_opinion/ and an un-flagged sweep reviews with THAT copy, not this one.
  echo "[second-opinion]  python -P -m second_opinion.run )" >&2
  exit 1
fi

# -P keeps cwd OFF sys.path. Without it `python3 -m` prepends cwd *ahead of* the image's
# PYTHONPATH=/opt/reviewer — and cwd here is the checkout being reviewed. So reviewing a
# repo that itself ships a top-level `second_opinion/` package (i.e. this one) silently
# imported the PR's copy of the reviewer instead of the released one: the dogfood check
# pinned @v1, logged "Download action repository ... SHA e4d8891" (v1.7.0), and then ran
# the branch's 1.8.0 code. Harmless-looking, but it means the released artifact was never
# the thing under test, and a PR could only ever be reviewed by its own reviewer.
# Requires python >= 3.11, asserted at image build time.
exec python3 -P -m second_opinion.run --pr "$PR_NUMBER"
