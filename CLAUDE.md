# second-opinion

An independent, **agentic** second-opinion code reviewer for GitHub pull requests. For
each PR it checks out the head commit into a worktree, lets a model **explore the repo
with tools** (read + grep) to understand the change in context, and posts **one advisory
comment**. It deliberately never reads other reviewers' comments — its entire value is
being *decorrelated* from them: a genuinely independent second pair of eyes, not a gate.

> Heritage: unified from two reviewers in [sisyphus](https://github.com/storkme/sisyphus)
> (`../sisyphus`). The clean OpenRouter-backed Action lived at `pr-review/.. pr-second-opinion/`;
> the self-hosted llama daemon lived at `pr-review/local_review.py`. This repo is the
> single core both collapse into. See **Status / unification plan** at the bottom.

## The shape: one core, two providers, two merge backends, two deliveries

```
                         ┌─────────────────────────────┐
   delivery ───────────► │  second_opinion/ (the core) │ ◄─────────── delivery
   GitHub Action         │   review.py  prompts+filter │      self-hosted daemon
   (action.yml,          │   providers.py  models.json │      (deploy/compose,
    one PR, event)       │   run.py     orchestration  │       run.py --watch)
                         └─────────────────────────────┘
        review provider ─┤ openrouter | local (llama-server) ├─ merge provider
```

- **One core** — `second_opinion/`, an installable package. All review logic, prompts,
  diff filtering, provider registration, and orchestration live here. Project-agnostic:
  no repo-specific constants, no dependency on any experiment harness or config file.
- **Two review providers** (`PROVIDER`): `openrouter` (default — runs anywhere, no GPU,
  pay per token) and `local` (a llama.cpp `llama-server`, free, zero marginal cost, needs
  a GPU box). Either is registered into pi's `~/.pi/agent/models.json` by `providers.py`.
- **Two merge backends** (`MERGE_PROVIDER`, only used when `K>1`): `openrouter` and
  `local` — each a single non-agentic HTTP chat call. Defaults to match `PROVIDER`, so
  `PROVIDER=local` is **fully offline/zero-cloud** end to end. (There is intentionally no
  `claude`-CLI merge: it would need Anthropic auth + the `claude` binary in the image,
  which we deliberately don't install.)
- **Two deliveries** — the **GitHub Action** (`action.yml` + `Dockerfile` + `entrypoint.sh`:
  event-driven, reviews exactly one PR, zero infra, paid CI review) and the **self-hosted
  daemon** (`deploy/`: `run.py --watch` on an interval, local GPU, free). Both call the
  same `run.py`; the only difference is single-shot vs. loop.

## Invariants (do not break these)

- **pi is the runner.** Agentic passes run via [`@mariozechner/pi-coding-agent`](https://www.npmjs.com/package/@mariozechner/pi-coding-agent)
  (`pi --provider … --model …`). Nothing else drives the model for the review passes.
- **Idempotency is an HTML marker comment on the PR** — `<!-- second-opinion sha={sha} -->`
  as the first line of the posted comment. No database, no state files. Safe on ephemeral
  CI runners; a force-push (new head SHA) earns a fresh review. The dedup check is a
  **paginated** REST read matching the marker at the **start** of a comment body.
- **Decorrelated & read-only.** The agent is *instructed* (not sandboxed) to read/grep the
  checked-out repo freely but never to read other reviewers' comments, fetch PR discussion,
  or edit/commit/push. This is a review-quality property, not a security boundary.
- **Advisory, never a gate.** Output always carries "Silence ≠ clean — treat as a tripwire,
  not a gate." Never wire this into branch protection / required checks.
- **Recall framing stays honest & generic.** Do NOT hardcode sisyphus's measured numbers
  (e.g. "~0.35 recall vs claude[bot]") or links to `pr-review/LESSONS.md` — that's
  sisyphus's measured-basis research and it stays in sisyphus. Here: "recall not separately
  measured for this config — calibrate accordingly."

## Layout

```
second_opinion/
  __init__.py
  review.py        # SYSTEM_TEMPLATE, AGENTIC_CLAUSE, system_prompt(), filter_diff(),
                   # shuffle_inputs(), DEFAULT_EXCLUDE_GLOBS — the project-agnostic core.
  providers.py     # write_models_json(): registers the OpenRouter OR local-llama provider
                   # into ~/.pi/agent/models.json (merge-preserving). Local model id is
                   # discovered from LLAMA_SERVER_URL/v1/models. (was pi_models.py)
  run.py           # orchestration + CLI: review one PR / sweep all / --watch daemon loop.
                   # merge_reviews() dispatches openrouter|local. Entry point: main().
  bootstrap.py     # OFFLINE tool: mine a repo's PR-review history → draft a review-guidance.md
                   # (one strong-model synthesis call; reuses run._chat + DEFAULT_MODEL). Mines
                   # the pulls API (reviewer findings), not the issues stream it posts to —
                   # structural decorrelation. CLI: main().
  eval.py          # OFFLINE tool: measure the reviewer's recall vs a real review loop —
                   # reconstruct a PR's pre-fix diff, run the reviewer, judge findings vs the
                   # loop's comments (recall / FP / validExtras). Reuses run_pass + _chat. CLI: main().
action.yml         # delivery 1: GitHub Action manifest (inputs → env).
Dockerfile         # Action image: node22 + python3 + gh + pi. claude NOT installed.
entrypoint.sh      # Action entrypoint: requires PR_NUMBER, runs `run.py --pr N` once.
examples/
  second-opinion.yml   # drop-in .github/workflows sample (fork guard, concurrency).
deploy/
  docker-compose.yml   # delivery 2: clones/mounts target repo, runs `... --watch`.
tests/
  test_review.py   # diff filtering / prompt construction.
  test_run.py      # merge-response parsing + marker dedup query (subprocess/requests stubbed).
  test_bootstrap.py # guidance mining: corpus build, decorrelation filter, brace-safe synth.
  test_eval.py     # eval scoring recompute + diff reconstruction / ground-truth filtering.
pyproject.toml     # package "second-opinion"; scripts second-opinion{,-bootstrap,-eval}; py>=3.11.
README.md          # user-facing setup/usage.
CLAUDE.md          # this file.
```

## Config — env only, plus a guidance file

No `config.toml`, no `Config` dataclass. Everything is environment variables (host/secret
values) plus one optional **guidance file** (the per-project review "memory"). This is the
deliberate simplification chosen during unification — the old `pr_review.config` carried a
lot of dead machinery (full-file context, line-numbered diffs, history branch) that the
agentic path never used.

| env | default | what |
|---|---|---|
| `GITHUB_REPO` | — (required) | `owner/name` |
| `GITHUB_TOKEN` | — (required) | needs `pull-requests: write`; also exported as `GH_TOKEN` |
| `PROVIDER` | `openrouter` | review provider: `openrouter` \| `local` |
| `OPENROUTER_API_KEY` | — | required when review or merge provider is `openrouter` |
| `LLAMA_SERVER_URL` | — | required when review or merge provider is `local` |
| `MODEL` | `DEFAULT_MODEL` (`z-ai/glm-5.2`) | OpenRouter model id (local: auto-discovered) |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api` | |
| `K` | provider-aware: `1` openrouter, `3` local | agentic passes to union; `K=1` skips the merge |
| `MERGE_PROVIDER` | = `PROVIDER` | union-merge backend: `openrouter` \| `local` |
| `MERGE_MODEL` | = review model | model for the `K>1` merge |
| `PROJECT` | `"this"` / repo name | injected into the prompt |
| `GUIDANCE` / `GUIDANCE_FILE` | — | per-project checklist ("memory"); file path is repo-relative |
| `EXCLUDE_GLOBS` | sensible set | comma-separated globs dropped from the diff |
| `MAX_DIFF_CHARS` | `60000` | diff size cap (whole-file boundaries) |
| `PASS_TIMEOUT_S` | `900` | per-pass timeout |
| `TOOLS` | `read,bash` | pi tool grant; `read` drops shell (safer on untrusted authors) |
| `PI_REASONING` | `true` | honored for **both** providers; set `false` for a non-reasoning model |
| `REPO_DIR` | cwd | the target repo checkout |

`--watch` / `--interval N` are CLI flags (daemon mode); `--pr N`, `--dry-run`, `--force`
are the existing single-shot flags.

### The guidance file = the reviewer's memory

`GUIDANCE_FILE` points at a Markdown file of project-specific review instructions —
recurring bug classes, conventions, "check X whenever Y" — injected as a second checklist
pass (Pass 2) in the system prompt. It is the one thing that makes the reviewer *yours*;
curate it like `CLAUDE.md`. sisyphus's curated `config.toml` `extra_instructions` block
moves into such a file on the sisyphus side.

## Build / test / run

```bash
pip install -e '.[test]'              # editable install + pytest
pytest                                 # unit tests (no network — subprocess/requests stubbed)

# single PR, dry run (prints, doesn't post):
GITHUB_REPO=owner/name GITHUB_TOKEN=… OPENROUTER_API_KEY=… \
  second-opinion --pr 42 --dry-run

# self-hosted daemon against a local GPU, fully offline:
GITHUB_REPO=owner/name GITHUB_TOKEN=… PROVIDER=local LLAMA_SERVER_URL=http://…:8080 \
  second-opinion --watch --interval 1800
```

Needs `pi`, `gh`, and `git` on PATH. The Docker image bundles `pi` + `gh`; `claude` is
intentionally absent.

## Security

The agent runs `pi` with `read,bash` inside a container holding `GITHUB_TOKEN` (and, for
the OpenRouter path, `OPENROUTER_API_KEY`). `bash` is **not sandboxed** — a hostile PR diff
could attempt prompt injection to run shell or exfiltrate secrets. So: enable only on repos
whose PR authors you trust; the example workflow's `head.repo == repo` fork guard stops
forks but not a compromised same-repo author; set `TOOLS=read` to drop shell at some recall
cost; use a low-limit OpenRouter key. `run.py` strips `GITHUB_TOKEN`/`GH_TOKEN` from the pi
subprocess as defense-in-depth (they're only needed by the `gh`/`git` calls in the parent).

## Changelog & releases

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com) + semver.

- **Every PR with a user-facing change** (an action input/behavior, the
  `second-opinion-bootstrap` CLI, the daemon) adds a bullet under `## [Unreleased]` in the
  *same* PR — grouped Added / Changed / Fixed / Removed / Security. Skip pure-internal noise
  (typos, test-only tweaks).
- **Cutting a release:** rename `[Unreleased]` → `## [X.Y.Z] - YYYY-MM-DD`, bump `version`
  in `pyproject.toml` to match, update the link refs at the bottom of the changelog, and
  commit. Then tag `vX.Y.Z` (annotated) and **move the major `v1` tag to it**
  (`git tag -fa v1 <sha> -m … && git push -f origin v1`), and publish a GitHub release with
  that section's notes. Consumers pin `uses: storkme/second-opinion@v1`, so `v1` must always
  point at the latest stable.
- Keep three things in agreement: `pyproject.toml` `version`, the newest `CHANGELOG.md`
  heading, and the latest tag.

## Status

**Core port: done** (2026-06-20) — the repo is built and the unit suite passes (`pytest`,
9 tests). Ported from `../sisyphus`: based on the clean Action, folded in the daemon's
local-llama provider, `--watch` loop, provider-aware K, and the selectable merge backend;
decoupled from sisyphus's `harness` module (`REPO_ROOT`/`_run`/`JUDGE_CWD`/`AGENTIC_CLAUSE`)
and `pr_review.config`/`config.toml`.

Decisions locked (2026-06-20): env-only config + guidance file (no TOML); a **local** merge
backend so self-hosted is fully offline (claude-CLI merge dropped); `--watch` in `run.py`
(one binary, both modes); marker unified to `<!-- second-opinion sha={sha} -->`; public name
`second-opinion` everywhere.

Carry-forward fixes (from #272's review):

- [x] **#1** defensive merge-parse (`choices`/`message`/`content` guarded) — `_chat()` in `run.py`.
- [x] **#2** upfront credential validation per provider (`OPENROUTER_API_KEY` / `LLAMA_SERVER_URL`) — `run.py` `main()`.
- [x] **#3** `PI_REASONING` honored for both providers — `providers.py`.
- [x] **#4** Resolved as *documented exposure*, not by stripping. `models.json` is `chmod 600` and `GITHUB_TOKEN`/`GH_TOKEN` are stripped from the pi subprocess (only the parent's `gh`/`git` need them). The OpenRouter key is deliberately **left reachable**: pi must read it from `models.json` to authenticate, and the agent's `bash` tool runs as the same user in the same container, so it can read that file (and the env) regardless — env-stripping removes neither the on-disk copy nor a determined exfil path, while risking auth breakage. The real controls are a low-limit key, the trusted-author boundary, and `TOOLS=read` to drop the shell. (Documented in README Security + the `run_pass` comment; validated end-to-end on PR #2.)
- [x] **#5** one `DEFAULT_MODEL` constant in `providers.py` (action.yml mirrors the literal as its input default — YAML can't import it).
- [x] **#6** kept `tools: read,bash` (recall) as the default in both deliveries; the `read` hardening is documented (README Security). Revisit if the Action is ever pointed at less-trusted repos.

Remaining — **on the sisyphus side** (separate repo, not this one):

- [ ] delete `pr-review/local_review.py` + the bespoke Docker reviewer; keep `pr-review/harness/` + the research docs (measured-basis research, not productizable).
- [ ] move sisyphus's `config.toml` `extra_instructions` checklist into a guidance file (e.g. `.github/review-guidance.md`) and point `GUIDANCE_FILE` at it.
- [ ] wire sisyphus to consume this repo: a `uses: storkme/second-opinion@v1` workflow and/or the daemon.
- [ ] tag `v1` here once validated against a live PR.
