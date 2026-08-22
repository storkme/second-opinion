# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). See the release procedure in
[CLAUDE.md](CLAUDE.md#changelog--releases).

## [Unreleased]

### Fixed
- **The rate row now excludes free local reviews too.** The token-mix row filtered on
  provider and the money row did not, which is backwards: a `PROVIDER=local` review
  contributes real `tokens` and `$0`, so *Effective rate*, *Cost per review* and *Cost per
  100k diff chars* were pulled toward zero in any estate running both the Action and the
  daemon — a price drop that never happened, the mirror of the false signal these panels
  were built to kill. Filtered on `provider`, deliberately not the mix row's
  `split_provider`: that field exists on nothing logged before 1.9.0, so filtering on it
  would discard the whole history these three panels exist to show.
- **A malformed usage envelope can no longer fabricate merge tokens.** `cached_tokens` is
  clamped to `prompt_tokens` rather than only flooring the subtraction at zero. With
  `prompt_tokens: 10, cached_tokens: 99` the components fallback added to 100 for a call
  the provider priced at 11 — inventing 89 tokens, which is precisely what the token-split
  docs forbid everywhere else.
- `providers.py` claimed the default model was GLM 5.2; it has been
  `deepseek/deepseek-v4-flash-0731` since 1.0. That comment is the one an operator reads
  when deciding whether to flip `pi-reasoning`.

## [1.9.0] - 2026-08-22

### Added
- **The token split: `tokens_input` / `tokens_output` / `tokens_cache_read` /
  `tokens_cache_write` on every `review`, `pass` and `merge` event.** The pooled `tokens`
  count could not answer the question a spend spike actually raises. Its four components
  bill an order of magnitude apart — for `deepseek-v4-flash-0731`, $0.08 / $0.18 / $0.016
  per Mtok for fresh input, output and cache reads — so `cost_usd / tokens` is a blend of
  price *and* mix, and a provider reprice, a cache regression and a chattier prompt all
  move it the same way by a similar amount. Chasing a $7.80 review night on 2026-08-21
  ended at exactly that wall: the effective rate had wobbled between $0.024 and $0.057
  per Mtok all month with no way to attribute any of it. Split, `tokens_cache_read /
  tokens` charts the cache hit rate directly, which is usually the bigger lever on the
  bill than list price ever is — hits falling from 90% to 50% roughly triples spend with
  the price untouched. No new data source: `_read_session_usage` already separated all
  four and `_finish_pass` discarded them one line before building the `PassResult`.
  The merge call gets its own split from the chat API's `usage`, with
  `prompt_tokens_details.cached_tokens` **subtracted** out of `prompt_tokens` — that
  shape counts cached tokens inside the prompt total, and without the subtraction every
  cache hit would be counted twice in the class charts built to measure it.
  **The four are not a partition** and the docs say so in both places: `tokens` is the
  provider's authoritative total wherever it gives one, and some providers fold cached
  tokens into their input count, so ratios against `tokens` are sound but deriving a
  fifth class by subtraction is not.
- **`split_provider` on the `review` event** — which provider's accounting the four token
  classes came from. `provider` names who ran the *passes*, but the split also pools the
  *merge*, and `MERGE_PROVIDER` is configured independently; llama-server reports no cached
  count, so a run it touched has a structurally-zero cache numerator over a real
  denominator. The mix panels filter on this rather than `provider`, which would have
  admitted an openrouter-reviewed/local-merged run and charted its missing cache reads as a
  regression that never happened. `"mixed"` only when a merge actually ran under a
  different provider — at `K=1` none does.
- **Dashboard: two rows.** *Unit economics* (effective $/Mtok, cost per review, tokens
  per review, cost per 100k diff chars) separates rate from volume; *Token mix* (cache hit
  rate, the four classes stacked, output share) then attributes a moving rate. Read
  together: if the mix holds steady and the rate moves, that is a reprice and nothing else
  it could be. Both read empty for reviews logged before the split shipped.

## [1.8.2] - 2026-08-09

### Added
- **A way to get *to* a review's trace.** Tracing (1.8.0) exported a correct span tree
  that nothing could navigate to: the trace id was minted deep inside the exporter, never
  published, and never derivable — so a slow review on the dashboard and its waterfall in
  Tempo were two disconnected facts about the same 726 seconds. The id is now minted
  before the review runs and published three ways: as a `trace_id` field on every
  `review`/`pass`/`merge` Loki event, as a `#N: trace <id>` line in the run log (the
  fallback that needs neither Loki nor a dashboard, and is already on screen when a check
  goes red), and as a click-through link in the dashboard. The field is **absent, not
  blank, when tracing is off**, so the link is never dead by construction — only by a
  failed export, which the run log records next to the id. Ids stay random per review rather than derived from the head
  SHA, so a `--force` re-review is its own trace instead of colliding into an unreadable
  merged one.
- **Dashboard: *Slowest reviews — click a trace id for the waterfall*.** Recent traced
  reviews, longest first, each linking into Explore; the *Degraded passes* table links the
  same way, which is the more useful one — its tokens column says how much a pass burned,
  the waterfall says on which turn it stopped. Both go through a new **Tempo data source**
  dashboard variable, so no stack-specific uid is baked into the exported JSON; leave it
  unset and every other panel behaves exactly as before.
- **README: "Finding the trace for a review"** — the three lookup paths plus a TraceQL
  cookbook (by PR, malfunctioning reviews, slow turns, errored tool calls), each query run
  against real review traces rather than written from the schema. The slow-turn query is
  the one that pays for tracing: it surfaced a 99-second `llm turn` that emitted zero
  output tokens and ended `stop_reason=error`, which the totals record only as "slow".

## [1.8.1] - 2026-08-07

### Fixed
- **The reviewed checkout can no longer shadow the reviewer.** Both deliveries ran
  `python3 -m second_opinion.run` with cwd set to the repo under review, and `-m` prepends
  cwd to `sys.path` *ahead of* the image's `PYTHONPATH=/opt/reviewer` — so reviewing a repo
  that ships its own top-level `second_opinion/` package silently imported that copy
  instead of the released one. Only this repo is such a target, which made it invisible:
  the dogfood check pinned `@v1`, logged `Download action repository … SHA e4d8891`
  (v1.7.0), and then executed the branch's code. It surfaced only because per-pass Loki
  events appeared for PRs reviewed by a release that contains no per-pass emission at all,
  with a review and its pass event timestamped 26µs apart — the single-request
  `emit_events()` batching that exists only on `main`. Consequences while it stood: this
  repo's checks never exercised the released artifact (so "verified in CI here" meant
  "verified as PR code"), and a PR could only ever be reviewed by its own reviewer. Both
  entrypoints now pass `-P`, and so does the manual-sweep hint the entrypoint prints —
  running that from inside this checkout would otherwise reintroduce the bug by hand. The
  image reproduces the whole A/B at **build** time (a decoy package in cwd versus the real
  one on `PYTHONPATH`) and fails the build if the decoy wins, so a base-image change that
  silently restores shadowing cannot ship. That check is deliberately behavioural rather
  than a version assert: on an older python `-P` is an unknown option and the process dies
  before any assert runs, and merely checking that the flag *parses* would never prove it
  still drops cwd. Consumers other than this repo were unaffected. Note the dogfood check
  pins `@v1` like any consumer, so it keeps running the shadowed path until this ships and
  `v1` is retagged — the PR that fixed this was, necessarily, reviewed by the bug.

## [1.8.0] - 2026-08-07

### Added
- **A monitoring event per agentic pass**, alongside the existing per-review event. The
  review event names *which* pass degraded (`pass_statuses`, `passes_ok`,
  `passes_degraded`) but pools tokens/cost/seconds across all `K`, so it cannot say what a
  failure **cost**: at `K>1` a pass that died instantly and one that burned 12.6M tokens
  first — the #29 runaway signature, and the reason `max-pass-tokens` exists — look
  identical. Each `pass` event carries its own `status`, `tokens`, `cost_usd`, `chars` and
  `elapsed_s`, labelled `outcome=<status>` so one selector finds every timeout across every
  repo. Emitted at `K=1` too: `elapsed_s` is model time alone, while the review's
  `duration_s` also covers diff fetch, worktree setup and posting, so the pair separates
  model time from harness overhead — and a uniform schema keeps "pass duration p95" from
  silently excluding every `K=1` repo. Note `sum(pass.tokens) ≤ review.tokens`; the
  difference is the `K>1` merge call, which is not a pass.
- **`metrics.emit_events()`** — batches a review and its `K` pass events into a **single**
  push, grouping entries by label set with timestamps ascending per stream. This is what
  makes the above free: looping the existing single-event emitter would have turned one
  HTTP round trip into `K+1`, each with its own 3s read timeout, on a path whose entire
  contract is "never delay the review". `emit_event()` is now a thin wrapper over it and is
  unchanged for callers; the never-raise and disabled-means-no-network contracts hold for
  both.
- **A `merge` event for the `K>1` union merge**, the one model call with no event of its
  own. The review event's `merged: true|false` says a fallback happened but never why, and
  pools the merge's spend — including *failed* attempts, accumulated deliberately because a
  reasoning-burn empty 200 is the expensive failure — into the review totals. The event
  carries `attempts`, `merged`, both failure reasons, tokens and cost, labelled
  `outcome=merged|merged_on_retry|fallback`. `merged_on_retry` is the one worth watching:
  a merge that fails once and recovers annotates nothing today (the warning fires only when
  *both* attempts fail), so the leading indicator of a degrading provider exists solely as a
  stdout line. Both reasons are kept rather than the last, for the reason the retry logic
  already documents — a 402 then an empty 200 is credits exhaustion, a persistent condition
  that recurs every sweep, which the last reason alone would file as "the model flaked".
- **Sweep events now report config degradation** (`config_degraded`, plus `config_problems`
  when non-zero). A non-numeric `MAX_PASS_TOKENS` disables that ceiling and annotates once,
  then `_CONFIG_ERRORS` is *drained* — right for annotations, since a daemon must not red
  every tick over one typo, but wrong for monitoring: the ceiling stays inactive for the
  process's entire life while every sweep after the first reports a clean config. A
  `--watch` daemon could run for weeks believing it had a spend ceiling it did not have.
  `config_degraded` is always present, even at `0`, so an alert need not distinguish "no
  problems" from "field absent" — Loki's `| json` yields no value at all for a missing field.
- **Optional OTLP tracing** (`otlp-endpoint` / `otlp-user` / `otlp-token`; env
  `OTLP_ENDPOINT` / `OTLP_USER` / `OTLP_TOKEN`, `tracing.py`). The Loki events say a review
  took 197s; they cannot say where the 197s went, because a review is a tree — K passes,
  each a back-and-forth of model turns and tool calls, then a merge. Each review now
  exports one trace shaped `review` › `pass` › `llm turn`/`tool` › `merge`, so at `K>1` the
  concurrent passes and any straggler are visible as overlap in a waterfall.
  **Tool calls come from pi's JSONL session transcript**, parsed after the pass completes —
  pi has no OTLP of its own, so nothing needs configuring on its side, and a transcript
  truncated by a killed pass simply loses the inner spans rather than the trace.
  What it surfaced immediately, measured on this repo's own #36 review (194.3s): **12 tool
  calls totalling 0.06s**, and latency tracking *output* tokens at ~48 tok/s — one turn took
  16s on 25k input tokens because it emitted only 821. The lever on review latency is what
  the model writes, not the size of the diff, which is not a conclusion the per-review
  numbers could reach. **No OpenTelemetry SDK**: the exporter is hand-rolled OTLP/JSON over
  the existing `requests` dependency, since every span is built retroactively from
  timestamps already recorded — there is no live context for an SDK to propagate. Same
  contracts as the Loki emitter: off unless configured (no endpoint = no network call, so
  `PROVIDER=local` stays fully offline), exported after the review is posted, never raises,
  and `OTLP_TOKEN` is stripped from the pi subprocess and scrubbed from transcripts like the
  other secrets.
- **Three dashboard panels** in `examples/grafana-dashboard.json`: *Degraded passes — what
  they burned* (every non-`ok` pass ranked by tokens spent before it stopped, which
  distinguishes "the ceiling would have helped" from "it failed early anyway"), *Pass
  duration* p50/p95, and *Union merge health* by outcome. Every query was validated against
  a live Loki rather than written from the docs.

### Changed
- The example workflow now shows how to wire the v1.7.0 Loki inputs (commented out), and
  the dogfood workflow actually uses them. The feature shipped with no worked example of
  where the three values come from, which left "scope the token to `logs:write`" as advice
  with nothing to copy. The pattern documented: URL and instance id are repo **variables**
  (neither is sensitive, and an unset variable expands to `""` — so `vars.LOKI_URL` doubles
  as the kill switch), only the token is a secret.

## [1.7.0] - 2026-08-06

### Changed
- **Default review model is now `deepseek/deepseek-v4-flash-0731`** (was `z-ai/glm-5.2`) —
  `DEFAULT_MODEL`, the `model` action-input default, and the docs/examples all move
  together. spaghettio has run this model (pinned) with good results at a much lower
  per-token rate; consumers that pin `model:` explicitly are unaffected. The
  `PI_MAX_TOKENS` default (65536) was already sized to this model's completion cap, and
  it is reasoning-capable, so `reasoning: true` stays the right default. Note the
  flash-tier pairing spaghettio uses is `k: 3` (union as the recall lever for a weaker
  model); the action's `k` default remains `1` — set `k: 3` per repo if recall matters
  more than the extra passes' cost.

### Added
- **Optional runtime monitoring via Loki** (`loki-url` / `loki-user` / `loki-token` action
  inputs; env `LOKI_URL` / `LOKI_USER` / `LOKI_TOKEN`). The reviewer runs autonomously, so
  runtime, cost, degraded-rate, and review rounds per PR were invisible without reading
  every run log. When configured, one structured JSON event per review (outcome, per-pass
  statuses, tokens, ≈cost, diff size/truncation, duration) and per sweep (candidates,
  reviewed, duration — the daemon's liveness signal, including "llama-server unreachable")
  is pushed to a Loki endpoint — Grafana Cloud or self-hosted, same API. Push rather than
  scrape because the Action delivery is an ephemeral job nothing can scrape; both
  deliveries instrument once in `run.py`. **Off by default** (no `LOKI_URL` = no network
  call, so `PROVIDER=local` stays fully offline) and **strictly fail-soft**: the event is
  emitted after the review is posted, and a failed push is one log line, never a degraded
  or failed review; `--dry-run` emits nothing. The Loki token gets the same treatment as
  the other secrets — stripped from the pi subprocess env, scrubbed from persisted
  transcripts; scope it to `logs:write` only (documented in README Security). An
  importable dashboard ships as `examples/grafana-dashboard.json`: reviews by outcome,
  cost/tokens per repo, duration p50/p95, review rounds per PR, sweep liveness, raw
  events.

## [1.6.0] - 2026-08-05

### Added
- **A per-pass spend ceiling** (`max-pass-tokens`, `max-pass-cost-usd`; env `MAX_PASS_TOKENS`,
  `MAX_PASS_COST_USD`). `pass-timeout-seconds` bounds *time*, not money: an agent stuck in a
  tool-call loop burned **12.6M tokens and $1.96 producing nothing**, ended only by the clock,
  and raising the timeout had merely doubled the worst-case bill. A watchdog now samples the
  session transcript — where usage is already written per assistant message — and aborts a
  pass that crosses the ceiling, reporting it as **`runaway`**, its own degraded cause rather
  than a timeout. **Off by default**: killing a legitimately long pass is its own failure, and
  one incident is thin evidence for a global default, so each repo opts in with numbers it has.
- **Degraded annotations now say what the pass spent.** "timed out after 1800s" reads
  identically for a pass working flat out (2.3M tok), one hung at ~30 tok/s, and one looping
  at ~7000 tok/s — three failure modes with three different remedies, indistinguishable in the
  checks UI. Every degraded annotation now carries `N tok · $C`, so a runaway is recognisable
  without opening the run log. A `runaway` additionally names the spend that tripped the
  ceiling. If a cost ceiling is set but pricing is unavailable — `PROVIDER=local` never
  prices, and a failed lookup is deliberately uncached — the run says so rather than
  silently not protecting, and a non-numeric ceiling disables loudly instead of crashing
  the process at import.

### Fixed
- **`filter_diff` now reports what it did, instead of callers guessing.** It returned
  `(text, files, truncated)` — enough to know an excerpt was capped, never enough to know
  *how*: which chunks vanished, whether the last kept one was cut mid-hunk, or whether a
  path had a second chunk that got dropped. Callers inferred all three, and each inference
  was wrong in some shape, every one surfacing as partial coverage described as full. It
  now returns a `FilteredDiff` carrying `dropped` (per **chunk**, not per filename),
  `clipped`, `full_text`, and derived `missing_files` / `partial_files`. Three consequences:
  - A path with several `diff --git` blocks (rename+modify, mode+content) where the first
    fits and the second doesn't is now disclosed as "a later hunk of X is missing" — it was
    previously described as a mid-file clip, which is a different thing.
  - A file carried in the excerpt but cut mid-hunk is named as such even when other files
    were also dropped, instead of the excerpt reading as though it held that file whole.
  - "Larger than a single read" is claimed only when the on-disk diff actually exceeds an
    agent read tool's limits, rather than unconditionally — and checks **both** of them
    (pi truncates at 2000 lines *or* 50KiB, whichever hits first), so a line-dense diff
    under the byte cap is not advertised as readable in one go.
- **`second-opinion-eval` measures the reviewer it claims to.** `eval.review_diff` carried a
  hand-copied `user_turn` with no truncation handling, so on any diff over `max-diff-chars`
  it fed the agent a capped excerpt with no disclosure and no on-disk full diff — measuring
  the pre-remediation reviewer while its docstring promised "the reviewer AS CONFIGURED",
  and biased hardest on large PRs, which is where recall questions usually live. Both
  callers now share `truncation_notice()` / `write_full_diff()` / `coverage_phrase()`, so
  they cannot drift again.

## [1.5.0] - 2026-08-05

### Changed
- **Default `pass-timeout-seconds` raised from 900 to 1800**, and the provider split for this
  knob is gone (local was already 1800). 900s was measurably too low for a reasoning model on
  a large diff: spaghettio saw two all-passes-timed-out failures inside 24 hours, and the run
  that *did* succeed on the same PR used 827/779/787s of the 900s budget — 87–92% utilisation,
  i.e. a coin flip per run. A timed-out pass posts no review, so under `fail-on-degraded` this
  surfaces as reviewer malfunction reported like a verdict. Note `K>1` does not protect against
  it: parallel passes give redundancy when *one* pass fails, but when the cause is systematic
  every pass hits the wall together.

  **Consumers must keep the calling job's `timeout-minutes` comfortably above this.** If the job
  cap fires first it cancels mid-pass and skips the degraded report entirely — no annotation, no
  failure notice — which is strictly worse than a pass timeout. Documented on the input and in
  the README table, and enforced in this repo's own dogfood workflow (40min job / 1800s pass =
  600s slack).

### Fixed
- **A truncated diff no longer silently reads as full coverage.** `max-diff-chars` caps the
  prompt excerpt, and the filter stops at the first file chunk that overflows — so with
  chunks in git's path order, one large early file starves every file behind it. Measured on
  spaghettio#575: the excerpt carried **1 of 16 changed files (1.7% of the diff)** — a single
  generated HTML artifact — while all 12 Rust source files went unseen, and the check went
  green behind a one-line footnote. Three changes:
  - The **complete** (still glob-filtered) diff is written into the agent's working
    directory as `.second-opinion-full-diff.patch`, and the prompt names it, states the
    excerpt is truncated, lists the files missing from it, and tells the agent to read the
    rest and prioritise source over generated artifacts. The whole diff was already in hand
    — only the prompt was capped — so nothing new is fetched. It lands inside the checkout
    on purpose: an absolute path outside it would be unreadable under `TOOLS=read`, and a
    full diff the agent cannot open is a quieter version of the same bug. `git worktree
    remove --force` cleans it up.
  - Truncation now emits a `::warning` naming the covered/total file counts and the dropped
    files, instead of being visible only as italics at the foot of the posted comment.
  - The comment footer states the real numbers (`covered N of M changed files`) rather than
    "coverage is partial", which reads identically at 1-of-16 and 15-of-16.

  - The on-disk copy is ordered **unseen files first, smallest first**, and both the
    prompt and a header inside the file say that one read is not the whole thing.
    An agent read tool truncates (pi: 2000 lines / 50KB, whichever first) and this
    file exceeds that by construction, so in git path order the first read returned
    the same files the excerpt already carried — the agent could "read the complete
    diff" and see nothing new. On spaghettio#575 one read reached 1 file; it now
    reaches 10, all source, because the giant generated artifacts sort last.

  Coverage of the remainder is now *reachable* rather than *guaranteed* — it depends on the
  agent actually reading the file — and the annotation and footer both say so. If the write
  itself fails, the agent is still told the excerpt is truncated and which files are missing
  (it just gets no pointer, and is told to read the checkout rather than to `git diff`, which
  a shallow checkout with no base ref cannot do).

## [1.4.0] - 2026-08-05

### Changed
- The shipped example workflow (and the README quickstart) now skip **Dependabot** PRs
  alongside forks. Dependabot-triggered runs read from GitHub's *separate* Dependabot
  secret store, so `secrets.OPENROUTER_API_KEY` arrives empty and the job fails with
  "Missing required environment variable" — a guaranteed red check on every dependency
  bump. Skipping is preferred over adding the key to the Dependabot store, which would
  expose it to every automated bump run.

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
  The budget is counted in UTF-8 bytes rather than code points: GitHub documents the cap
  in "characters" without pinning the unit, and the comment is not ASCII, so a byte budget
  is the only one that holds under every reading.
- `second-opinion-eval` inherits the merge fallback. Previously a merge flake raised and
  the eval loop skipped that PR entirely; now it judges the raw passes, which is what
  production would have posted — so eval measures real behaviour. The eval log gains the
  fallback's `::warning` line when this happens.

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

[Unreleased]: https://github.com/storkme/second-opinion/compare/v1.9.0...HEAD
[1.9.0]: https://github.com/storkme/second-opinion/compare/v1.8.2...v1.9.0
[1.8.2]: https://github.com/storkme/second-opinion/compare/v1.8.1...v1.8.2
[1.8.1]: https://github.com/storkme/second-opinion/compare/v1.8.0...v1.8.1
[1.8.0]: https://github.com/storkme/second-opinion/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/storkme/second-opinion/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/storkme/second-opinion/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/storkme/second-opinion/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/storkme/second-opinion/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/storkme/second-opinion/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/storkme/second-opinion/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/storkme/second-opinion/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/storkme/second-opinion/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/storkme/second-opinion/releases/tag/v1.0.0
