# Image for second-opinion: python + node + pi + gh + the reviewer. Used by both the
# GitHub Action (ENTRYPOINT below) and the self-hosted daemon (deploy/, which overrides
# the entrypoint to clone the target repo and run `... --watch`).
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git curl ca-certificates python3 python3-requests \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# pi: the agentic runner. Install the exact, integrity-checked dependency graph from the
# committed lockfile. Lifecycle scripts are disabled: pi ships built JS and needs none at
# install time. claude is intentionally NOT installed — both the review passes and the K>1
# merge run via OpenRouter or a local llama-server, so no Anthropic auth.
COPY package.json package-lock.json /opt/pi/
RUN npm ci --prefix /opt/pi --omit=dev --ignore-scripts --no-audit --no-fund \
    && /opt/pi/node_modules/.bin/pi --version \
    && npm cache clean --force
ENV PATH="/opt/pi/node_modules/.bin:${PATH}"

COPY second_opinion/ /opt/reviewer/second_opinion/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt/reviewer

# Build-time proof that the reviewed checkout cannot shadow the reviewer. The entrypoints
# rely on `-P` to keep cwd off sys.path; this reproduces the exact A/B — a decoy package
# in cwd against the real one on PYTHONPATH — and fails the BUILD if the decoy wins.
#
# It deliberately checks the BEHAVIOUR, not the interpreter version: asserting >= 3.11 is
# dead code, because on an older python `-P` is an unknown option and the process dies
# before any assert runs. A parse check alone is also too weak — it proves the flag is
# accepted, never that it still drops cwd. This is a tripwire for a base-image change, so
# it should test the property actually depended on.
#
# It runs `-m second_opinion.run`, the same form the entrypoints use, rather than a
# convenient `-c` import: `-P` suppresses the cwd entry for both, but a guard for a
# runtime invocation should BE that invocation. Hence the exit-code protocol — the decoy's
# run.py exits 42, and the real one exits non-zero on the missing GITHUB_REPO, which is
# expected here and why output is discarded.
#
# Both directions are asserted. Checking only that the decoy loses under -P would pass
# vacuously if a future python stopped putting cwd first at all, leaving a green build
# guarding nothing.
RUN set -eu; \
    mkdir -p /tmp/shadowcheck/second_opinion; \
    : > /tmp/shadowcheck/second_opinion/__init__.py; \
    printf 'import sys; sys.exit(42)\n' > /tmp/shadowcheck/second_opinion/run.py; \
    cd /tmp/shadowcheck; \
    rc=0; python3 -m second_opinion.run >/dev/null 2>&1 || rc=$?; \
    [ "$rc" = 42 ] || { echo "guard is vacuous: cwd no longer shadows without -P (rc=$rc)" >&2; exit 1; }; \
    rc=0; python3 -P -m second_opinion.run >/dev/null 2>&1 || rc=$?; \
    [ "$rc" != 42 ] || { echo "cwd shadowed /opt/reviewer despite -P" >&2; exit 1; }; \
    rm -rf /tmp/shadowcheck

ENTRYPOINT ["/entrypoint.sh"]
