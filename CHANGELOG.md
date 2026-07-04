# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). See the release procedure in
[CLAUDE.md](CLAUDE.md#changelog--releases).

## [Unreleased]

### Fixed
- A **degraded** review pass — one that times out, exits non-zero (e.g. a `402` out-of-credits),
  exits cleanly with *no* review output, or can't check out the PR's head commit — now emits a
  GitHub Actions annotation at the point of failure (`::warning` / `::error`, the latter carrying
  the subprocess's own message so a `402`
  surfaces the *why*) and, when the PR gets no posted review, fails the check (exit 2) instead of
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

[Unreleased]: https://github.com/storkme/second-opinion/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/storkme/second-opinion/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/storkme/second-opinion/releases/tag/v1.0.0
