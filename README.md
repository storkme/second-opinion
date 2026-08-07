# second-opinion

An independent, **agentic** second-opinion code reviewer for pull requests. For each PR it
checks out the head commit, lets a model **explore the repo with tools** (read + grep) to
understand the change in context, and posts one advisory comment. It deliberately never
reads other reviewers' comments — its value is being *decorrelated* from them: a genuinely
independent second pair of eyes, never a merge gate.

Run it two ways, with either of two providers:

| | provider | when |
|---|---|---|
| **GitHub Action** | OpenRouter | zero infra, paid CI review on every PR |
| **Self-hosted daemon** | local llama-server | you have a GPU box; free, zero marginal cost, fully offline |

> Heritage: unified from the [sisyphus](https://github.com/storkme/sisyphus) reviewers.
> The K-pass union was a recall hack for a weak local model; a strong hosted model defaults
> to a single agentic pass.

## Quickstart — GitHub Action

1. Add an **`OPENROUTER_API_KEY`** repo secret (Settings → Secrets → Actions).
2. Add `.github/workflows/second-opinion.yml`:

```yaml
name: Second Opinion
on:
  pull_request: { types: [opened, synchronize, ready_for_review, reopened] }
jobs:
  review:
    if: github.event.pull_request.head.repo.full_name == github.repository &&
        github.event.pull_request.user.login != 'dependabot[bot]'
    runs-on: ubuntu-latest
    permissions: { contents: read, pull-requests: write }
    steps:
      - uses: actions/checkout@v4
      - uses: storkme/second-opinion@v1
        with:
          openrouter-api-key: ${{ secrets.OPENROUTER_API_KEY }}
          guidance-file: .github/review-guidance.md   # optional
```

See [`examples/second-opinion.yml`](examples/second-opinion.yml) for the fuller version
(concurrency, fork guard, timeout).

> **Dependabot PRs are skipped.** Dependabot-triggered runs read from GitHub's separate
> Dependabot secret store, so `secrets.OPENROUTER_API_KEY` arrives empty and the job would
> fail on every dependency bump. Prefer skipping over adding the key to that store — it
> would expose it to every automated bump run.

### Action inputs

| input | default | what |
|---|---|---|
| `openrouter-api-key` | — (required) | OpenRouter key |
| `github-token` | `${{ github.token }}` | needs `pull-requests: write` |
| `pr-number` | the triggering PR | which PR to review |
| `model` | `deepseek/deepseek-v4-flash-0731` | OpenRouter model id for the passes |
| `k` | `1` | agentic passes to union; `K=1` skips the merge. For `K>1` on OpenRouter the passes run in parallel (one pi subprocess each); local stays sequential. |
| `merge-model` | = `model` | model for the `K>1` union merge |
| `project` | repo name | used in the prompt |
| `guidance-file` | — | path to this repo's review checklist (its "memory") |
| `exclude-globs` | sensible set | comma-separated globs dropped from the diff |
| `max-diff-chars` | `60000` | diff size cap |
| `max-tokens` | *(provider-aware)* | max completion tokens per pass. Empty = `65536` OpenRouter (deepseek-v4-flash-0731's cap) / `32768` local. A reasoning model can exhaust a smaller budget in its reasoning channel and return an empty 200. |
| `max-pass-tokens` | — (off) | abort a pass over this many tokens, reported as `runaway` not `timeout`. A clock bounds time, not spend: a looping agent burns tokens producing nothing and a longer timeout only raises the bill. |
| `max-pass-cost-usd` | — (off) | same, in USD. Depends on a price lookup that can fail — prefer `max-pass-tokens`. |
| `pass-timeout-seconds` | `1800` | per-pass timeout. Keep the job's `timeout-minutes` comfortably above it — a job cap firing mid-pass skips the degraded report entirely. |
| `session-dir` | — | when set, pi writes each pass's JSONL session transcript here (instead of ephemeral `--no-session`). Point it at a path you persist — e.g. upload as an artifact — to replay a blocked/empty pass. |
| `tools` | `read,bash` | agent tool grant; set `read` to drop shell (see Security) |
| `reasoning` | `true` | set `false` for a non-reasoning `model` |
| `fail-on-degraded` | `true` | fail the check when a pass degrades and posts no review ([below](#degraded-passes-fail-the-check)) |
| `loki-url` | — (off) | optional [monitoring](#monitoring-optional): Loki push endpoint receiving JSON events per review/pass/sweep |
| `loki-user` | — | basic-auth user for `loki-url` (Grafana Cloud: the numeric Loki instance id) |
| `loki-token` | — | basic-auth password for `loki-url` — scope it to `logs:write` only (see Security) |

## Quickstart — self-hosted daemon

You have a GPU box running a [llama.cpp](https://github.com/ggml-org/llama.cpp)
`llama-server` (OpenAI-compatible `/v1`). The daemon polls open PRs on an interval and
posts reviews — free, and with `PROVIDER=local` + the default `local` merge, fully offline.

```bash
cd deploy
cp .env.example .env        # set GITHUB_REPO, GITHUB_TOKEN, LLAMA_SERVER_URL
docker compose up -d --build
```

It clones the target repo once into a volume, then loops `second-opinion --watch`. A
host-side model swap is picked up on the next tick; the server being briefly down just
skips that tick.

## The guidance file (per-project memory)

Both modes accept a Markdown file of project-specific review instructions — recurring bug
classes, conventions, "check X whenever Y". It's injected as a second checklist pass in the
prompt. Keep it tight and curate it like `CLAUDE.md`; it's the one thing that makes the
reviewer *yours*. Omit it and the reviewer still runs on general code-review judgement.

## Bootstrapping the guidance file

Don't hand-write the guidance from scratch — **mine it from the repo's own review history**:

```bash
GITHUB_TOKEN=… OPENROUTER_API_KEY=… \
  second-opinion-bootstrap --repo owner/name --output .github/review-guidance.md
```

It samples merged PRs across the repo's history (`--window`/`--limit`, so older bug classes
aren't buried under recent work), collects the findings other reviewers already raised on
them (inline review comments + review summaries — `claude[bot]`, humans, …), and asks one
strong model to distill the *recurring, repo-specific* bug classes and conventions into a draft.
Decorrelation is structural: it mines line-level reviewer findings (the *pulls* API), **not
the PR conversation stream where second-opinion posts its advisory** (the *issues* API) — so
the reviewer's own output never enters the corpus it learns from.

The result is a **draft to curate**, not a finished file — prune and sharpen it like
`CLAUDE.md` before pointing the reviewer at it. (Default prints to stdout; `--output`
writes a file.) A repo with little review history won't have much to mine — that's the case
a deeper agentic history-audit would cover, which isn't built yet.

## Measuring recall (eval)

How do you know the second opinion is worth the extra comment? Measure it:

```bash
GITHUB_REPO=owner/name GITHUB_TOKEN=… OPENROUTER_API_KEY=… \
  second-opinion-eval 200 190 --dry-run   # reconstruct + ground truth, no model spend
second-opinion-eval --auto 5              # the 5 most-reviewed recent merged PRs
```

For each merged PR it reconstructs the diff *as the reviewer first saw it* (pre-fix), runs the
reviewer on it, and judges its findings against the loop's actual review comments — reporting
recall, false positives, and **validExtras** (real issues the loop missed — the decorrelation
payoff). Runs from a local checkout (needs `git` + `pi`); ~$0.3–0.5/PR, so use a small set and
`--dry-run` to scope first. For trustworthy FP/validExtras, judge with a *different* model
(`--judge-model`) — a model grading its own output is self-favoring; `--judge-only --save-dir DIR`
re-grades a previous run's saved reviews with another judge cheaply (no new agentic passes). A deeper, label-free
*agentic time-travel audit* (forward-fix as ground truth) is the next tier, not built yet.

## How it works

```
PR event / poll tick → for the PR head:
  fetch refs/pull/N/head → worktree at the head commit
  → K agentic `pi` passes (read+bash tools, read-only, no other-reviewer access)
  → K=1: post the pass · K>1: union/dedupe via one merge call → post
state: an HTML marker comment on the PR (one review per head SHA) — no database
```

- **Storage-free.** Idempotency lives in the PR (the marker comment), so ephemeral runners
  are fine; a force-push re-reviews.
- **Decorrelated.** The agent is *instructed* to read/grep the repo and never to edit,
  push, or read other reviewers' comments — an instruction, not a sandbox (see Security).
- **Two providers, two merge backends.** `PROVIDER` picks the review backend (`openrouter`
  or `local`); `MERGE_PROVIDER` picks the `K>1` union backend (defaults to `PROVIDER`).
  `PROVIDER=local` needs no cloud credential at all.

## Degraded passes fail the check

A review pass is **degraded** when it times out, the `pi` subprocess exits non-zero (bad
key, a `402 … requires more credits`, an unknown model id, a server 5xx/OOM), or it exits
cleanly but produces *no* review output (a review prompt never legitimately yields nothing).
A PR whose head commit can't even be checked out (`git worktree add` fails) counts the same
way — the reviewer never ran, which is not a clean bill of health. Each degraded pass emits
a GitHub Actions annotation at the point of failure — a `::warning`
for a timeout or empty output, a `::error` carrying the subprocess's own message (so a `402`
surfaces the *why*) for a non-zero exit or a failed checkout — visible in the checks UI and
the job summary.

If a pass degrades **and no review is posted for that PR**, the run exits non-zero (code 2),
turning the check red. This is a tripwire for reviewer *malfunction*, not review *findings*:
a posted review — however critical — always exits `0`, and a `K>1` run where one pass
succeeds posts and passes (its degraded sibling downgrades to an annotation only). It honors
the banner's own promise — *"Silence ≠ clean. Treat as a tripwire, not a gate."* — which the
old always-green-on-empty behavior quietly violated.

It's still **advisory, never a merge gate**: keep the job out of branch protection / required
checks (the example workflow does). A red advisory check is a signal, not a block. To keep the
old always-green behavior, set `fail-on-degraded: "false"` (or `FAIL_ON_DEGRADED=false` for the
CLI/daemon). In `--watch` mode a degraded pass annotates but never kills the daemon. `--dry-run`
follows the same contract: a preview whose passes all degrade also exits `2` (nothing was
posted) — set `FAIL_ON_DEGRADED=false` if a wrapper script needs a guaranteed-`0` preview.

## Providers & cost

- **OpenRouter** (`PROVIDER=openrouter`, the Action default): real tokens per PR (the model
  reads files). `K` defaults to `1`. Use a low-limit key.
- **Local llama-server** (`PROVIDER=local`, the daemon default): free, zero marginal cost.
  The model id is auto-discovered from `LLAMA_SERVER_URL/v1/models`. `K` defaults to `3` —
  the union is a recall lever for the weaker local model. The `K>1` merge runs locally too,
  so nothing leaves the box.

## Monitoring (optional)

The reviewer runs autonomously, so without monitoring you can't see whether it's working:
runtime, cost, how often it degrades, how many review rounds a PR accumulates. When
`LOKI_URL` is set, every review pushes **structured JSON events** to a
[Loki](https://grafana.com/oss/loki/) endpoint — Grafana Cloud or self-hosted, the push
API is identical, so moving off the cloud later is a URL + credential swap.

Three event types: **`review`** (one per PR reviewed), **`pass`** (one per agentic pass)
and **`sweep`** (one per daemon cycle — the liveness signal). A review and its passes
ride in a *single* request, so instrumenting `K` passes costs no extra round trip.

```jsonc
// Stream labels (indexed, low-cardinality): service/delivery/repo/event/outcome —
// e.g. outcome="posted". Everything else rides in the JSON line, parsed by `| json`:
{"event": "review", "pr": 574, "sha": "…", "model": "deepseek/deepseek-v4-flash-0731",
 "provider": "openrouter", "k": 1, "pass_statuses": "ok", "passes_ok": 1,
 "passes_degraded": 0, "merged": true, "tokens": 184000, "cost_usd": 0.031,
 "diff_chars": 41200, "diff_truncated": false, "duration_s": 412.3}

// One per pass. outcome = that pass's own status, so a single selector finds every
// timeout across every repo: {service="second-opinion", event="pass", outcome="timeout"}
{"event": "pass", "pr": 574, "sha": "…", "k": 3, "pass": 2, "status": "timeout",
 "tokens": 912345, "cost_usd": 0.503, "chars": 0, "elapsed_s": 1800.0}
```

The `review` event's `pass_statuses` says *which* pass died; the `pass` event says what
it **cost** to die. At `K=1` those are the same numbers, but at `K>1` the review totals
are pooled, so a pass that failed instantly and one that burned 12.6M tokens first (the
`max-pass-tokens` case) are indistinguishable without this. Note
`sum(pass.tokens) ≤ review.tokens`: the difference is the `K>1` merge call, which is not
a pass. At `K=1` a pass event is still worth having — `elapsed_s` is model time alone,
while the review's `duration_s` also covers diff fetch, worktree setup and posting.

- **Setup (Grafana Cloud):** from your stack's Loki details page take the push URL
  (`https://logs-prod-XXX.grafana.net/loki/api/v1/push`) and the numeric instance id, and
  create an access-policy token scoped to **`logs:write` only**. Set them as `loki-url` /
  `loki-user` / `loki-token` (Action inputs — org-level secrets cover multiple repos) or
  `LOKI_URL` / `LOKI_USER` / `LOKI_TOKEN` (daemon `.env`). Self-hosted Loki without auth
  needs only the URL.
- **Dashboard:** import [`examples/grafana-dashboard.json`](examples/grafana-dashboard.json)
  and point it at your Loki data source — reviews by outcome, cost/tokens per repo,
  review and pass duration p50/p95, degraded passes ranked by what they burned, review
  rounds per PR, daemon liveness, and a raw event log.
- **Contract:** off by default (no `LOKI_URL` = no network call, so `PROVIDER=local`
  stays fully offline), and strictly **fail-soft** — the push happens *after* the review
  is posted, and a failed push is one log line, never a degraded or failed review. The
  flip side: a run that crashes before the emit point sends no event at all — that's
  exactly the case the [`fail-on-degraded`](#degraded-passes-fail-the-check) tripwire
  turns red in CI, so the two mechanisms cover each other.
- **What it can't tell you:** that the reviews are *good*. This is runtime telemetry
  (it ran, it cost X, it didn't malfunction); review quality is what
  [`second-opinion-eval`](#measuring-recall-eval) measures.

## Security

The agent runs `pi` with **`read,bash`** tools inside the container, which holds your
`GITHUB_TOKEN` (and, on the OpenRouter path, your `OPENROUTER_API_KEY`). The system prompt
tells it not to edit/push or read other reviews, but **nothing sandboxes the `bash` tool** —
a sufficiently adversarial PR diff could attempt prompt injection to run arbitrary shell or
exfiltrate secrets. So:

- **Only enable this on repos whose PR authors you trust.** The example's fork guard
  (`head.repo == repo`) stops *forks*, but a same-repo PR from a compromised or untrusted
  author is still a vector — "decorrelated" is about review quality, not a security boundary.
- **To harden, set `tools: read`** — drops the agent's shell (it can still read files, but
  can't grep/run commands). Smaller blast radius, at some recall cost.
- `run.py` strips `GITHUB_TOKEN`/`GH_TOKEN` from the pi subprocess as defense-in-depth, and
  `providers.py` writes `~/.pi/agent/models.json` with mode `600`. The OpenRouter key still
  lives in that file in cleartext (pi reads it from there) — use a **low-limit key**.
- **The Loki credential is one more secret in the container** (when monitoring is enabled).
  Scope it to `logs:write` **only** — then the worst a compromised container can do with it
  is push garbage log lines, not read your metrics or touch dashboards. It gets the same
  defense-in-depth as the other secrets: stripped from the pi subprocess env (only the
  parent pushes events) and scrubbed from persisted transcripts.
- **Session transcripts are on disk and are your responsibility.** Every pass writes a JSONL
  transcript (a throwaway temp dir by default, or `PI_SESSION_DIR` when set). Persisted
  transcripts are **auto-redacted** — the `OPENROUTER_API_KEY` value, any `sk-or-v1-…`
  token, and `GITHUB_TOKEN`/`GH_TOKEN` are scrubbed before the file is kept. Still, do not
  point `session-dir` at an artifact that lands on a **public** repo you don't fully trust:
  a prompt-injected agent could echo the key at runtime before redaction runs, and a durable
  public artifact is a much larger exposure surface than an ephemeral runner. Prefer private/
  expiring artifacts on public repos, or drop `session-dir` there entirely.
- Treat the output as advisory, never a merge gate.

## Local / CLI use

It's also a plain CLI (`pip install -e .`, needs `pi`, `gh`, and `git` on PATH):

```bash
# single PR, dry run (print, don't post):
GITHUB_REPO=owner/name GITHUB_TOKEN=… OPENROUTER_API_KEY=… \
  second-opinion --pr 42 --dry-run

# self-hosted daemon against a local GPU, fully offline:
GITHUB_REPO=owner/name GITHUB_TOKEN=… PROVIDER=local LLAMA_SERVER_URL=http://…:8080 \
  second-opinion --watch --interval 1800
```

`--pr N` reviews one PR; no `--pr` scans all open PRs; `--force` ignores the marker;
`--watch` loops on `--interval` seconds.

## Development

```bash
pip install -e '.[test]'
pytest        # unit tests, no network (subprocess/requests stubbed)
```

See [`CLAUDE.md`](CLAUDE.md) for architecture and the unification notes.
