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

# pi: the agentic runner. claude is intentionally NOT installed — both the review passes
# and the K>1 merge run via OpenRouter or a local llama-server, so no Anthropic auth.
RUN npm install -g @mariozechner/pi-coding-agent && npm cache clean --force

COPY second_opinion/ /opt/reviewer/second_opinion/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt/reviewer
ENTRYPOINT ["/entrypoint.sh"]
