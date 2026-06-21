#!/usr/bin/env python3
"""Bootstrap a draft review-guidance.md from a repo's PR-review history.

Thin and project-agnostic: take the most-recent merged PRs densely PLUS a sample spread
across the older history (recent-dense catches current bug clusters; the historical sample
surfaces older classes — neither alone gets both), pull the review findings other reviewers
already raised on them (inline review comments + review summaries — claude[bot], humans,
etc.), and synthesize the recurring, repo-specific bug classes into a draft guidance file
via ONE strong-model call. No agentic per-PR audit — that's the (deferred) high-fidelity
upgrade; this mines the review history you already have.

The output is a DRAFT to curate like CLAUDE.md, not a finished artifact — it's printed to
stdout by default (or written with --output), never wired in automatically.

Decorrelation is structural: we mine line-level reviewer findings from the *pulls* API
(inline review comments + formal review summaries), NOT the PR conversation stream (the
*issues* comments endpoint) where second-opinion posts its own advisory — so the reviewer's
own output can't enter the corpus it learns from.

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


def _sample_evenly(prs: list[dict], k: int) -> list[dict]:
    """Pick k PRs spread evenly across the list (gh returns newest-first), including both
    ends — so the corpus spans the repo's history instead of clustering on the newest PRs.
    Returns all of them when k >= len(prs). Order (newest-first) is preserved."""
    n = len(prs)
    if k <= 0:
        return []
    if k >= n:
        return prs
    if k == 1:
        return [prs[0]]
    idxs = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
    return [prs[i] for i in idxs]


def pr_findings(repo: str, n: int) -> list[dict]:
    """A PR's line-level review findings: inline review comments + non-empty review
    summaries. Both are *pulls* endpoints — second-opinion posts to *issues*, so its own
    advisory is never read here (structural decorrelation; see module docstring)."""
    findings: list[dict] = []
    try:
        inline = _ndjson(_gh(["api", f"repos/{repo}/pulls/{n}/comments", "--paginate",
                              "-q", ".[] | {author: .user.login, path: .path, body: .body}"]))
    except RuntimeError:
        inline = []
    for c in inline:
        body = (c.get("body") or "").strip()
        if body:
            findings.append({"author": c.get("author"), "path": c.get("path"), "body": body})
    try:
        reviews = _ndjson(_gh(["api", f"repos/{repo}/pulls/{n}/reviews", "--paginate",
                               "-q", '.[] | select(.body != "") | {author: .user.login, body: .body}']))
    except RuntimeError:
        reviews = []
    for r in reviews:
        body = (r.get("body") or "").strip()
        if body:
            findings.append({"author": r.get("author"), "path": None, "body": body})
    return findings


def build_corpus(items: list[tuple], max_chars: int, max_finding_chars: int = 600,
                 max_per_pr: int = 8) -> tuple[str, int, int]:
    """items: [(pr_number, title, [findings])]. Whole-PR blocks, capped at max_chars.
    At most max_per_pr findings per PR — a few "hot" PRs with 20+ findings would otherwise
    eat the char budget and crowd out the rest, and guidance wants breadth of PRs over depth
    on one. Returns (corpus, n_prs, n_findings) for what's ACTUALLY included, so the caller
    reports accurate counts. Oversized blocks are skipped (not a hard stop), so one big PR
    early in the list doesn't discard every later one."""
    blocks: list[str] = []
    total = n_prs = n_findings = 0
    for n, title, findings in items:
        if not findings:
            continue
        kept = findings[:max_per_pr]
        lines = [f"=== PR #{n}: {title} ==="]
        for f in kept:
            loc = f"{f['path']}: " if f.get("path") else ""
            body = " ".join(f["body"].split())[:max_finding_chars]
            lines.append(f"- [{f.get('author') or '?'}] {loc}{body}")
        if len(findings) > max_per_pr:
            lines.append(f"- (+{len(findings) - max_per_pr} more findings on this PR)")
        block = "\n".join(lines)
        if total + len(block) > max_chars:
            continue
        blocks.append(block)
        total += len(block)
        n_prs += 1
        n_findings += len(kept)
    return "\n\n".join(blocks), n_prs, n_findings


def synthesize(corpus: str, project: str, model: str, n_prs: int, n_findings: int,
               save_dir: Path | None = None) -> str:
    base = (os.environ.get("OPENROUTER_BASE_URL", "").strip().rstrip("/")
            or "https://openrouter.ai/api")
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is required")
    prompt = SYNTHESIS_PROMPT.format(project=project, n_prs=n_prs, n_findings=n_findings) + corpus
    if save_dir:  # persist the exact synthesis input for analysis / prompt iteration
        (save_dir / "corpus.txt").write_text(corpus, encoding="utf-8")
        (save_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    out = _chat(base, key, model, prompt)
    if not out:
        raise RuntimeError(f"synthesis ({model}) returned no usable content")
    if save_dir:
        (save_dir / "response.md").write_text(out, encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Bootstrap a draft review-guidance.md from a repo's PR-review history")
    ap.add_argument("--repo", help="owner/name (default: $GITHUB_REPO)")
    ap.add_argument("--limit", type=int, default=50,
                    help="merged PRs to mine, sampled evenly across --window (default 50)")
    ap.add_argument("--window", type=int, default=300,
                    help="pool of recent merged PRs to draw from (default 300)")
    ap.add_argument("--recent", type=int, default=20,
                    help="of --limit, how many to take from the most-recent PRs densely; the "
                    "rest are sampled across the older window (default 20)")
    ap.add_argument("--model", default="", help=f"synthesis model (default {DEFAULT_MODEL})")
    ap.add_argument("--max-chars", type=int, default=60000,
                    help="cap on the findings corpus sent to the model (default 60000)")
    ap.add_argument("--max-findings-per-pr", type=int, default=8,
                    help="cap findings included per PR (default 8) — favors breadth of PRs "
                    "over depth on one hot PR")
    ap.add_argument("--output", help="write the draft here (default: stdout)")
    ap.add_argument("--save-dir", help="persist the findings cache + synthesis prompt/response "
                    "here (for analysis, prompt iteration, and free re-runs)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch from GitHub even when --save-dir has a cached findings.json")
    args = ap.parse_args()

    repo = (args.repo or os.environ.get("GITHUB_REPO", "")).strip()
    if not repo:
        raise SystemExit("Missing repo: pass --repo owner/name or set GITHUB_REPO")
    model = args.model.strip() or DEFAULT_MODEL

    save_dir = Path(args.save_dir) if args.save_dir else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
    cache = (save_dir / "findings.json") if save_dir else None

    if cache and cache.exists() and not args.refresh:
        items = [(r["number"], r["title"], r["findings"])
                 for r in json.loads(cache.read_text(encoding="utf-8"))]
        log(f"loaded {len(items)} PR(s) with findings from {cache} (use --refresh to re-fetch)")
    else:
        pool = merged_prs(repo, args.window)
        recent = pool[:min(args.recent, args.limit)]
        sampled_rest = _sample_evenly(pool[len(recent):], args.limit - len(recent))
        selected = recent + sampled_rest
        log(f"mining {len(selected)} of {len(pool)} merged PRs "
            f"({len(recent)} most-recent + {len(sampled_rest)} sampled across older history)")
        items = []
        for pr in selected:
            f = pr_findings(repo, pr["number"])
            if f:
                items.append((pr["number"], pr["title"], f))
                log(f"#{pr['number']}: {len(f)} finding(s)")
        if cache:
            cache.write_text(json.dumps(
                [{"number": n, "title": t, "findings": f} for n, t, f in items], indent=2),
                encoding="utf-8")
    if not items:
        raise SystemExit(
            f"No review findings in the {args.limit} PR(s) sampled from {repo}'s {args.window} "
            "most-recent merged PRs. This thin bootstrap mines EXISTING review history; a repo "
            "without one needs the agentic time-travel audit (not yet implemented).")

    corpus, n_prs, n_findings = build_corpus(items, args.max_chars,
                                             max_per_pr=args.max_findings_per_pr)
    if not corpus:
        raise SystemExit(
            f"All {len(items)} PR(s) with findings exceed --max-chars={args.max_chars}; raise it.")
    log(f"synthesizing from {n_prs} PR(s) / {n_findings} finding(s) via {model}")
    draft = synthesize(corpus, repo.split("/")[-1], model, n_prs, n_findings, save_dir)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(draft.rstrip() + "\n", encoding="utf-8")
        log(f"wrote draft -> {out} — curate it like CLAUDE.md before pointing the reviewer at it")
    else:
        print(draft)


if __name__ == "__main__":
    main()
