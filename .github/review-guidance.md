# Review guidance — second-opinion

This repo's own review checklist, injected as a second pass for both the Claude reviewer
and the (dogfooded) second-opinion reviewer. Keep it tight; it encodes where *this*
codebase is most likely to break. Read `CLAUDE.md` first for the architecture and invariants.

## Bug classes to check for every applicable change

- **Provider / merge dispatch.** `PROVIDER` (review) and `MERGE_PROVIDER` (K>1 union) are
  independent axes. Check that `run_pass` uses `PI_PROVIDER`, that `merge_reviews` routes to
  the right backend (`local` → `LLAMA_SERVER_URL`, else OpenRouter), and that `_merge_model_for`
  resolves a sane model for every (PROVIDER, MERGE_PROVIDER) combo — especially the
  cross combos (local review + openrouter merge, and vice versa).
- **Model resolution & server-down.** For `PROVIDER=local`, the model is discovered from
  `LLAMA_SERVER_URL/v1/models`; `resolve_model()` returning `None` must SKIP the run (cron/
  daemon-safe), never crash or post an empty review.
- **Env-var contract.** New knobs must have a default and be validated where required.
  Provider-aware defaults (`K`: 1 openrouter / 3 local; `PASS_TIMEOUT_S`: 900 / 1800) must
  stay consistent between `run.py` and the docs/action inputs. Upfront credential checks in
  `main()` must cover both `PROVIDER` and `MERGE_PROVIDER`.
- **Defensive HTTP parsing.** `_chat()` must tolerate any malformed-but-200 envelope
  (empty `choices`, error/moderation shapes) and return `""` so the caller raises cleanly —
  never reintroduce brittle `r.json()["choices"][0]["message"]` indexing.
- **Marker idempotency.** The dedup is a *paginated* REST read matching the marker at the
  **start** of a comment body. Don't switch to `gh pr view --json comments` (truncates) or a
  loose substring match (a quoted marker would suppress the next review). One review per head SHA.
- **Secret handling.** `run_pass` strips `GITHUB_TOKEN`/`GH_TOKEN` from the pi subprocess;
  `providers.py` writes `models.json` mode `600`. Flag anything that widens secret exposure,
  logs a key, or removes the fork guard from a workflow. The OpenRouter key in `models.json`
  is a known, documented exposure — don't make it worse.
- **DEFAULT_MODEL single source.** The OpenRouter default lives once in `providers.py`
  (`action.yml` mirrors the literal as an input default). Flag re-duplication in Python.

## Conventions

- Python ≥ 3.11, standard library + `requests` only — no new runtime deps without reason.
- The core (`second_opinion/`) stays project-agnostic: no repo-specific constants, no
  coupling back to a delivery wrapper.
- Non-trivial logic ships with a test in `tests/` (subprocess/`requests` stubbed — no network).
- The reviewer is advisory and decorrelated: never a merge gate, never reads other reviews.
