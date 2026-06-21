# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). See the release procedure in
[CLAUDE.md](CLAUDE.md#changelog--releases).

## [Unreleased]

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

[Unreleased]: https://github.com/storkme/second-opinion/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/storkme/second-opinion/releases/tag/v1.0.0
