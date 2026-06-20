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
  echo "[second-opinion]  python -m second_opinion.run )" >&2
  exit 1
fi

exec python3 -m second_opinion.run --pr "$PR_NUMBER"
