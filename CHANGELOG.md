# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). See the release procedure in
[CLAUDE.md](CLAUDE.md#changelog--releases).

## [Unreleased]

### Fixed
- A flaked union merge no longer throws away the review it was given. When `K>1`, the merge
  call is retried once, and if it fails twice the raw passes are posted unmerged behind a
  header note and a `::warning` annotation. The passes *are* the review and the merge is
  only editorial, so a merge-step outage now degrades formatting instead of delivery —
  previously a single empty-content 200 after K successful passes discarded all of them and
  turned the required check red. `merge_reviews()` no longer raises. Cost/token accounting
  sums every attempt, so a retried merge reports what it actually spent rather than only
  the winning call, and when both attempts fail the annotation names *both* reasons — a
  `402` followed by an empty 200 is credits exhaustion, not a model flake, and collapsing
  to the last reason hid the actionable one. The posted header reads `×K unmerged` on that
  path rather than claiming a `union ×K` that did not happen.
- **An oversized review is now clipped instead of lost.** GitHub rejects a comment body
  over 65536 characters, and nothing bounded the posted review (`max-diff-chars` caps the
  *input*). An over-cap body failed the post, and the error surfaced as a silent failure —
  exit 2 with no review at all. The body is now trimmed to fit, behind a truncation note
  and a `::warning`; the marker and cost footer are preserved so idempotency and reporting
  still work. This mattered most on the new unmerged-fallback path, whose body concatenates
  the K raw passes with no dedup and is therefore larger than the merged body would be.

## [1.3.0] - 2026-08-04

### Security
- The Action image no longer performs an unpinned global npm install. Its maintained pi
  runtime is pinned exactly and installed with `npm ci` from a committed integrity-checked
  lockfile, with dependency lifecycle scripts disabled. This also moves off the deprecated
  `@mariozechner` package, whose final release has active security advisories, to the
  maintained `@earendil-works` package. CI audits the locked production graph and builds the
  image, preventing vulnerable or non-reproducible dependency updates from slipping through;
  the Docker build context is allowlisted so repository secrets and unrelated files are not
  sent to the builder.
- **Persisted transcripts are auto-redacted.** Because session transcripts record the full
  agent conversation (including `bash` tool output), a prompt-injected agent could otherwise
  echo the OpenRouter key into one. Persisted transcripts (under `session-dir`) are now
  scrubbed of the `OPENROUTER_API_KEY` value, any `sk-or-v1-…` token, and
  `GITHUB_TOKEN`/`GH_TOKEN` before the file is kept. Operators should still avoid uploading
  `session-dir` transcripts to **public** artifacts on untrusted repos (redaction runs after
  the pass, so it is a mitigation, not a sandbox).

### Fixed
- A degraded pass is no longer a black box. The timeout branch surfaces the partial
  stdout/stderr captured before the kill, and an exit-0-with-no-output pass surfaces any
  stderr, so a blocked review carries a forensic tail in the log/annotation instead of a
  bare "produced no review output" line.
- A degraded review with **no output** now posts a comment on the PR explaining the failure
  and linking the run log/artifacts (built from `GITHUB_RUN_ID`), instead of posting
  nothing at all — both for the all-passes-empty case and the head-checkout-failed case.
  The notice carries a distinct `second-opinion-failed` marker so a daemon sweep never
  re-posts it for the same SHA (and it never collides with the success marker, so a later
  retry/push on a new SHA still gets a real review). It is not a passing review, so the
  `fail-on-degraded` tripwire still exits 2 and the check stays red — no silent-green.

### Added
- **Parallel K passes (hosted providers).** For `K>1` with `PROVIDER=openrouter`, the agentic
  passes now run **concurrently** (one pi subprocess each, up to K in-flight) instead of
  sequentially — so `K×timeout` wall-clock collapses to roughly one pass, giving much more
  headroom against spaghettio's hard-timeout mode. Each pass gets its own session subdir (a
  `pass-N` dir under `PI_SESSION_DIR` when persisting, else a throwaway temp dir) so
  concurrent transcripts never collide and per-pass cost/token attribution stays correct.
  Local llama stays sequential: a single GPU serves one request at a time, and parallelism
  could overload the server.
- **Real cost/token reporting.** Per-pass token counts are read from pi's session transcript,
  normalized from pi's camelCase `Usage` schema (`cacheRead`/`cacheWrite`), and the log line
  now shows `N tokens · $cost`; the OpenRouter **merge** call reports its authoritative
  `usage.cost`. pi's own per-message `cost.total` is used when present; otherwise the cost is
  estimated from real token counts × OpenRouter list prices (cached in `_model_prices`), so
  `PROVIDER=local` stays fully offline (no cloud pricing lookup). The footer shows the **total
  review price and token count**; because pass-derived cost is always an estimate, the footer
  marks it `≈`. Per-pass usage is attributed correctly even when passes share a persisted
  `session-dir` (each pass counts only its own transcript, not the cumulative usage of
  earlier passes).
- New `session-dir` action input (env `PI_SESSION_DIR`): when set, pi persists each pass's
  full JSONL session transcript there (upload it as an artifact to replay a blocked/empty
  pass). When empty, pi still writes a throwaway session internally per pass so token/cost
  reporting works — but the transcript is scrubbed afterward rather than retained, matching
  the old ephemeral behavior.
- New `max-tokens` action input (env `PI_MAX_TOKENS`): max completion tokens for a pass.
  The default is provider-aware and empty-string safe — OpenRouter `65536`
  (deepseek-v4-flash-0731's cap) instead of the old `32768`, local stays at `32768` — so a
  reasoning-capable model is less likely to exhaust its output budget in the reasoning
  channel and return an empty `content` on a 200 (the same class as the #16 merge fix, seen
  again on the agentic passes of spaghettio #574). Set a value to override.

## [1.2.1] - 2026-08-02

### Fixed
- Review passes on PRs with **large diffs (>100 KB prompt)** no longer crash with
  `[Errno 7] Argument list too long: 'pi'`. Linux caps a single `execve()` argument at
  128 KiB (`MAX_ARG_STRLEN`); the diff-bearing user prompt was passed to `pi` inline as
  one argv element, so any sufficiently large PR failed deterministically before the
  review even started (observed on spaghettio#569, a 163 KB diff). Prompts above a
  conservative `PROMPT_ARG_MAX` (env-tunable, default 100000 bytes) are now **piped to
  `pi` via stdin**, which pi uses verbatim as the initial prompt — the model sees the
  same bytes as the inline path (pi's `@file` syntax was rejected for this: it wraps
  content in `<file>` markup, changing the prompt shape and leaking a temp path into
  model context). Smaller prompts keep the byte-identical inline invocation.
- An **oversized system prompt** (unbounded operator `GUIDANCE`/`GUIDANCE_FILE`, which
  rides argv via `--append-system-prompt`) now fails the pass legibly through the normal
  degraded-pass machinery (error annotation naming the cause) instead of the same opaque
  E2BIG crash. Guidance is deliberately never clipped.

## [1.2.0] - 2026-07-04

### Fixed
- A **degraded** review pass — one that times out, exits non-zero (e.g. a `402` out-of-credits),
  exits cleanly with *no* review output, or can't check out the PR's head commit — now emits a
  GitHub Actions annotation at the point of failure (`::warning` / `::error`, the latter carrying
  the subprocess's own message so a `402` surfaces the *why*) and, when the PR gets no posted
  review, fails the check (exit 2) instead of
  exiting silently green. It gates on reviewer *malfunction*, never on review *findings*: a posted
  review always exits `0`, and a `K>1` run where one pass succeeds posts and passes (its degraded
  sibling downgrades to an annotation only). New `fail-on-degraded` input (`FAIL_ON_DEGRADED` env),
  default `true`; set `false` for the old always-green behavior. In `--watch` mode a degraded pass
  annotates but never kills the daemon; `--dry-run` follows the same exit contract. (#11)

### Added
- `second-opinion-eval` CLI — measure the reviewer's recall against a real review loop:
  reconstruct a merged PR's pre-fix diff (the commit the loop's reviewer commented on most),
  run the reviewer, and judge its findings against the loop's review comments — recall, false
  positives, and validExtras (what it caught that the loop missed). `--dry-run` reconstructs
  ground truth with no model spend. (#7)
- `second-opinion-eval --judge-only` — re-grade the saved reviews from a previous run with a
  different `--judge-model` (re-fetches ground truth, no new agentic passes), e.g. to re-judge
  with an independent/stronger model since a model grading its own output is self-favoring. (#8)

## [1.1.0] - 2026-06-21

### Added
- `second-opinion-bootstrap` CLI — generate a draft `review-guidance.md` from a repo's
  PR-review history. Mines the findings other reviewers already raised (inline comments +
  review summaries), with hybrid recent+historical sampling and a per-PR findings cap, then
  synthesizes the recurring repo-specific bug classes in one strong-model call. `--save-dir`
  caches findings and persists the synthesis transcript. (#5)
- This changelog and a documented release procedure.

### Security
- Pinned third-party GitHub Actions (`actions/checkout`, `actions/setup-python`,
  `anthropics/claude-code-action`) to commit SHAs, and added Dependabot to keep them
  current. First-party `storkme/second-opinion@v1` and the consumer example stay on tags. (#3)

## [1.0.0] - 2026-06-20

### Added
- Initial release: an independent, agentic second-opinion PR reviewer.
  - Two review providers — OpenRouter (hosted) and a local llama.cpp `llama-server` (free/offline).
  - Two delivery modes — a GitHub Action (event-driven, one PR) and a self-hosted `--watch` daemon.
  - Two merge backends for the `K>1` union (defaults to the review provider; `local` is fully offline).
  - Per-project guidance file (the reviewer's "memory"), HTML-marker idempotency (no database),
    and decorrelated, advisory-never-a-gate framing.

[Unreleased]: https://github.com/storkme/second-opinion/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/storkme/second-opinion/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/storkme/second-opinion/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/storkme/second-opinion/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/storkme/second-opinion/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/storkme/second-opinion/releases/tag/v1.0.0
