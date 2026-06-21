#!/usr/bin/env python3
"""Bootstrap a draft review-guidance.md from a repo's PR-review history.

Thin and project-agnostic: fetch merged PRs, pull the review findings other reviewers
already raised on them (inline review comments + review summaries — claude[bot], humans,
etc.), and synthesize the recurring, repo-specific bug classes into a draft guidance file
via ONE strong-model call. No agentic per-PR audit — that's the (deferred) high-fidelity
upgrade; this mines the review history you already have.

The output is a DRAFT to curate like CLAUDE.md, not a finished artifact — it's printed to
stdout by default (or written with --output), never wired in automatically.

Decorrelation: second-opinion's OWN comments are excluded from the corpus, so the guidance
is mined from independent signal, not the reviewer's own past output.

Env: GITHUB_TOKEN (or GH_TOKEN), OPENROUTER_API_KEY, OPENROUTER_BASE_URL (optional).
Usage: second-opinion-bootstrap --repo owner/name [--limit 50] [--model ID] [--output FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .providers import DEFAULT_MODEL
from .run import _chat  # reuse the defensive OpenRouter chat helper

# Exclude our own advisory comments from the mined corpus (decorrelation): don't let the
# reviewer bootstrap guidance from its own past output. Matches the marker run.py posts.
OWN_MARKER = "<!-- second-opinion"

# Scalar fields are .format()-ed in; the findings corpus is APPENDED raw afterwards so
# braces in review bodies (code snippets!) can't break string formatting.
SYNTHESIS_PROMPT = """\
You are distilling a project-specific code-review guidance file for {project} from
EVIDENCE: the real findings reviewers raised on its merged pull requests. Find the
RECURRING, repo-specific failure modes — the bug classes and conventions that actually
bite THIS codebase — and write them as a tight checklist a reviewer can apply.

Below are review findings mined from {n_prs} merged PRs ({n_findings} findings).

Rules:
- Ground every item in the evidence: patterns that recur across PRs or describe a concrete
  failure mode seen here. Do NOT pad with generic best-practices ("write tests", "handle
  errors") unless the evidence shows that class actually recurs in this repo.
- Two sections: "## Recurring bug classes" (failure modes) and "## Conventions"
  (repo-specific rules reviewers enforce). Omit a section if the evidence is thin.
- Per item: name the pattern — when it bites — what to check. 1-2 lines, tight.
- Prefer a class of bug that generalizes over a one-off incident.
- Output GitHub-flavored Markdown only (a review-guidance.md body), no preamble/JSON.
- Start with a one-line italic note that this was bootstrapped from PR-review history and
  should be curated before use.

=== REVIEW FINDINGS CORPUS ===
"""


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", file=sys.stderr, flush=True)


def _gh(args: list[str], timeout_s: int = 120) -> str:
    token = (os.environ.get("GITHUB_TOKEN", "").strip()
             or os.environ.get("GH_TOKEN", "").strip())
    env = {**os.environ, "GH_TOKEN": token} if token else dict(os.environ)
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True,
                           timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gh {' '.join(args[:3])}: timed out after {timeout_s}s")
    if p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {p.stderr.strip()[:160]}")
    return p.stdout


def _ndjson(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def merged_prs(repo: str, limit: int) -> list[dict]:
    out = _gh(["pr", "list", "-R", repo, "--state", "merged", "--limit", str(limit),
               "--json", "number,title"])
    return json.loads(out)


def pr_findings(repo: str, n: int) -> list[dict]:
    """A PR's inline review comments + non-empty review summaries, minus our own comments."""
    findings: list[dict] = []
    try:
        inline = _ndjson(_gh(["api", f"repos/{repo}/pulls/{n}/comments", "--paginate",
                              "-q", ".[] | {author: .user.login, path: .path, body: .body}"]))
    except RuntimeError:
        inline = []
    for c in inline:
        body = (c.get("body") or "").strip()
        if body and OWN_MARKER not in body:
            findings.append({"author": c.get("author"), "path": c.get("path"), "body": body})
    try:
        reviews = _ndjson(_gh(["api", f"repos/{repo}/pulls/{n}/reviews", "--paginate",
                               "-q", '.[] | select(.body != "") | {author: .user.login, body: .body}']))
    except RuntimeError:
        reviews = []
    for r in reviews:
        body = (r.get("body") or "").strip()
        if body and OWN_MARKER not in body:
            findings.append({"author": r.get("author"), "path": None, "body": body})
    return findings


def build_corpus(items: list[tuple], max_chars: int, max_finding_chars: int = 600) -> str:
    """items: [(pr_number, title, [findings])]. Whole-PR blocks, capped at max_chars."""
    blocks: list[str] = []
    total = 0
    for n, title, findings in items:
        if not findings:
            continue
        lines = [f"=== PR #{n}: {title} ==="]
        for f in findings:
            loc = f"{f['path']}: " if f.get("path") else ""
            body = " ".join(f["body"].split())[:max_finding_chars]
            lines.append(f"- [{f.get('author') or '?'}] {loc}{body}")
        block = "\n".join(lines)
        if total + len(block) > max_chars:
            break
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks)


def synthesize(corpus: str, project: str, model: str, n_prs: int, n_findings: int) -> str:
    base = (os.environ.get("OPENROUTER_BASE_URL", "").strip().rstrip("/")
            or "https://openrouter.ai/api")
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is required")
    prompt = SYNTHESIS_PROMPT.format(project=project, n_prs=n_prs, n_findings=n_findings) + corpus
    out = _chat(base, key, model, prompt)
    if not out:
        raise RuntimeError(f"synthesis ({model}) returned no usable content")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bootstrap a draft review-guidance.md from a repo's PR-review history")
    ap.add_argument("--repo", help="owner/name (default: $GITHUB_REPO)")
    ap.add_argument("--limit", type=int, default=50,
                    help="most-recent merged PRs to mine (default 50)")
    ap.add_argument("--model", default="", help=f"synthesis model (default {DEFAULT_MODEL})")
    ap.add_argument("--max-chars", type=int, default=60000,
                    help="cap on the findings corpus sent to the model (default 60000)")
    ap.add_argument("--output", help="write the draft here (default: stdout)")
    args = ap.parse_args()

    repo = (args.repo or os.environ.get("GITHUB_REPO", "")).strip()
    if not repo:
        raise SystemExit("Missing repo: pass --repo owner/name or set GITHUB_REPO")
    model = args.model.strip() or DEFAULT_MODEL

    log(f"fetching up to {args.limit} merged PRs from {repo}")
    items: list[tuple] = []
    n_findings = 0
    for pr in merged_prs(repo, args.limit):
        f = pr_findings(repo, pr["number"])
        if f:
            items.append((pr["number"], pr["title"], f))
            n_findings += len(f)
            log(f"#{pr['number']}: {len(f)} finding(s)")
    if not items:
        raise SystemExit(
            f"No review findings mined from {repo}'s last {args.limit} merged PRs. This thin "
            "bootstrap mines EXISTING review history; a repo without one needs the agentic "
            "time-travel audit (not yet implemented).")

    corpus = build_corpus(items, args.max_chars)
    log(f"synthesizing from {len(items)} PR(s) / {n_findings} finding(s) via {model}")
    draft = synthesize(corpus, repo.split("/")[-1], model, len(items), n_findings)

    if args.output:
        Path(args.output).write_text(draft.rstrip() + "\n", encoding="utf-8")
        log(f"wrote draft -> {args.output} — curate it like CLAUDE.md before pointing the reviewer at it")
    else:
        print(draft)


if __name__ == "__main__":
    main()
