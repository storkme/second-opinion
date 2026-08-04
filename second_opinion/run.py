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
  FAIL_ON_DEGRADED    exit 2 when a degraded pass posts no review (default true; set
                      false for the old always-green behavior)
  REPO_DIR            repo checkout (default: cwd)

Usage: run.py [--pr N] [--dry-run] [--force] [--watch [--interval S]]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import shutil
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
PASS_TIMEOUT_S = int(_pt) if _pt else (900 if PROVIDER == "openrouter" else 1800)
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


DEGRADED = {"timeout", "error", "empty"}


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


def run_pass(wt: str, model: str, system: str, user: str) -> PassResult:
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
        return _run_pass_argv(wt, model, system, prompt_arg=None, stdin_input=user)
    return _run_pass_argv(wt, model, system, prompt_arg=user)


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
    try:
        base = (os.environ.get("OPENROUTER_BASE_URL", "").strip().rstrip("/")
                or "https://openrouter.ai/api")
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        resp = requests.get(f"{base}/v1/models", headers=headers, timeout=15)
        resp.raise_for_status()
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
        prices = None
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


def _read_session_usage(session_dir: str, exclude: set = ()) -> dict:
    """Sum real token usage across the pi session JSONL file(s) in a dir, skipping files
    already present before the pass began (so a pass shares a persisted dir without
    absorbing the cumulative usage of earlier passes).

    pi's session `Usage` uses camelCase keys (`cacheRead`/`cacheWrite`); these are
    normalized to snake_case. pi's authoritative per-message `cost.total` is also summed
    into `cost_total` when present."""
    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_total": 0.0}
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
                        usage = (entry.get("message") or {}).get("usage")
                        if not isinstance(usage, dict):
                            continue
                        total["input"] += int(usage.get("input") or 0)
                        total["output"] += int(usage.get("output") or 0)
                        total["cache_read"] += int(usage.get("cacheRead")
                                                   or usage.get("cache_read") or 0)
                        total["cache_write"] += int(usage.get("cacheWrite")
                                                    or usage.get("cache_write") or 0)
                        cost = (usage.get("cost") or {}).get("total")
                        if isinstance(cost, (int, float)):
                            total["cost_total"] += float(cost)
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
    tokens = (usage.get("input", 0) + usage.get("output", 0)
              + usage.get("cache_read", 0) + usage.get("cache_write", 0))
    cost = usage.get("cost_total", 0.0)
    if cost <= 0:
        # pi may not price a custom OpenRouter model (cost.total is 0); fall back to a
        # list-price estimate from real token counts.
        cost = _cost_from_usage(model, usage)
    return PassResult(text, status, cost=cost, tokens=tokens)


def _run_pass_argv(wt: str, model: str, system: str, prompt_arg: str | None,
                   stdin_input: str | None = None) -> PassResult:
    flags = list(PI_FLAGS)
    session_dir = os.environ.get("PI_SESSION_DIR", "").strip()
    internal = False
    if session_dir:
        os.makedirs(session_dir, exist_ok=True)
        flags += ["--session-dir", session_dir]
    else:
        # Always write a session (to a throwaway dir) so the pass's real token usage/cost is
        # readable and the transcript is recoverable on a crash; scrubbed afterward when not
        # persisted. Transcripts are kept for replay only when PI_SESSION_DIR points where
        # the consumer persists them (e.g. an action artifact).
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
    try:
        # input=None keeps today's inherited-stdin behavior for the inline path;
        # a str pipes it (non-TTY), which pi reads as the verbatim initial prompt.
        p = subprocess.run(cmd, cwd=wt, capture_output=True, text=True,
                           timeout=PASS_TIMEOUT_S, env=env, input=stdin_input)
    except subprocess.TimeoutExpired as e:
        # The exception carries the partial output captured before the kill (stdout in
        # `output`, stderr in `stderr`) — surface it so a blocked pass is diagnosable
        # from the log, not a silent black box.
        tail = _peek(e.stderr or e.output or "")
        note = f" — partial output: {tail}" if tail else ""
        log(f"pi pass timed out after {PASS_TIMEOUT_S}s{note}")
        _annotate("warning", f"pi pass timed out after {PASS_TIMEOUT_S}s — no review produced{note}")
        return _finish_pass(model, session_dir, internal, "", "timeout", prior_files)
    if p.returncode != 0:
        # Surface the failure (bad key, 402 out-of-credits, unknown model id, server
        # 4xx/OOM) instead of leaving only a "0c" line. Partial stdout from a crash isn't
        # trustworthy. The annotation carries WHY (e.g. the 402 message) to the operator.
        detail = " ".join((p.stderr or p.stdout or "").split())[:200]
        log(f"pi pass exited {p.returncode}: {detail}")
        _annotate("error", f"pi exited {p.returncode} — {detail[:150]}")
        return _finish_pass(model, session_dir, internal, "", "error", prior_files)
    text = (p.stdout or "").strip()
    if not text:
        # Exit 0 with nothing to say: the model returned no review at all. Treat as a
        # degraded pass, not a clean bill of health. A silent pass can still carry a tale
        # in stderr (a provider warning, an empty assistant message pi relayed) — surface
        # it so the failure isn't a black box.
        note = f" — stderr: {_peek(p.stderr)}" if (p.stderr or "").strip() else ""
        log(f"pi pass exited 0 but produced no review output{note}")
        _annotate("warning", f"pass completed but produced no review output{note}")
        return _finish_pass(model, session_dir, internal, "", "empty", prior_files)
    return _finish_pass(model, session_dir, internal, text, "ok", prior_files)


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
    choices = r.json().get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    content = (msg.get("content") or "").strip()
    if not content and choices:
        # Diagnose the empty-content-200 shape instead of failing mute:
        # finish_reason + reasoning length distinguish the reasoning-burn
        # failure mode from a genuinely empty reply.
        log(f"_chat: empty content on 200 — finish_reason="
            f"{choices[0].get('finish_reason')!r}, "
            f"reasoning_len={len(msg.get('reasoning') or '')}")
    usage = r.json().get("usage") or {}
    if meta is not None:
        meta["cost"] = float(usage.get("cost") or 0)
        meta["tokens"] = int(usage.get("total_tokens") or 0)
        ctd = usage.get("completion_tokens_details") or {}
        meta["reasoning_tokens"] = int(ctd.get("reasoning_tokens") or 0)
    return content


def merge_reviews(pr: int, title: str, passes: list[str], merge_model: str | None = None, meta: dict | None = None) -> str:
    """Union the K passes via one merge call (only used when K>1)."""
    merge_model = merge_model or MERGE_MODEL or MODEL
    passes_block = "\n\n".join(
        f"=== PASS {i+1} of {len(passes)} (independent) ===\n{p}"
        for i, p in enumerate(passes))
    prompt = MERGE_PROMPT.format(pr=pr, title=title, passes_block=passes_block)
    if MERGE_PROVIDER == "local":
        out = _chat(LLAMA_SERVER_URL, "", merge_model, prompt, meta)
    else:
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        out = _chat(OPENROUTER_BASE, key, merge_model, prompt, meta)
    if not out:
        raise RuntimeError(f"merge ({MERGE_PROVIDER}/{merge_model}) returned no usable content")
    return out


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
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        rid = os.environ.get("GITHUB_RUN_ID", "")
        if repo and rid:
            text += f"\n- **Run log + artifacts:** {server}/{repo}/actions/runs/{rid}\n"
    except Exception:
        pass
    text += ("\n*The check is deliberately red so this is not mistaken for a passing review. "
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
    if _already_noticed_failure(pr, sha):
        log(f"#{pr}: failure notice for {sha[:10]} already posted — skipping duplicate")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(text)
        tmp = f.name
    try:
        _gh(["pr", "comment", str(pr), "--body-file", tmp])
        log(f"#{pr}: posted degraded-review failure notice")
    except Exception:
        log(f"#{pr}: failed to post failure notice — check stays red regardless")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def review_pr(pr: int, title: str, sha: str, model: str, merge_model: str, dry_run: bool) -> ReviewOutcome:
    diff = _gh(["pr", "diff", str(pr)])
    filtered, _files, truncated = rv.filter_diff(diff, _exclude_globs(), MAX_DIFF_CHARS)
    if not filtered.strip():
        log(f"#{pr}: empty filtered diff — skipping")
        return ReviewOutcome(False, False)

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
        # The reviewer never ran — the same silent-failure class as a degraded pass
        # (the PR stays unreviewed), so it must trip the tripwire, not slip out green.
        detail = " ".join((add.stderr or "").split())[:120]
        log(f"#{pr}: worktree add failed @ {sha[:10]}: {detail}")
        _annotate("error", f"#{pr}: worktree add failed @ {sha[:10]} — {detail} — no review produced")
        _post_failure_notice(pr, sha, dry_run, checkout=True)
        return ReviewOutcome(False, True)

    passes: list[str] = []
    degraded = False
    try:
        total_cost = 0.0
        total_tokens = 0
        for i in range(K):
            diff_use = filtered if i == 0 else rv.shuffle_inputs(filtered, i)
            t0 = time.time()
            result = run_pass(wt, model, system, user_turn(diff_use))
            total_cost += result.cost
            total_tokens += result.tokens
            log(f"#{pr}: pass {i+1}/{K} — {len(result.text)}c · "
                f"{result.tokens:,} tok · ${result.cost:.4f} in {time.time()-t0:.0f}s")
            if result.status in DEGRADED:
                degraded = True
            if result.text:
                passes.append(result.text)
    finally:
        _git(["worktree", "remove", "--force", wt], check=False)

    if not passes:
        # Degraded with no output: still flag it loudly — but make the failure visible on the
        # PR with a comment + a link to the run log/artifacts, instead of posting nothing at
        # all. The notice is NOT a review, so the tripwire still exits 2 (check stays red).
        log(f"#{pr}: all passes empty — posting failure notice, check stays red")
        _post_failure_notice(pr, sha, dry_run)
        return ReviewOutcome(False, True)

    k = len(passes)
    if k == 1:
        review_body = passes[0]
    else:
        mm: dict = {}
        review_body = merge_reviews(pr, title, passes, merge_model, meta=mm)
        total_cost += mm.get("cost", 0.0)
        total_tokens += mm.get("tokens", 0)
    pass_label = "single pass" if k == 1 else f"union ×{k}"
    body = HEADER.format(marker=MARKER.format(sha=sha), pass_label=pass_label,
                         model=model, body=review_body)
    # Pass-derived costs are list-price estimates (pi does not price custom OpenRouter
    # models), so label the total as an estimate regardless of whether a pass degraded.
    body += _cost_footer(total_cost, total_tokens, estimated=True)
    if truncated:
        body += "\n\n*(diff truncated to fit context — coverage is partial)*"

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
