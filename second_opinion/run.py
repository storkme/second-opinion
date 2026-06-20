#!/usr/bin/env python3
"""Independent second-opinion PR reviewer — agentic, two providers, two deliveries.

For each open (or one named) PR: check out the PR head in a worktree, run K agentic `pi`
passes (read+bash tools) that explore the repo, union them (K>1) via one merge call, and
post a single advisory comment. Idempotent: state lives in the PR as an HTML marker
comment, so it's safe on ephemeral CI runners — no database.

Decorrelated by design: the model never reads other reviewers' comments. Two review
providers (PROVIDER): `openrouter` (hosted, paid, K defaults to 1) and `local` (a
llama.cpp llama-server, free, K defaults to 3 — the union is a recall hack for the weaker
local model). The K>1 union merge runs through MERGE_PROVIDER (defaults to PROVIDER), so
PROVIDER=local is fully offline end to end.

Two run modes: single-shot (the GitHub Action / a cron) and `--watch` (the self-hosted
daemon — sweep open PRs on an interval).

Env:
  GITHUB_REPO         owner/name (required)
  GITHUB_TOKEN        token with pull-requests:write (required; also used as GH_TOKEN)
  PROVIDER            openrouter (default) | local
  OPENROUTER_API_KEY  required when PROVIDER or MERGE_PROVIDER is openrouter
  LLAMA_SERVER_URL    required when PROVIDER or MERGE_PROVIDER is local; model is auto-discovered
  MODEL               OpenRouter model id (default z-ai/glm-5.2; ignored for PROVIDER=local)
  OPENROUTER_BASE_URL default https://openrouter.ai/api
  K                   agentic passes to union (default: 1 openrouter / 3 local; K=1 skips the merge)
  MERGE_PROVIDER      union-merge backend: openrouter | local (default = PROVIDER)
  MERGE_MODEL         model for the K>1 merge (default = the review model)
  PROJECT             project name used in the prompt (default "this")
  GUIDANCE / GUIDANCE_FILE   per-project review checklist ("memory")
  EXCLUDE_GLOBS       comma-separated globs to drop (default: lockfiles/build/images)
  MAX_DIFF_CHARS      diff cap (default 60000)
  PASS_TIMEOUT_S      per-pass timeout (default: 900 openrouter / 1800 local)
  TOOLS               pi tool grant (default read,bash; set read to drop shell)
  PI_REASONING        whether the model is a reasoning model (default true)
  REPO_DIR            repo checkout (default: cwd)

Usage: run.py [--pr N] [--dry-run] [--force] [--watch [--interval S]]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from . import review as rv
from .providers import DEFAULT_MODEL, pi_provider, write_models_json

# GITHUB_REPOSITORY is auto-set in the Action container; the bare CLI / daemon sets
# GITHUB_REPO. action.yml can't pass GITHUB_REPO (no `github` context in runs.env).
REPO = (os.environ.get("GITHUB_REPO", "").strip()
        or os.environ.get("GITHUB_REPOSITORY", "").strip())
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
PROVIDER = os.environ.get("PROVIDER", "").strip().lower() or "openrouter"
PI_PROVIDER = pi_provider(PROVIDER)
MODEL = os.environ.get("MODEL", "").strip() or DEFAULT_MODEL
OPENROUTER_BASE = (os.environ.get("OPENROUTER_BASE_URL", "").strip().rstrip("/")
                   or "https://openrouter.ai/api")
LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", "").strip().rstrip("/")
PROJECT = os.environ.get("PROJECT", "").strip() or "this"
# K is the recall lever. The local model is weaker/higher-variance, so it unions 3 passes
# by default; a strong hosted model needs no union (K=1). Override with K.
_k = os.environ.get("K", "").strip()
K = int(_k) if _k else (1 if PROVIDER == "openrouter" else 3)
MERGE_PROVIDER = os.environ.get("MERGE_PROVIDER", "").strip().lower() or PROVIDER
MERGE_MODEL = os.environ.get("MERGE_MODEL", "").strip()
MAX_DIFF_CHARS = int(os.environ.get("MAX_DIFF_CHARS", "").strip() or "60000")
_pt = os.environ.get("PASS_TIMEOUT_S", "").strip()
PASS_TIMEOUT_S = int(_pt) if _pt else (900 if PROVIDER == "openrouter" else 1800)
REPO_DIR = os.environ.get("REPO_DIR", "").strip() or os.getcwd()
PI_FLAGS = ["--no-session", "--no-extensions", "--no-skills", "--no-themes",
            "--no-prompt-templates"]
# Agent tool grant. `read,bash` lets it grep for callers/tests (best recall); set
# TOOLS=read to drop shell access on repos with untrusted PR authors. bash is NOT
# sandboxed — see the README Security section.
TOOLS = os.environ.get("TOOLS", "").strip() or "read,bash"
MARKER = "<!-- second-opinion sha={sha} -->"

MERGE_PROMPT = """\
You are merging K independent reviews of the SAME pull-request diff into one comment.
The reviews come from a sampled model, so they disagree: one pass may flag a bug
another pass ignores or even declares fine. Produce the UNION:

- Keep a finding if ANY pass raises it. A later pass dismissing it does NOT remove it.
- Deduplicate by root cause/location (passes word the same issue differently).
- For each finding keep: a severity tag in **[severity]** form (use the highest any
  pass assigned), file:line if given, a 1-3 sentence explanation (tightest version),
  and a pass-agreement note like "(2/3 passes)".
- Order: blockers/critical first, then major, then minor. Drop pure praise and
  restated diff descriptions. If NO pass found any issue, output exactly:
  "No findings from any pass. (Silence is weak evidence — this reviewer's recall is limited.)"
- Fold ALL minor-severity nits about test coverage, comments, naming, hardcoded
  values, code duplication, or stylistic consistency into ONE final line starting
  "Nits:" (comma-separated, no elaboration). Never give them their own sections.
- Output GitHub markdown only — no preamble, no JSON, no headers above ###.

=== PR #{pr}: {title} ===

{passes_block}
"""

HEADER = """{marker}
### 🤖 Second opinion — {pass_label} (`{model}`)

*Advisory, independent second opinion — agentic review, does not read other reviews.
**Silence ≠ clean.** Treat as a tripwire, not a gate.*

---

{body}
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _guidance() -> str:
    gf = os.environ.get("GUIDANCE_FILE", "").strip()
    if gf:
        p = Path(gf)
        if not p.is_absolute():
            p = Path(REPO_DIR) / gf
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
        log(f"GUIDANCE_FILE not found: {p}")
    return os.environ.get("GUIDANCE", "").strip()


def _exclude_globs() -> list[str]:
    raw = os.environ.get("EXCLUDE_GLOBS", "").strip()
    if raw:
        return [g.strip() for g in raw.split(",") if g.strip()]
    return rv.DEFAULT_EXCLUDE_GLOBS


def _gh(args: list[str], timeout_s: int = 60) -> str:
    env = {**os.environ, "GH_TOKEN": TOKEN}
    try:
        p = subprocess.run(["gh", *args], cwd=REPO_DIR, capture_output=True,
                           text=True, timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gh {' '.join(args[:3])}: timed out after {timeout_s}s")
    if p.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {p.stderr.strip()[:160]}")
    return p.stdout


def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_DIR, capture_output=True,
                          text=True, check=check)


def served_model() -> str | None:
    """The model id a local llama-server is currently serving (None if unreachable)."""
    if not LLAMA_SERVER_URL:
        return None
    try:
        d = requests.get(f"{LLAMA_SERVER_URL}/v1/models", timeout=10).json()
        return (d.get("data") or [{}])[0].get("id")
    except Exception:  # noqa: BLE001
        return None


def resolve_model() -> str | None:
    """The model id to drive pi with. For local, discovered from the server (None if down
    — the caller skips this run, which is cron/daemon-safe)."""
    if PROVIDER == "local":
        m = served_model()
        if not m:
            log("llama-server unreachable — skipping this run (cron/daemon-safe)")
            return None
        return m
    return MODEL


def _merge_model_for(model: str) -> str:
    """Default merge model: explicit MERGE_MODEL wins; else the merge provider's own model
    (the review model when providers match; an OpenRouter/local id otherwise)."""
    if MERGE_MODEL:
        return MERGE_MODEL
    if MERGE_PROVIDER == "local":
        return model if PROVIDER == "local" else (served_model() or model)
    return MODEL  # openrouter merge → an OpenRouter model id


def open_prs() -> list[dict]:
    out = _gh(["pr", "list", "--state", "open", "--limit", "200",
               "--json", "number,title,headRefOid,isDraft"])
    return [r for r in json.loads(out) if not r["isDraft"]]


def pr_meta(n: int) -> dict:
    return json.loads(_gh(["pr", "view", str(n), "--json",
                           "number,title,headRefOid,isDraft"]))


def already_reviewed(n: int, sha: str) -> bool:
    # Paginated read — `gh pr view --json comments` truncates on busy PRs. Match the
    # marker at the START of a comment body (we post it as the first line), so a comment
    # that merely *quotes* the marker can't suppress the next review.
    marker = MARKER.format(sha=sha)
    jq = f".[] | select(.body | startswith({json.dumps(marker)}))"
    out = _gh(["api", f"repos/{REPO}/issues/{n}/comments", "--paginate",
               "--jq", jq], timeout_s=120)
    return out.strip() != ""


def run_pass(wt: str, model: str, system: str, user: str) -> str:
    cmd = (["pi", "--provider", PI_PROVIDER, "--model", model] + PI_FLAGS
           + ["--tools", TOOLS, "--append-system-prompt", system, "-p", user])
    # Defense-in-depth: don't hand the agent's shell the GitHub token. pi reaches the
    # provider via the key in models.json; GH_TOKEN/GITHUB_TOKEN are for _gh() only, so
    # drop them here — a bash-tool prompt-injection can't exfiltrate the token that posts
    # comments / reads the repo. (Not a full sandbox: the OpenRouter key still lives in
    # models.json — chmod 600'd by providers.py — and the worktree's git config can hold
    # the checkout token. Trusting PR authors is the real boundary; see README Security.)
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    try:
        p = subprocess.run(cmd, cwd=wt, capture_output=True, text=True,
                           timeout=PASS_TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        log(f"pi pass timed out after {PASS_TIMEOUT_S}s")
        return ""
    if p.returncode != 0:
        # Surface the failure (bad key, unknown model id, server 4xx/OOM) instead of
        # leaving only a "0c" line. Partial stdout from a crash isn't trustworthy.
        log(f"pi pass exited {p.returncode}: {(p.stderr or '').strip()[:200]}")
        return ""
    return (p.stdout or "").strip()


def _chat(base_url: str, api_key: str, model: str, prompt: str) -> str:
    """One non-agentic chat completion (used by the K>1 merge). Defensive parse: returns
    "" on any malformed-but-200 envelope (empty choices / error / moderation shape) so the
    caller raises a clean error instead of a raw KeyError/IndexError leaking out."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    r = requests.post(
        f"{base_url}/v1/chat/completions",
        headers=headers,
        json={"model": model, "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.3, "max_tokens": 16384},
        timeout=600,
    )
    r.raise_for_status()
    choices = r.json().get("choices") or []
    return ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""


def merge_reviews(pr: int, title: str, passes: list[str], merge_model: str | None = None) -> str:
    """Union the K passes via one merge call (only used when K>1)."""
    merge_model = merge_model or MERGE_MODEL or MODEL
    passes_block = "\n\n".join(
        f"=== PASS {i+1} of {len(passes)} (independent) ===\n{p}"
        for i, p in enumerate(passes))
    prompt = MERGE_PROMPT.format(pr=pr, title=title, passes_block=passes_block)
    if MERGE_PROVIDER == "local":
        out = _chat(LLAMA_SERVER_URL, "", merge_model, prompt)
    else:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        out = _chat(OPENROUTER_BASE, key, merge_model, prompt)
    if not out:
        raise RuntimeError(f"merge ({MERGE_PROVIDER}/{merge_model}) returned no usable content")
    return out


def review_pr(pr: int, title: str, sha: str, model: str, merge_model: str, dry_run: bool) -> bool:
    diff = _gh(["pr", "diff", str(pr)])
    filtered, _files, truncated = rv.filter_diff(diff, _exclude_globs(), MAX_DIFF_CHARS)
    if not filtered.strip():
        log(f"#{pr}: empty filtered diff — skipping")
        return False

    system = rv.system_prompt(PROJECT, _guidance())

    def user_turn(diff_text: str) -> str:
        return (f"PR #{pr}: {title}\n\nThe full repository is checked out in your working "
                f"directory at the PR's head commit. Use your tools (read, grep via bash) "
                f"to inspect callers, tests, and definitions as needed. The change to "
                f"review is this diff:\n\n{diff_text}\n")

    _git(["fetch", "-q", "origin", f"refs/pull/{pr}/head"], check=False)
    wt = os.path.join(tempfile.gettempdir(), f"second-opinion-pr{pr}")
    _git(["worktree", "remove", "--force", wt], check=False)
    add = _git(["worktree", "add", "--detach", "--force", wt, sha], check=False)
    if add.returncode != 0:
        log(f"#{pr}: worktree add failed @ {sha[:10]}: {add.stderr.strip()[:120]}")
        return False

    passes: list[str] = []
    try:
        for i in range(K):
            diff_use = filtered if i == 0 else rv.shuffle_inputs(filtered, i)
            t0 = time.time()
            text = run_pass(wt, model, system, user_turn(diff_use))
            log(f"#{pr}: pass {i+1}/{K} — {len(text)}c in {time.time()-t0:.0f}s")
            if text:
                passes.append(text)
    finally:
        _git(["worktree", "remove", "--force", wt], check=False)

    if not passes:
        log(f"#{pr}: all passes empty — not posting")
        return False

    k = len(passes)
    review_body = passes[0] if k == 1 else merge_reviews(pr, title, passes, merge_model)
    pass_label = "single pass" if k == 1 else f"union ×{k}"
    body = HEADER.format(marker=MARKER.format(sha=sha), pass_label=pass_label,
                         model=model, body=review_body)
    if truncated:
        body += "\n\n*(diff truncated to fit context — coverage is partial)*"

    if dry_run:
        print("\n" + "=" * 72 + f"\nDRY RUN — would post to #{pr}:\n" + "=" * 72 + f"\n{body}\n")
        return True
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp = f.name
    try:
        _gh(["pr", "comment", str(pr), "--body-file", tmp])
    finally:
        os.unlink(tmp)
    log(f"#{pr}: posted {pass_label} review ({model})")
    return True


def sweep(args: argparse.Namespace) -> None:
    """One pass over candidate PRs: resolve the model, register it with pi, review."""
    model = resolve_model()
    if model is None:
        return
    write_models_json(model)  # register the provider's model with pi
    merge_model = _merge_model_for(model)

    targets = [pr_meta(args.pr)] if args.pr else open_prs()
    merge_desc = f"{MERGE_PROVIDER}:{merge_model}" if K > 1 else "n/a (K=1)"
    log(f"second opinion · provider={PROVIDER} · model={model} · K={K} · merge={merge_desc} "
        f"· {len(targets)} candidate PR(s)")
    for t in targets:
        n, sha, title = t["number"], t["headRefOid"], t["title"]
        if t.get("isDraft") and not args.force:
            log(f"#{n}: draft — skipping (use --force to override)")
            continue
        if not args.force and already_reviewed(n, sha):
            log(f"#{n}: head {sha[:10]} already reviewed — skipping")
            continue
        try:
            review_pr(n, title, sha, model, merge_model, args.dry_run)
        except Exception as e:  # noqa: BLE001 — one PR's failure shouldn't sink the rest
            log(f"#{n}: ERROR {str(e)[:200]} — continuing")


def _require(name: str) -> None:
    if not os.environ.get(name, "").strip():
        raise SystemExit(f"Missing required environment variable: {name}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Independent second-opinion PR reviewer")
    ap.add_argument("--pr", type=int, help="review a single PR number")
    ap.add_argument("--dry-run", action="store_true", help="print instead of posting")
    ap.add_argument("--force", action="store_true", help="ignore the marker comment")
    ap.add_argument("--watch", action="store_true",
                    help="daemon mode: sweep open PRs on an interval instead of once")
    ap.add_argument("--interval", type=int, default=1800,
                    help="seconds between sweeps in --watch mode (default 1800)")
    args = ap.parse_args()

    if not REPO:
        raise SystemExit("Missing required environment variable: GITHUB_REPO (or GITHUB_REPOSITORY)")
    _require("GITHUB_TOKEN")
    if PROVIDER == "openrouter" or MERGE_PROVIDER == "openrouter":
        _require("OPENROUTER_API_KEY")
    if PROVIDER == "local" or MERGE_PROVIDER == "local":
        _require("LLAMA_SERVER_URL")

    if not args.watch:
        sweep(args)
        return

    log(f"watch mode · interval={args.interval}s")
    while True:
        try:
            sweep(args)
        except Exception as e:  # noqa: BLE001 — a bad sweep shouldn't kill the daemon
            log(f"sweep ERROR {str(e)[:200]} — retrying next tick")
        log(f"sleeping {args.interval}s")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
