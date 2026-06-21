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
    if: github.event.pull_request.head.repo.full_name == github.repository
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

### Action inputs

| input | default | what |
|---|---|---|
| `openrouter-api-key` | — (required) | OpenRouter key |
| `github-token` | `${{ github.token }}` | needs `pull-requests: write` |
| `pr-number` | the triggering PR | which PR to review |
| `model` | `z-ai/glm-5.2` | OpenRouter model id for the passes |
| `k` | `1` | agentic passes to union; `K=1` skips the merge |
| `merge-model` | = `model` | model for the `K>1` union merge |
| `project` | repo name | used in the prompt |
| `guidance-file` | — | path to this repo's review checklist (its "memory") |
| `exclude-globs` | sensible set | comma-separated globs dropped from the diff |
| `max-diff-chars` | `60000` | diff size cap |
| `pass-timeout-seconds` | `900` | per-pass timeout |
| `tools` | `read,bash` | agent tool grant; set `read` to drop shell (see Security) |
| `reasoning` | `true` | set `false` for a non-reasoning `model` |

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

## Providers & cost

- **OpenRouter** (`PROVIDER=openrouter`, the Action default): real tokens per PR (the model
  reads files). `K` defaults to `1`. Use a low-limit key.
- **Local llama-server** (`PROVIDER=local`, the daemon default): free, zero marginal cost.
  The model id is auto-discovered from `LLAMA_SERVER_URL/v1/models`. `K` defaults to `3` —
  the union is a recall lever for the weaker local model. The `K>1` merge runs locally too,
  so nothing leaves the box.

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
