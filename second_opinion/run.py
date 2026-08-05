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
  PASS_TIMEOUT_S      per-pass timeout (default 1800; the calling job's budget must exceed it)
  MAX_PASS_TOKENS     abort a pass over this many tokens (default: unset = no ceiling)
  MAX_PASS_COST_USD   same, in USD (default: unset = no ceiling)
  TOOLS               pi tool grant (default read,bash; set read to drop shell)
  PI_REASONING        whether the model is a reasoning model (default true)
  FAIL_ON_DEGRADED    exit 2 when a degraded pass posts no review (default true; set
                      false for the old always-green behavior)
  REPO_DIR            repo checkout (default: cwd)

Usage: run.py [--pr N] [--dry-run] [--force] [--watch [--interval S]]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import subprocess
import shutil
import threading
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

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
PASS_TIMEOUT_S = int(_pt) if _pt else 1800
# Spend ceilings, per pass. A wall clock bounds TIME, not money: a looping agent burned
# 12.6M tokens ($1.96) producing nothing, and raising PASS_TIMEOUT_S only raised the bill.
# Empty = DISABLED, deliberately: killing a legitimately long pass is its own failure
# mode, and one incident is thin evidence for a default. Opt in per repo, where you have
# the numbers. Tokens are the primary lever because they are provider-neutral and always
# available; cost depends on a pricing lookup that returns None when it fails, which would
# silently disable a cost-only ceiling.
# Deferred because _annotate is defined below; main() emits these at startup.
_CONFIG_ERRORS: list[str] = []


def _num_env(name: str, cast, default):
    """Parse a numeric env knob, or disable it loudly. `max-pass-cost-usd: "$1.96"` and
    `"1,000"` are natural operator mistakes, and a raw ValueError traceback at import is
    the opposite of this project's "fail legibly through the degraded machinery" rule."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        _CONFIG_ERRORS.append(f"{name}={raw!r} is not a number — ignoring it, so that "
                              f"ceiling is INACTIVE for this run")
        return default


MAX_PASS_TOKENS = _num_env("MAX_PASS_TOKENS", int, 0)
MAX_PASS_COST_USD = _num_env("MAX_PASS_COST_USD", float, 0.0)
BUDGET_POLL_S = 20
REPO_DIR = os.environ.get("REPO_DIR", "").strip() or os.getcwd()
PI_FLAGS = ["--no-extensions", "--no-skills", "--no-themes",
            "--no-prompt-templates"]
# Agent tool grant. `read,bash` lets it grep for callers/tests (best recall); set
# TOOLS=read to drop shell access on repos with untrusted PR authors. bash is NOT
# sandboxed — see the README Security section.
TOOLS = os.environ.get("TOOLS", "").strip() or "read,bash"
# A silent failure (a degraded pass that posts no review) exits non-zero so the check
# turns red — a tripwire for reviewer *malfunction*, not review *findings*. Opt out for
# the old always-green behavior.
FAIL_ON_DEGRADED = (os.environ.get("FAIL_ON_DEGRADED", "true").strip().lower()
                    in ("1", "true", "yes", "on"))
MARKER = "<!-- second-opinion sha={sha} -->"
FAIL_MARKER = "<!-- second-opinion-failed sha={sha} -->"
# GitHub rejects an issue/PR comment body over 65536 characters. Nothing upstream bounds
# the posted review (MAX_DIFF_CHARS caps the *input*), and an over-cap body 422s on post →
# the exception leaves review_pr → sweep files it as a silent failure → exit 2 with no
# review at all. The merge-fallback path is the worst case, since it concatenates the K raw
# passes with no dedup and is therefore strictly larger than the merged body would be.
COMMENT_MAX = 65536
# Filename for the untruncated diff dropped into the worktree when the prompt excerpt is
# capped. Deliberately inside the checkout: pi's `read` tool is reliable there, whereas an
# absolute path outside it depends on the tool grant (TOOLS=read alone could not reach it),
# and a full diff the agent cannot open is just a quieter version of the bug this fixes.
# `git worktree remove --force` deletes it with the worktree, so there is nothing to clean up.
FULL_DIFF_NAME = ".second-opinion-full-diff.patch"

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


def _annotate(level: str, msg: str) -> None:
    """Emit a GitHub Actions workflow annotation (surfaces in the checks UI + job
    summary), flushed at the point of failure so a later hang/timeout can't swallow it.
    Off Actions it's just a line on stdout — harmless."""
    # The runner percent-decodes %25/%0D/%0A in the message, so escape them (the inverse,
    # per actions/toolkit escapeData) or a literal "%25" in pi's stderr renders as "%".
    msg = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title=second-opinion::{msg}", flush=True)


def _as_text(v) -> str:
    """Decode a subprocess payload that may be bytes even when text=True was requested."""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v or ""


def _peek(out: str | None, limit: int = 200) -> str:
    """Collapse whitespace and truncate a subprocess\'s partial output for a log/annotation, so a blocked pass carries a forensic tail instead of a silent black box."""
    return " ".join((out or "").split())[:limit]


def _should_fail(posted: bool, degraded: bool) -> bool:
    """The tripwire: a degraded pass (timeout / non-zero pi exit / empty output / failed
    head-checkout) that produced NO posted review is a silent failure — surface it as a
    non-zero exit. A
    posted review is success even when a K>1 sibling degraded, and a clean run with
    nothing to say (no degraded pass) stays green."""
    return degraded and not posted


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


class PassResult(NamedTuple):
    """One agentic pass. `status` is "ok" (usable review text) or a degraded cause:
    "timeout", "error" (pi exited non-zero), or "empty" (exit 0 but no output — a review
    prompt never legitimately yields nothing)."""
    text: str
    status: str
    cost: float = 0.0
    tokens: int = 0


DEGRADED = {"timeout", "error", "empty", "runaway"}


class ReviewOutcome(NamedTuple):
    """Per-PR result: whether a review was posted, and whether any pass (or the head
    checkout itself) degraded."""
    posted: bool
    degraded: bool


# Linux caps a single execve() argument at 128 KiB (MAX_ARG_STRLEN). A large PR's
# diff-bearing prompt blows past that and the pass dies with E2BIG ("[Errno 7]
# Argument list too long") before pi even starts — a hard fail unrelated to the
# review itself. Above this conservative threshold the prompt is PIPED TO pi VIA
# STDIN: pi reads piped (non-TTY) stdin in full and, when no positional message is
# given, uses it verbatim as the initial prompt (main.js `readPipedStdin`;
# initial-message.js joins raw parts) — the model sees a byte-identical prompt to
# the inline-argv path. Deliberately NOT pi's `@file` syntax: pi wraps @file args
# in `<file name="...">...</file>` markup, so large PRs would get a structurally
# different prompt, with the temp path leaked into model context and the prompt
# sitting on disk (agent-readable) for the whole run.
PROMPT_ARG_MAX = int(os.environ.get("PROMPT_ARG_MAX", "100000"))


def run_pass(wt: str, model: str, system: str, user: str, session_dir: str | None = None) -> PassResult:
    if len(system.encode("utf-8", errors="replace")) > PROMPT_ARG_MAX:
        # Operator-supplied GUIDANCE/GUIDANCE_FILE is unbounded and rides argv via
        # --append-system-prompt, so it can independently trip the same E2BIG.
        # Deliberately not clipped (silently truncating guidance would change review
        # behavior) and not rerouted (stdin already carries the user prompt): fail
        # the pass legibly through the normal degraded machinery instead of letting
        # execve die with an opaque OSError.
        log(f"system prompt exceeds PROMPT_ARG_MAX ({len(system)} chars) — refusing pass")
        _annotate("error",
                  "system prompt (GUIDANCE) exceeds PROMPT_ARG_MAX — trim the guidance file")
        return PassResult("", "error")
    if len(user.encode("utf-8", errors="replace")) > PROMPT_ARG_MAX:
        return _run_pass_argv(wt, model, system, prompt_arg=None, stdin_input=user,
                              session_dir=session_dir)
    return _run_pass_argv(wt, model, system, prompt_arg=user, session_dir=session_dir)


_PRICE_CACHE: dict = {}


def _model_prices(model: str) -> dict | None:
    """Per-token OpenRouter list prices {in,out,cache_read,cache_write}, fetched once and
    cached. Returns None if the lookup fails — callers then report tokens only."""
    if model in _PRICE_CACHE:
        return _PRICE_CACHE[model]
    if PROVIDER == "local":
        # Keep PROVIDER=local fully offline (repo invariant): never hit a cloud pricing
        # endpoint. Local inference is free anyway.
        _PRICE_CACHE[model] = None
        return None
    prices = None
    fetched_ok = False
    try:
        base = (os.environ.get("OPENROUTER_BASE_URL", "").strip().rstrip("/")
                or "https://openrouter.ai/api")
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        resp = requests.get(f"{base}/v1/models", headers=headers, timeout=15)
        resp.raise_for_status()
        fetched_ok = True
        for m in resp.json().get("data", []):
            if m.get("id") == model:
                p = m.get("pricing") or {}
                prices = {
                    "in": float(p.get("prompt") or 0),
                    "out": float(p.get("completion") or 0),
                    "cache_read": float(p.get("input_cache_read") or 0),
                    "cache_write": float(p.get("input_cache_write") or 0),
                }
                break
    except Exception:
        fetched_ok = False
    if fetched_ok:
        # Cache a genuine lookup result (including a "no price" → None). A transient fetch
        # failure is left uncached so the long-lived daemon retries next sweep instead of
        # silently dropping USD cost reporting for its whole lifetime.
        _PRICE_CACHE[model] = prices
    return prices


def _cost_from_usage(model: str, usage: dict) -> float:
    """Estimate USD from real pi-session token counts × OpenRouter list pricing. Returns 0
    when pricing is unavailable (merge cost, from OpenRouter's own usage.cost, is authoritative)."""
    prices = _model_prices(model)
    if not prices:
        return 0.0
    return (usage.get("input", 0) * prices["in"]
            + usage.get("output", 0) * prices["out"]
            + usage.get("cache_read", 0) * prices["cache_read"]
            + usage.get("cache_write", 0) * prices["cache_write"])


def _list_session_files(session_dir: str) -> set:
    """Absolute paths of the session JSONL files currently in a dir."""
    if not session_dir or not os.path.isdir(session_dir):
        return set()
    out = set()
    for root, _dirs, files in os.walk(session_dir):
        for fn in files:
            if fn.endswith(".jsonl"):
                out.add(os.path.join(root, fn))
    return out


_REDACTED = "[REDACTED]"
_REDACT_RE = re.compile(r"sk-or-v1-[A-Za-z0-9_-]+")


def _secret_values() -> list:
    vals = [os.environ.get(k, "").strip() for k in ("OPENROUTER_API_KEY", "GITHUB_TOKEN", "GH_TOKEN")]
    return [v for v in vals if v]


def _redact_text(text: str) -> str:
    """Scrub known secret values (and any OpenRouter key shape) from a transcript before it
    is persisted, so a key that an injected agent echoed into a tool result can't be stored."""
    out = text
    for v in _secret_values():
        out = out.replace(v, _REDACTED)
    return _REDACT_RE.sub(_REDACTED, out)


def _redact_transcripts(session_dir: str) -> None:
    """Rewrite persisted session files in-place, redacting secrets."""
    if not session_dir or not os.path.isdir(session_dir):
        return
    for fp in _list_session_files(session_dir):
        try:
            with open(fp, encoding="utf-8") as f:
                data = f.read()
        except OSError:
            continue
        clean = _redact_text(data)
        if clean != data:
            try:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(clean)
            except OSError:
                continue


def _int_or_zero(value: object) -> int:
    """Best-effort numeric parsing for provider/session usage metadata."""
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _float_or_zero(value: object) -> float:
    """Best-effort finite float parsing for provider/session usage metadata."""
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _accumulate_usage(total: dict, usage) -> None:
    """Fold one pi `Usage` record into a running total. Shared by the whole-file reader
    and the watchdog's incremental one so the two cannot drift — pi's schema is camelCase
    (`cacheRead`/`cacheWrite`/`totalTokens`), normalized to snake_case here."""
    if not isinstance(usage, dict):
        return
    input_tokens = _int_or_zero(usage.get("input"))
    output_tokens = _int_or_zero(usage.get("output"))
    cache_read = _int_or_zero(usage.get("cacheRead") or usage.get("cache_read"))
    cache_write = _int_or_zero(usage.get("cacheWrite") or usage.get("cache_write"))
    total["input"] += input_tokens
    total["output"] += output_tokens
    total["cache_read"] += cache_read
    total["cache_write"] += cache_write
    raw_total = usage.get("totalTokens")
    if raw_total is None:
        raw_total = usage.get("total_tokens")
    if raw_total is None:
        total["total_tokens"] += input_tokens + output_tokens + cache_read + cache_write
    else:
        total["total_tokens"] += _int_or_zero(raw_total)
    cost = usage.get("cost")
    if isinstance(cost, dict):
        total["cost_total"] += _float_or_zero(cost.get("total"))


def _read_session_usage(session_dir: str, exclude: set = ()) -> dict:
    """Sum real token usage across the pi session JSONL file(s) in a dir, skipping files
    already present before the pass began (so a pass shares a persisted dir without
    absorbing the cumulative usage of earlier passes).

    pi's session `Usage` uses camelCase keys (`cacheRead`/`cacheWrite`/`totalTokens`);
    these are normalized to snake_case. The authoritative per-message `totalTokens` and
    `cost.total` values are summed when present. Component token counts remain available
    for cost estimation, and are the total-token fallback for older transcripts."""
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
             "total_tokens": 0, "cost_total": 0.0}
    if not session_dir or not os.path.isdir(session_dir):
        return total
    for root, _dirs, files in os.walk(session_dir):
        for fn in files:
            fp = os.path.join(root, fn)
            if not fn.endswith(".jsonl") or fp in exclude:
                continue
            try:
                with open(fp, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        entry = json.loads(line)
                        _accumulate_usage(total, (entry.get("message") or {}).get("usage"))
            except (OSError, ValueError):
                continue
    return total


def _finish_pass(model: str, session_dir: str, internal: bool, text: str, status: str,
                 prior_files: set = ()) -> PassResult:
    """Read the pass's real usage, compute cost, scrub the throwaway session dir if internal,
    and package the PassResult with cost/tokens. `prior_files` = session files that already
    existed before this pass, excluded so a shared persisted dir isn't double-counted."""
    usage = _read_session_usage(session_dir, exclude=prior_files)
    if internal:
        shutil.rmtree(session_dir, ignore_errors=True)
    else:
        # Persisted transcript: scrub secrets before the consumer uploads it as an artifact.
        _redact_transcripts(session_dir)
    tokens = usage.get("total_tokens", 0)
    cost = usage.get("cost_total", 0.0)
    if cost <= 0:
        # pi may not price a custom OpenRouter model (cost.total is 0); fall back to a
        # list-price estimate from real token counts.
        cost = _cost_from_usage(model, usage)
    return PassResult(text, status, cost=cost, tokens=tokens)


class _UsageTail:
    """Usage reader that only parses bytes appended since the last poll.

    The watchdog exists to catch a pass producing a multi-MB transcript, so re-reading it
    from byte 0 every poll would be O(n^2) precisely where it matters — and a parse that
    outran the poll interval would add detection lag on top. Binary mode because text-mode
    `tell()` is unusable after line iteration; a trailing partial line is left unconsumed
    so the next poll sees it whole."""

    def __init__(self, session_dir: str, exclude: set = ()):
        self.dir = session_dir
        self.exclude = set(exclude)
        self.offsets: dict = {}
        self.total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                      "total_tokens": 0, "cost_total": 0.0}

    def poll(self) -> dict:
        if not self.dir or not os.path.isdir(self.dir):
            return dict(self.total)
        for root, _dirs, files in os.walk(self.dir):
            for fn in files:
                fp = os.path.join(root, fn)
                if not fn.endswith(".jsonl") or fp in self.exclude:
                    continue
                try:
                    with open(fp, "rb") as fh:
                        fh.seek(self.offsets.get(fp, 0))
                        raw = fh.read()
                except OSError:
                    continue
                if not raw:
                    continue
                consumed = raw.rfind(b"\n") + 1
                if not consumed:
                    continue
                self.offsets[fp] = self.offsets.get(fp, 0) + consumed
                for line in raw[:consumed].decode("utf-8", "replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    _accumulate_usage(self.total, (entry.get("message") or {}).get("usage"))
        return dict(self.total)


def _spend_note(usage: dict, model: str) -> str:
    """` · N tok · $C` for a degraded annotation. "timed out after 1800s" reads the same
    for a pass working flat out, one hung at 30 tok/s, and one looping at 7000 tok/s —
    the spend is what tells them apart, and it is already on disk."""
    tok = usage.get("total_tokens", 0)
    cost = usage.get("cost_total", 0.0) or _cost_from_usage(model, usage)
    if not tok and not cost:
        return ""
    bits = f"{tok:,} tok" if tok else ""
    if cost:
        bits += f" · ${cost:.4f}" if bits else f"${cost:.4f}"
    return f" · {bits}"


def _budget_breach(usage: dict, model: str) -> str:
    """Why this pass has blown its spend ceiling, or "" if it hasn't."""
    tok = usage.get("total_tokens", 0)
    if MAX_PASS_TOKENS and tok > MAX_PASS_TOKENS:
        return f"{tok:,} tokens over the {MAX_PASS_TOKENS:,} token ceiling"
    if MAX_PASS_COST_USD:
        cost = usage.get("cost_total", 0.0) or _cost_from_usage(model, usage)
        if cost > MAX_PASS_COST_USD:
            return f"${cost:.4f} over the ${MAX_PASS_COST_USD:.2f} ceiling"
    return ""


def _run_pass_argv(wt: str, model: str, system: str, prompt_arg: str | None,
                   stdin_input: str | None = None, session_dir: str | None = None) -> PassResult:
    flags = list(PI_FLAGS)
    internal = False
    if session_dir:
        # Caller-supplied per-pass session dir (e.g. a pass-N dir under PI_SESSION_DIR for
        # parallel passes) — persisted, never cleaned up here.
        session_dir = os.path.abspath(session_dir)
        os.makedirs(session_dir, exist_ok=True)
        flags += ["--session-dir", session_dir]
    else:
        session_dir = os.environ.get("PI_SESSION_DIR", "").strip()
        if session_dir:
            # pi runs with cwd=wt below, while this process creates/reads the transcript dir
            # from its own cwd. Normalize once so a relative PI_SESSION_DIR names the same
            # directory for both processes and survives removal of the temporary worktree.
            session_dir = os.path.abspath(session_dir)
            os.makedirs(session_dir, exist_ok=True)
            flags += ["--session-dir", session_dir]
        else:
            # Always write a session (to a throwaway dir) so the pass's real token usage/cost
            # is readable and the transcript is recoverable on a crash; scrubbed afterward
            # when not persisted. Transcripts are kept for replay only when PI_SESSION_DIR
            # points where the consumer persists them (e.g. an action artifact).
            internal = True
            session_dir = tempfile.mkdtemp(prefix="so-session-")
            flags += ["--session-dir", session_dir]
    cmd = (["pi", "--provider", PI_PROVIDER, "--model", model] + flags
           + ["--tools", TOOLS, "--append-system-prompt", system, "-p"])
    if prompt_arg is not None:
        cmd.append(prompt_arg)
    # Defense-in-depth: don't hand the agent's shell the GitHub token. pi reaches the
    # provider via the key in models.json; GH_TOKEN/GITHUB_TOKEN are for _gh() only, so
    # drop them here — a bash-tool prompt-injection can't exfiltrate the token that posts
    # comments / reads the repo. (Not a full sandbox: the OpenRouter key still lives in
    # models.json — chmod 600'd by providers.py — and the worktree's git config can hold
    # the checkout token. Trusting PR authors is the real boundary; see README Security.)
    env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
    prior_files = _list_session_files(session_dir)
    proc = None
    try:
        # Popen rather than subprocess.run so a watchdog can watch the pass WHILE it runs.
        # The watchdog is a thread, not a communicate() poll loop: communicate() accepts
        # its input exactly once and the oversized-prompt path pipes the diff through
        # stdin, so re-entering it after a timeout would be ill-defined. The thread only
        # observes and kills; the normal call below is unchanged.
        proc = subprocess.Popen(
            cmd, cwd=wt, env=env, text=True,
            stdin=subprocess.PIPE if stdin_input is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        breach = {"why": ""}
        stop = threading.Event()
        tail = _UsageTail(session_dir, prior_files)

        def _watch() -> None:
            while not stop.wait(BUDGET_POLL_S):
                try:
                    why = _budget_breach(tail.poll(), model)
                except Exception:  # noqa: BLE001 — a transient read must not kill a pass
                    continue
                if why and proc.poll() is None:
                    # Guard on "still running": a pass that finished naturally can have
                    # final usage above the ceiling, and setting breach then would discard
                    # a perfectly good review. kill() is a no-op on a reaped process, so
                    # without this the flag alone would misreport it as a runaway.
                    breach["why"] = why
                    proc.kill()
                    return

        if MAX_PASS_COST_USD and not MAX_PASS_TOKENS and _model_prices(model) is None:
            # "I set a spend ceiling, so I'm protected" is the false assurance to avoid.
            # PROVIDER=local never prices (the offline invariant), and a failed OpenRouter
            # lookup is deliberately left uncached — either way cost stays 0.0 and the
            # ceiling can never trip, leaving the pass bounded only by the clock.
            _annotate("warning",
                      f"cost ceiling INACTIVE: no pricing available for {model}, so "
                      f"MAX_PASS_COST_USD can never trip and this pass is bounded only "
                      f"by the clock. Set MAX_PASS_TOKENS instead.")
        if MAX_PASS_TOKENS or MAX_PASS_COST_USD:
            threading.Thread(target=_watch, daemon=True).start()
        try:
            # input=None keeps today's inherited-stdin behavior for the inline path;
            # a str pipes it (non-TTY), which pi reads as the verbatim initial prompt.
            out, err = proc.communicate(input=stdin_input, timeout=PASS_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            proc.kill()
            try:
                out, err = proc.communicate()
            except Exception:  # noqa: BLE001
                # TimeoutExpired carries BYTES on POSIX even under text=True, and _peek
                # does " ".join(x.split()) — bytes there is a TypeError that would crash
                # the pass instead of returning a clean degraded "timeout".
                out, err = _as_text(e.output), _as_text(e.stderr)
            # The exception carries the partial output captured before the kill (stdout in
            # `output`, stderr in `stderr`) — surface it so a blocked pass is diagnosable
            # from the log, not a silent black box.
            # NOT `tail`: that name is the _UsageTail the watchdog closure captures,
            # and rebinding it to a string would leave a future reordering with a
            # watchdog that silently stops working.
            peek = _peek(err or out or "")
            note = f" — partial output: {peek}" if peek else ""
            spend = _spend_note(_read_session_usage(session_dir, exclude=prior_files), model)
            log(f"pi pass timed out after {PASS_TIMEOUT_S}s{spend}{note}")
            _annotate("warning",
                      f"pi pass timed out after {PASS_TIMEOUT_S}s{spend} — no review produced{note}")
            return _finish_pass(model, session_dir, internal, "", "timeout", prior_files)
        finally:
            stop.set()
        # Belt and braces with the watchdog's poll() guard: classify as runaway only if
        # the process really was killed (negative returncode), never if it exited on its
        # own between the last poll and communicate() returning.
        if breach["why"] and (proc.returncode or 0) < 0:
            # Killed for spend, not time. Reported as its own cause so the three failure
            # modes stop being indistinguishable in the checks UI.
            # Only promise evidence that will actually exist. With no session-dir
            # configured, internal=True and _finish_pass deletes the throwaway transcript
            # a line later — pointing the operator at it would recreate the #29 failure
            # this feature exists to fix ("the numbers survived, the evidence did not"),
            # for every consumer who sets a ceiling without also setting session-dir.
            evidence = ("see the retained session transcript for what it was looping on"
                        if not internal else
                        "no transcript was retained — set `session-dir` to capture what "
                        "it loops on next time")
            spend = _spend_note(_read_session_usage(session_dir, exclude=prior_files), model)
            log(f"pi pass aborted — {breach['why']}")
            _annotate("warning",
                      f"pi pass aborted: {breach['why']}{spend} — no review produced. "
                      f"A longer timeout would only raise the bill; {evidence}.")
            return _finish_pass(model, session_dir, internal, "", "runaway", prior_files)
        if proc.returncode != 0:
            # Surface the failure (bad key, 402 out-of-credits, unknown model id, server
            # 4xx/OOM) instead of leaving only a "0c" line. Partial stdout from a crash isn't
            # trustworthy. The annotation carries WHY (e.g. the 402 message) to the operator.
            detail = " ".join((err or out or "").split())[:200]
            spend = _spend_note(_read_session_usage(session_dir, exclude=prior_files), model)
            log(f"pi pass exited {proc.returncode}{spend}: {detail}")
            _annotate("error", f"pi exited {proc.returncode}{spend} — {detail[:150]}")
            return _finish_pass(model, session_dir, internal, "", "error", prior_files)
        text = (out or "").strip()
        if not text:
            # Exit 0 with nothing to say: the model returned no review at all. Treat as a
            # degraded pass, not a clean bill of health. A silent pass can still carry a tale
            # in stderr (a provider warning, an empty assistant message pi relayed) — surface
            # it so the failure isn't a black box.
            note = f" — stderr: {_peek(err)}" if (err or "").strip() else ""
            spend = _spend_note(_read_session_usage(session_dir, exclude=prior_files), model)
            log(f"pi pass exited 0 but produced no review output{spend}{note}")
            _annotate("warning",
                      f"pass completed but produced no review output{spend}{note}")
            return _finish_pass(model, session_dir, internal, "", "empty", prior_files)
        return _finish_pass(model, session_dir, internal, text, "ok", prior_files)
    finally:
        # subprocess.run wrapped its Popen in a `with` and killed the child on ANY
        # exception; a raw Popen does not, and that regression is worse here than
        # elsewhere: the inner `finally: stop.set()` has already retired the watchdog by
        # the time an exception reaches this point, so an orphaned pi would keep burning
        # tokens with no ceiling at all — the exact failure this feature exists to bound.
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
            try:
                proc.wait(timeout=10)   # reap, so no zombie is left behind
            except Exception:  # noqa: BLE001
                pass
        # Safety net for the paths _finish_pass never reaches. Redaction lives ONLY
        # in _finish_pass, which every normal return goes through — but an exception
        # escaping this function (a UnicodeDecodeError out of text=True on non-UTF-8
        # stderr, an OSError, anything not TimeoutExpired) would leave pi's partial
        # JSONL RAW on disk. A consumer persisting session-dir may then publish it,
        # and that failure surfaces as a FAILED job, not a cancelled one, so a
        # workflow-side `!cancelled()` guard does not cover it. Both operations are
        # idempotent, so repeating them after a normal return is harmless.
        if internal:
            shutil.rmtree(session_dir, ignore_errors=True)
        else:
            _redact_transcripts(session_dir)


def _chat(base_url: str, api_key: str, model: str, prompt: str, meta: dict | None = None) -> str:
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
              "temperature": 0.3, "max_tokens": 16384,
              # Reasoning-capable models (deepseek-v4-flash included) may spend
              # the entire max_tokens budget in the reasoning channel and return
              # an EMPTY `content` on a 200 — observed 3/3 on the spaghettio
              # #565/#566 merge calls, 2026-08-01, which surfaced as "merge
              # returned no usable content" failing the required check. The
              # merge is a mechanical union task that gains nothing from
              # reasoning; disable it. Non-reasoning models and llama-server's
              # OpenAI-compat endpoint ignore the field.
              "reasoning": {"enabled": False}},
        timeout=600,
    )
    r.raise_for_status()
    try:
        payload = r.json()
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    choices = payload.get("choices") or []
    if not isinstance(choices, list):
        choices = []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    msg = choice.get("message") or {}
    if not isinstance(msg, dict):
        msg = {}
    raw_content = msg.get("content")
    content = raw_content.strip() if isinstance(raw_content, str) else ""
    if not content and choices:
        # Diagnose the empty-content-200 shape instead of failing mute:
        # finish_reason + reasoning length distinguish the reasoning-burn
        # failure mode from a genuinely empty reply.
        log(f"_chat: empty content on 200 — finish_reason="
            f"{choice.get('finish_reason')!r}, "
            f"reasoning_len={len(msg.get('reasoning') or '')}")
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    if meta is not None:
        meta["cost"] = _float_or_zero(usage.get("cost"))
        meta["tokens"] = _int_or_zero(usage.get("total_tokens"))
        ctd = usage.get("completion_tokens_details") or {}
        if not isinstance(ctd, dict):
            ctd = {}
        meta["reasoning_tokens"] = _int_or_zero(ctd.get("reasoning_tokens"))
    return content


def merge_reviews(pr: int, title: str, passes: list[str], merge_model: str | None = None, meta: dict | None = None) -> str:
    """Union the K passes via one merge call (only used when K>1). Never raises.

    The passes ARE the review; the merge is editorial. Losing K good reviews because the
    editorial step flaked is the wrong failure mode — under `fail-on-degraded` it reds the
    check with nothing to show for it (observed live on spaghettio#561: 3/3 passes produced
    reviews, 4240/4959/4275 chars, then the merge returned an empty-content 200 and the
    whole run exited 2).

    Disabling reasoning on the merge call removed the *known* cause of those empty 200s,
    but it cannot make a hosted call infallible — it can still flake empty or raise. So:
    retry once, in the spirit of the retry `eval.py`'s judge has had since #9 (broader
    here: eval's judge retries only on unusable content and lets a raised HTTPError
    propagate, while this must not raise at all), then fall back to posting the raw passes
    unmerged. A merge-step outage now degrades formatting, never delivery, and the
    `::warning` annotation keeps the malfunction visible instead of trading a red check
    for a silent one."""
    merge_model = merge_model or MERGE_MODEL or MODEL
    passes_block = "\n\n".join(
        f"=== PASS {i+1} of {len(passes)} (independent) ===\n{p}"
        for i, p in enumerate(passes))
    prompt = MERGE_PROMPT.format(pr=pr, title=title, passes_block=passes_block)
    failures = []
    for attempt in (1, 2):
        attempt_meta: dict = {}
        try:
            if MERGE_PROVIDER == "local":
                out = _chat(LLAMA_SERVER_URL, "", merge_model, prompt, attempt_meta)
            else:
                key = os.environ.get("OPENROUTER_API_KEY", "").strip()
                out = _chat(OPENROUTER_BASE, key, merge_model, prompt, attempt_meta)
        # Deliberately broad: this function's contract is that it never raises, so any
        # exception has to become a fallback rather than an escape. It is not swallowed
        # silently — the type and message go into the annotation below, so a genuine bug
        # (say an AttributeError from a bad refactor) reads as "raised AttributeError",
        # not as a model flake.
        except Exception as e:  # noqa: BLE001
            # Bind to a second name: Python unbinds the `as` target at block exit.
            out, err = "", e
        else:
            err = None
        # A flaked attempt still bills for the tokens it burned (a reasoning-burn empty is
        # the *expensive* failure), so accumulate rather than overwrite: reporting only the
        # winning attempt would understate real spend on exactly the runs that cost most.
        if meta is not None:
            for field, value in attempt_meta.items():
                meta[field] = meta.get(field, 0) + value
        if out:
            return out
        reason = (f"raised {type(err).__name__}: {str(err)[:120]}" if err is not None
                  else "returned no usable content")
        failures.append(reason)
        log(f"merge ({MERGE_PROVIDER}/{merge_model}) attempt {attempt}/2 {reason}")
    # Report BOTH reasons, not just the last. The attempts can fail differently, and the
    # distinction is the operator-actionable part: a 402 on attempt 1 followed by an empty
    # 200 on attempt 2 is credits exhaustion — a persistent condition that will recur every
    # sweep — but collapsing to the last reason would file it under "the model flaked".
    detail = "; ".join(f"attempt {i+1} {r}" for i, r in enumerate(failures))
    _annotate("warning",
              f"union merge failed twice ({detail}) — posting {len(passes)} raw passes unmerged")
    # Tell the caller the passes were never unioned, so the posted header can say so rather
    # than claiming a "union ×K" that did not happen.
    if meta is not None:
        meta["merged"] = False
    parts = [f"*(union merge unavailable — the {len(passes)} independent passes follow "
             "unmerged; findings may repeat or disagree between passes)*"]
    parts += [f"### Pass {i+1} of {len(passes)}\n\n{p}" for i, p in enumerate(passes)]
    return "\n\n---\n\n".join(parts)


# An agent read tool truncates on TWO independent limits, whichever hits first — pi's are
# DEFAULT_MAX_LINES = 2000 and DEFAULT_MAX_BYTES = 50 * 1024. Both matter: a line-dense
# diff can sit under the byte cap and still be cut short, so checking bytes alone would
# advertise "fits in a single read" for a file the agent only partly receives — the same
# inaccuracy this machinery exists to prevent, in the code meant to prevent it.
AGENT_READ_BYTES = 50 * 1024
AGENT_READ_LINES = 2000


# write_full_diff prepends an explanatory header. Both the prompt and that header must
# reach the SAME paging verdict, so both budget for it — otherwise a diff sitting just
# under either cap flips over it once the header lands, and the file claims to fit in one
# read while the agent receives all but the tail.
FULL_DIFF_HEADER_PAD_BYTES = 600
FULL_DIFF_HEADER_PAD_LINES = 10


def exceeds_one_read(text: str, pad_bytes: int = 0, pad_lines: int = 0) -> bool:
    """True when an agent's single `read` cannot return all of `text` (plus any padding
    the caller knows will be prepended)."""
    return (_bytes(text) + pad_bytes > AGENT_READ_BYTES
            or text.count("\n") + pad_lines > AGENT_READ_LINES)


def _needs_paging(fd: rv.FilteredDiff) -> bool:
    """The single paging verdict, shared by the prompt and the on-disk header."""
    return exceeds_one_read(fd.full_text, FULL_DIFF_HEADER_PAD_BYTES,
                            FULL_DIFF_HEADER_PAD_LINES)


def coverage_phrase(fd: rv.FilteredDiff) -> str:
    """One clause describing what the excerpt actually holds. Every shape truncation takes
    gets its own words: files wholly absent, a file cut mid-hunk, and a file present but
    missing a later hunk are three different things and must not read alike."""
    shown = len(dict.fromkeys(fd.files))
    total = shown + len(fd.missing_files)
    head = (f"covers {shown} of {total} changed file(s)" if fd.missing_files
            else f"holds all {total} changed file(s)")
    caveats = []
    if fd.clipped:
        caveats.append(f"{fd.clipped} is cut off mid-file")
    caveats += [f"a later hunk of {p} is missing" for p in fd.partial_files]
    return head + (" — " + "; ".join(caveats) if caveats else "")


def write_full_diff(fd: rv.FilteredDiff, wt: str, pr: int) -> str:
    """Drop the complete diff into the agent's working directory. Returns the filename to
    point at, or "" if it could not be written (callers must then stop claiming it exists).

    Ordered missing-files-first, smallest-first: whenever the file needs paging (the
    usual case — see _needs_paging) git path order would make the first read return what
    the excerpt already carried. When nothing is wholly missing the order is left alone — the material the
    agent lacks is then the TAIL, and promoting anything would point it the wrong way."""
    ordered = rv.reorder_unseen_first(fd.full_text, fd.missing_files)
    shown = len(dict.fromkeys(fd.files))
    lines = [f"# Full diff for PR #{pr} — placed here by second-opinion; NOT part of the PR.\n"]
    if fd.missing_files:
        lines.append(f"# The prompt excerpt carried {shown} of "
                     f"{shown + len(fd.missing_files)} changed files.\n"
                     f"# The {len(fd.missing_files)} it did NOT carry are ordered FIRST below,\n"
                     f"# smallest first: reading from the TOP gives you material the excerpt\n"
                     f"# lacked.\n")
    else:
        tail_hint = ("# is the TAIL. Page DOWN to reach it.\n" if _needs_paging(fd)
                     else "# is at the END of this file.\n")
        # No "page down" when the whole file comes back in one read — the prompt says it
        # fits, and a header telling it to page would contradict that in the same breath.
        lines.append("# The excerpt carried every changed file, but is cut short. Order here is\n"
                     "# unchanged, so the TOP repeats what you already saw — what you are missing\n"
                     + tail_hint)
    if _needs_paging(fd):
        lines.append("# This file is larger than a single read returns.\n")
    try:
        with open(os.path.join(wt, FULL_DIFF_NAME), "w", encoding="utf-8") as fh:
            fh.write("".join(lines) + "\n" + ordered)
    except OSError as e:
        # Deliberately silent: the caller emits exactly one annotation describing both the
        # coverage and the write outcome. Annotating here too produced a contradictory pair.
        log(f"#{pr}: could not write the full diff — {' '.join(str(e).split())[:120]}")
        return ""
    return FULL_DIFF_NAME


def truncation_notice(fd: rv.FilteredDiff, full_diff_rel: str) -> str:
    """The prompt suffix telling the agent what the excerpt is missing and how to reach it.

    Disclosure and pointer are separate: whether the on-disk copy exists changes what the
    agent can DO about truncation, never whether it needs to know it happened."""
    if not fd.truncated:
        return ""
    out = [f"\nIMPORTANT — the diff above is TRUNCATED: it {coverage_phrase(fd)}. "]
    if full_diff_rel:
        out.append(f"The COMPLETE diff is in your working directory at `./{full_diff_rel}` "
                   f"(it is not part of the PR — it was placed there for you). ")
        if _needs_paging(fd):
            out.append("It is LARGER than a single read returns, so ")
            out.append(f"the {len(fd.missing_files)} file(s) absent from the excerpt are "
                       f"ordered FIRST in it: read from the TOP, then page onwards"
                       if fd.missing_files else
                       "page DOWN — what you are missing is at the END; the top just "
                       "repeats what you already saw")
            if "bash" in TOOLS:
                out.append(f" (or `grep -n '^diff --git' {full_diff_rel}` to jump to a file)")
            out.append(". One read of that file is NOT the whole diff — do not treat it as such. ")
        else:
            out.append("It fits in a single read. ")
    else:
        out.append("The complete diff could NOT be provided this run. The repository is "
                   "checked out at the reviewed commit, so read the files named below to see "
                   "their current state — but you cannot diff them against the base (the "
                   "checkout is shallow and the base ref is absent), so report your view of "
                   "them as partial rather than implying you reviewed the change. ")
    out.append("Treat the excerpt above as a starting point, not the change. Prioritise "
               "source files over generated artifacts.")
    if fd.missing_files:
        listed = "".join(f"  - {p}\n" for p in fd.missing_files[:60])
        if len(fd.missing_files) > 60:
            listed += f"  - ... and {len(fd.missing_files) - 60} more\n"
        out.append(f" Files changed by this PR but absent from the excerpt:\n{listed}")
    return "".join(out)


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _clip_utf8(s: str, max_bytes: int) -> str:
    """Truncate to at most `max_bytes` UTF-8 bytes without splitting a character."""
    if max_bytes <= 0:
        return ""
    raw = s.encode("utf-8")
    if len(raw) <= max_bytes:
        return s
    # errors="ignore" drops a trailing partial sequence rather than raising.
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _clip_review_body(review_body: str, reserved: int, limit: int = COMMENT_MAX) -> str:
    """Trim the review body so the assembled comment fits under GitHub's comment cap.

    Losing the tail of a long review is a bad outcome; losing the *whole* review to a 422
    is a worse one, and that is the silent-failure class this reviewer exists to avoid.
    Only the body is clipped — the marker leads the comment and idempotency is a
    `startswith` match, so dedup keeps working — and `reserved` holds room for the header
    and footer so the cost line survives too.

    Budgeted in **UTF-8 bytes**, not code points. GitHub documents the cap in
    "characters" without pinning the unit, and the comment is not ASCII: the header alone
    carries `🤖`, `—` and `×` (48 code points, 54 bytes), and findings routinely add more
    emoji. For every string, UTF-8 bytes >= UTF-16 units >= code points, so a body that
    fits the byte budget fits under all three readings — whereas budgeting by code points
    would be correct only under the most generous one, with zero slack to absorb being
    wrong."""
    room = limit - reserved
    notice = "\n\n*(review truncated to fit GitHub's comment size limit — see the run log)*"
    if room <= 0:
        # Pathological: header+footer alone meet or exceed the cap, so the assembled
        # comment is over-cap no matter what happens to the body. Returning "" does not
        # rescue that — nothing here can — it just avoids making it worse. Unreachable
        # from review_pr, where the shell and footer are ~380 bytes against a 65536 cap.
        return ""
    if _bytes(review_body) <= room:
        return review_body
    keep = room - _bytes(notice)
    if keep <= 0:
        # `room` is positive but too small for even the notice. Unreachable from review_pr
        # (the header and footer are ~380 bytes against a 65536 cap), but the helper takes
        # a general `limit`, so keep it correct in isolation: return at most `room` bytes.
        return _clip_utf8(notice.strip(), room)
    log(f"review body {_bytes(review_body)}B exceeds the comment cap — clipping to {keep}B")
    _annotate("warning",
              f"review body clipped to fit GitHub's {limit}-char comment limit "
              f"({_bytes(review_body)}B → {keep}B) — the tail is in the run log/artifacts")
    return _clip_utf8(review_body, keep).rstrip() + notice


def _cost_footer(total_cost: float, tokens: int, estimated: bool) -> str:
    """Append a cost/token line to a posted review, or '' when nothing was spent."""
    if total_cost <= 0 and tokens <= 0:
        return ""
    cost_bit = ""
    if total_cost > 0:
        approx = "≈" if estimated else ""
        cost_bit = f" · {approx}${total_cost:.4f}"
    return f"\n\n---\n\n<sub>*Review cost{cost_bit} · {tokens:,} tokens total.*</sub>\n"


def _failure_notice_text(sha: str, checkout: bool = False) -> str:
    """Markdown for the notice posted when a review produces no output. Carries a distinct
    FAIL_MARKER so `_already_noticed_failure` can dedup daemon sweeps without colliding with
    the success marker (a later retry/push on a new SHA still gets a real review)."""
    text = FAIL_MARKER.format(sha=sha) + "\n\n"
    if checkout:
        text += "### :warning: Second opinion — PR head could not be reviewed\n\n"
        text += ("The PR head commit could not be checked out, so no review ran. "
                 "This is a reviewer malfunction, not a clean bill of health.\n")
    else:
        text += "### :warning: Second opinion — review produced no output\n\n"
        text += ("The review passes all came back empty (degraded): no findings were posted. "
                 "This is a reviewer malfunction, not a clean bill of health.\n")
    try:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        rid = os.environ.get("GITHUB_RUN_ID", "")
        if REPO and rid:
            text += f"\n- **Run log + artifacts:** {server}/{REPO}/actions/runs/{rid}\n"
    except Exception:
        pass
    if FAIL_ON_DEGRADED:
        text += ("\n*In one-shot mode this malfunction fails the check so it is not mistaken "
                 "for a passing review. Re-run the job or push a commit to retry.*\n")
    else:
        text += ("\n*`FAIL_ON_DEGRADED=false`, so this malfunction does not fail the check. "
                 "Re-run the job or push a commit to retry.*\n")
    return text


def _already_noticed_failure(n: int, sha: str) -> bool:
    """True if a failure notice already exists for this exact SHA (dedups daemon sweeps)."""
    marker = FAIL_MARKER.format(sha=sha)
    jq = f".[] | select(.body | startswith({json.dumps(marker)}))"
    out = _gh(["api", f"repos/{REPO}/issues/{n}/comments", "--paginate", "--jq", jq], timeout_s=120)
    return out.strip() != ""


def _post_failure_notice(pr: int, sha: str, dry_run: bool, checkout: bool = False) -> None:
    text = _failure_notice_text(sha, checkout)
    if dry_run:
        print("\n" + "=" * 72 + f"\nDRY RUN — would post failure notice to #{pr}:\n"
              + "=" * 72 + f"\n{text}\n")
        return
    try:
        already_noticed = _already_noticed_failure(pr, sha)
    except Exception as e:  # a failed dedup read must not suppress the more important notice
        log(f"#{pr}: could not check for an existing failure notice "
            f"({' '.join(str(e).split())[:120]}) — attempting to post")
        already_noticed = False
    if already_noticed:
        log(f"#{pr}: failure notice for {sha[:10]} already posted — skipping duplicate")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
        tmp = f.name
    try:
        _gh(["pr", "comment", str(pr), "--body-file", tmp])
        log(f"#{pr}: posted degraded-review failure notice")
    except Exception:
        log(f"#{pr}: failed to post failure notice — review remains degraded")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def review_pr(pr: int, title: str, sha: str, model: str, merge_model: str, dry_run: bool) -> ReviewOutcome:
    diff = _gh(["pr", "diff", str(pr)])
    globs = _exclude_globs()
    fd = rv.filter_diff(diff, globs, MAX_DIFF_CHARS)
    filtered = fd.text
    if not filtered.strip():
        log(f"#{pr}: empty filtered diff — skipping")
        return ReviewOutcome(False, False)

    system = rv.system_prompt(PROJECT, _guidance())
    _git(["fetch", "-q", "origin", f"refs/pull/{pr}/head"], check=False)
    wt = os.path.join(tempfile.gettempdir(), f"second-opinion-pr{pr}")
    # Set after the worktree exists (write_full_diff needs it); user_turn closes over the
    # name and is only called further down, so it sees the post-write value.
    full_diff_rel = ""

    def user_turn(diff_text: str) -> str:
        return (f"PR #{pr}: {title}\n\nThe full repository is checked out in your working "
                f"directory at the PR's head commit. Use your tools (read, grep via bash) "
                f"to inspect callers, tests, and definitions as needed. The change to "
                f"review is this diff:\n\n{diff_text}\n"
                + truncation_notice(fd, full_diff_rel))

    _git(["worktree", "remove", "--force", wt], check=False)
    add = _git(["worktree", "add", "--detach", "--force", wt, sha], check=False)
    if add.returncode != 0:
        # The reviewer never ran — the same silent-failure class as a degraded pass
        # (the PR stays unreviewed), so it must trip the tripwire, not slip out green.
        detail = " ".join((add.stderr or "").split())[:120]
        log(f"#{pr}: worktree add failed @ {sha[:10]}: {detail}")
        _annotate("error", f"#{pr}: worktree add failed @ {sha[:10]} — {detail} — no review produced")
        _post_failure_notice(pr, sha, dry_run, checkout=True)
        return ReviewOutcome(False, True)

    passes: list[str] = []
    degraded = False
    total_cost = 0.0
    total_tokens = 0
    try:
        # Inside the try: log()/_annotate() print, and a BrokenPipeError here
        # would otherwise escape review_pr with the worktree still on disk.
        if fd.truncated:
            # Write FIRST, then report, so there is exactly one annotation and it describes
            # what actually happened. Announcing "supplied on disk" and then failing to write
            # it would be two contradictory warnings, and a reader believes the optimistic one.
            full_diff_rel = write_full_diff(fd, wt, pr)
            excerpt = len(filtered)
            if filtered.endswith(rv.TRUNCATION_TRAILER):
                excerpt -= len(rv.TRUNCATION_TRAILER)
            pct = 100 * excerpt // max(1, len(fd.full_text))
            tail = ("full diff supplied on disk, but coverage of the remainder depends on the "
                    "agent reading it" if full_diff_rel else
                    "and the full diff could NOT be written — this review covers the excerpt ONLY")
            listed = (". Not in the excerpt: "
                      + ", ".join(fd.missing_files[:10])
                      + (f", +{len(fd.missing_files) - 10} more" if len(fd.missing_files) > 10
                         else "")) if fd.missing_files else ""
            log(f"#{pr}: diff truncated — excerpt {coverage_phrase(fd)} "
                f"({pct}% by size); "
                f"{'full diff at ' + FULL_DIFF_NAME if full_diff_rel else 'WRITE FAILED'}")
            _annotate("warning",
                      f"#{pr}: prompt excerpt {coverage_phrase(fd)} "
                      f"({pct}% of the filtered diff by size) — {tail}{listed}")
        msgs = [user_turn(filtered if i == 0 else rv.shuffle_inputs(filtered, i))
                for i in range(K)]
        elapsed: dict = {}
        if K > 1 and PROVIDER == "openrouter":
            # Parallel passes — the wall-clock win for hosted providers: K pi subprocesses
            # run at once, so K×timeout collapses to roughly one timeout. Each pass gets its
            # own session subdir (a pass-N dir under PI_SESSION_DIR when persisting, else a
            # throwaway temp dir) so concurrent transcripts never collide. Local llama stays
            # sequential: a single GPU serves one request at a time, so parallelism buys
            # nothing and could overload the server.
            session_base = os.environ.get("PI_SESSION_DIR", "").strip()
            pass_dirs = ([os.path.join(session_base, f"pass-{i+1}") for i in range(K)]
                         if session_base else [None] * K)
            started = {i: time.monotonic() for i in range(K)}
            results: dict = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=K) as ex:
                fut_to_i = {ex.submit(run_pass, wt, model, system, msg, sdir): i
                            for i, (msg, sdir) in enumerate(zip(msgs, pass_dirs))}
                for fut in concurrent.futures.as_completed(fut_to_i):
                    i = fut_to_i[fut]
                    results[i] = fut.result()
                    elapsed[i] = time.monotonic() - started[i]
            ordered = [results[i] for i in range(K)]
        else:
            ordered = []
            for i, msg in enumerate(msgs):
                t0 = time.monotonic()
                ordered.append(run_pass(wt, model, system, msg))
                elapsed[i] = time.monotonic() - t0
        for i, result in enumerate(ordered):
            total_cost += result.cost
            total_tokens += result.tokens
            log(f"#{pr}: pass {i+1}/{K} — {len(result.text)}c · "
                f"{result.tokens:,} tok · ${result.cost:.4f} in {elapsed.get(i, 0):.0f}s")
            if result.status in DEGRADED:
                degraded = True
            if result.text:
                passes.append(result.text)
    finally:
        _git(["worktree", "remove", "--force", wt], check=False)

    if not passes:
        # Degraded with no output: still flag it loudly — but make the failure visible on the
        # PR with a comment + a link to the run log/artifacts, instead of posting nothing at
        # all. The notice is NOT a review, so the configurable degraded tripwire still applies.
        log(f"#{pr}: all passes empty — posting failure notice")
        _post_failure_notice(pr, sha, dry_run)
        return ReviewOutcome(False, True)

    k = len(passes)
    merged = True
    if k == 1:
        review_body = passes[0]
    else:
        mm: dict = {}
        review_body = merge_reviews(pr, title, passes, merge_model, meta=mm)
        total_cost += mm.get("cost", 0.0)
        total_tokens += mm.get("tokens", 0)
        merged = mm.get("merged", True)
    if k == 1:
        pass_label = "single pass"
    else:
        # Don't claim a union that didn't happen — on the merge-fallback path the passes
        # are posted raw, so the header says so instead of advertising "union ×K".
        pass_label = f"union ×{k}" if merged else f"×{k} unmerged"
    # Pass-derived costs are list-price estimates (pi does not price custom OpenRouter
    # models), so label the total as an estimate regardless of whether a pass degraded.
    footer = _cost_footer(total_cost, total_tokens, estimated=True)
    if fd.truncated:
        # Name what the excerpt actually held. "coverage is partial" reads identically at
        # 1-of-16 and 15-of-16, and the on-disk claim is gated on full_diff_rel — the
        # honest signal — so a failed write cannot leave the footer advertising coverage
        # the agent never had.
        if full_diff_rel:
            footer += (f"\n\n*(prompt excerpt {coverage_phrase(fd)}; the full diff was "
                       f"supplied to the agent on disk. Coverage of the remainder depends "
                       f"on it having read that.)*")
        else:
            footer += (f"\n\n*(prompt excerpt {coverage_phrase(fd)}, and the full diff "
                       f"could not be supplied — this review covers that excerpt **only**.)*")
    marker = MARKER.format(sha=sha)
    shell = HEADER.format(marker=marker, pass_label=pass_label, model=model, body="")
    # Reserve in bytes, matching _clip_review_body's budget — the shell is not ASCII.
    review_body = _clip_review_body(review_body, _bytes(shell) + _bytes(footer))
    body = HEADER.format(marker=marker, pass_label=pass_label,
                         model=model, body=review_body) + footer

    if dry_run:
        print("\n" + "=" * 72 + f"\nDRY RUN — would post to #{pr}:\n" + "=" * 72 + f"\n{body}\n")
        return ReviewOutcome(True, degraded)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp = f.name
    try:
        _gh(["pr", "comment", str(pr), "--body-file", tmp])
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # cleanup only — must not escape and turn a posted review into exit 2
    log(f"#{pr}: posted {pass_label} review ({model})")
    return ReviewOutcome(True, degraded)


def sweep(args: argparse.Namespace) -> bool:
    """One pass over candidate PRs: resolve the model, register it with pi, review.
    Returns True if any PR ended in a silent failure (a degraded pass — or an unhandled
    review error — that posted no review); the single-shot caller turns that into a
    non-zero exit. Never aborts the sweep early: every candidate PR is still processed."""
    model = resolve_model()
    if model is None:
        return False
    write_models_json(model)  # register the provider's model with pi
    merge_model = _merge_model_for(model)

    targets = [pr_meta(args.pr)] if args.pr else open_prs()
    merge_desc = f"{MERGE_PROVIDER}:{merge_model}" if K > 1 else "n/a (K=1)"
    log(f"second opinion · provider={PROVIDER} · model={model} · K={K} · merge={merge_desc} "
        f"· {len(targets)} candidate PR(s)")
    for problem in _CONFIG_ERRORS:
        # Surfaced here rather than at import: _annotate is defined below the constants,
        # and a misconfigured ceiling must not be a silent no-op.
        log(f"config: {problem}")
        _annotate("error", f"config: {problem}")
    silent_failure = False
    for t in targets:
        n, sha, title = t["number"], t["headRefOid"], t["title"]
        if t.get("isDraft") and not args.force:
            log(f"#{n}: draft — skipping (use --force to override)")
            continue
        if not args.force and already_reviewed(n, sha):
            log(f"#{n}: head {sha[:10]} already reviewed — skipping")
            continue
        try:
            outcome = review_pr(n, title, sha, model, merge_model, args.dry_run)
            if _should_fail(outcome.posted, outcome.degraded):
                silent_failure = True
        except Exception as e:  # noqa: BLE001 — one PR's failure shouldn't sink the rest
            # An unhandled review error also leaves the PR unreviewed while the job would
            # otherwise stay green — the same silent-failure class, so surface it too.
            log(f"#{n}: ERROR {str(e)[:200]} — continuing")
            _annotate("error", f"#{n}: review errored — {' '.join(str(e).split())[:150]}")
            silent_failure = True
    return silent_failure


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
        silent_failure = sweep(args)
        # A silent failure has already annotated at the point of failure; exit 2 so the
        # check turns red (unless the operator opted out). Not a gate on review content —
        # a posted review, however critical, always exits 0.
        if silent_failure and FAIL_ON_DEGRADED:
            raise SystemExit(2)
        return

    # Watch/daemon mode: degraded passes still annotate, but a silent failure must NOT
    # kill the daemon — it keeps sweeping. So the return is intentionally ignored here.
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
