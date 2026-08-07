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
RUN set -eu; \
    mkdir -p /tmp/shadowcheck/second_opinion; \
    printf 'DECOY = True\n' > /tmp/shadowcheck/second_opinion/__init__.py; \
    cd /tmp/shadowcheck; \
    python3 -P -c 'import second_opinion as m; assert not getattr(m, "DECOY", False), \
"cwd shadowed /opt/reviewer despite -P: " + str(m.__file__)'; \
    python3 -c 'import second_opinion as m; assert getattr(m, "DECOY", False), \
"the decoy no longer shadows without -P, so this check proves nothing"'; \
    rm -rf /tmp/shadowcheck

ENTRYPOINT ["/entrypoint.sh"]
