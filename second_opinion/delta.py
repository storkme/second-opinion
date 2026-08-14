"""Is the delta since the last review worth another review? — the trivial-delta rules.

A review costs a model call and 10-20 minutes of wall clock, and it re-reads the WHOLE PR
diff every time. A push that only edits prose or comments therefore buys nothing: the
review it triggers can only re-find what the previous one already reported, which is how
a repo whose conventions generate doc-only pushes (decision logs, comment sweeps) ends up
paying for four re-reviews of one PR.

This module holds the rules and nothing else: everything here is a pure function over a
GitHub *compare* payload, so the whole decision is unit-testable with no network. The API
calls, the marker lookup, and the skip comment live in `run.py`.

The doctrine is deliberately asymmetric. A wrong "review" costs one model call; a wrong
"trivial" means a code change ships unreviewed — the silent-failure class this project
exists to avoid. So every ambiguity resolves to REVIEW: an unknown compare status, a
possibly-truncated file list, a rename, a missing patch, a file type nobody taught this
module about. There is no configuration that can invert that.

Two residual over-skips are accepted and documented rather than papered over, because
detecting them needs a parser this gate should not grow:

- a changed line that begins with the comment token but sits INSIDE a string literal
  (`let s = "\n// not a comment";` split across lines) reads as a comment here;
- Rust `///` doc comments are treated as comments, but their fenced examples compile and
  run as doctests, so a doctest edit can slip through as prose.

Both are bounded by the same accumulation property that makes the gate safe overall: a
skip never advances the review baseline (`run.py` posts a *different* marker), so the
next push that does touch code buys a review of the entire accumulated delta, including
whatever was waved through here.
"""
from __future__ import annotations

from typing import NamedTuple

from .review import matches_glob

# GitHub's compare API returns at most 300 entries in `files` and reports no total, so a
# list AT the limit may be a prefix of the real change. Nothing in the payload can tell a
# 300-file change from a 900-file one, and "the ones I cannot see are probably docs" is
# precisely the assumption that must never be made silently.
COMPARE_FILE_LIMIT = 300

# Paths whose content is prose by construction. Consumers extend this (TRIVIAL_GLOBS /
# the `trivial-globs` input) with their own doc trees; the glob syntax is the one
# `EXCLUDE_GLOBS` already uses, where `**` crosses `/`.
DEFAULT_TRIVIAL_GLOBS = ["**/*.md"]

# Extensions whose line comments this gate can recognise, mapped to the token that starts
# one. Extending it is a one-line change — that is the whole reason it is a table.
#
# Only `//` languages ship by default, and that is a judgement about RISK, not effort.
# The one way this gate can be wrong in the dangerous direction is a changed line that
# begins with the comment token inside a multi-line string, and in `#` languages that is
# idiomatic rather than exotic: a `#` heading inside a Python docstring, a comment-looking
# line in a shell heredoc, a `#` in a YAML block scalar. `//` at the start of a line
# inside a Rust raw string or a JS template literal is rare enough to price in.
_SLASH_SLASH = (
    ".rs", ".go", ".java", ".kt", ".kts", ".scala", ".swift", ".dart", ".zig",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".cs",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".proto",
)
LINE_COMMENT_PREFIXES = {ext: "//" for ext in _SLASH_SLASH}


class Verdict(NamedTuple):
    """Whether the delta is trivial, plus why — in both registers.

    `reason` is a stable slug (`code-in-delta`, `history-diverged`, …): it is what the log
    line, the metrics field and the tests key on, so it must not drift with wording.
    `detail` is the human sentence naming the file or status that decided it — the part an
    operator reads when a skip looks wrong."""
    trivial: bool
    reason: str
    detail: str = ""


def _ext(path: str) -> str:
    """Lowercased extension of a path, or "" — dotfiles have none (`.gitignore` is a name)."""
    base = path.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


# A line comment is prose — unless it is a DIRECTIVE. `//go:build`, `// +build`,
# `/// <reference …>`, `// @ts-expect-error`, `//# sourceMappingURL=`, `//nolint:all` and
# `// eslint-disable-next-line` are read by a compiler, a bundler or a linter: editing one
# changes what the code DOES while looking exactly like editing prose. `.go`, `.ts` and
# `.js` are in the default table, so this is reachable rather than theoretical — and the
# accumulation property does not rescue it if a directive-only edit is the LAST push
# before a merge. (Raised by the review bot on PR #47.)
#
# Matched by shape first and by name only where shape cannot: a directive either carries
# punctuation prose does not use in that position (`@ < # +`), or attaches its argument to
# the token with no space (`//go:build`, `//nolint:all`), or is one of a short list of
# tool names. Prose keeps the space — `// NOTE: …`, `// SAFETY: …` and `/// doc line` stay
# comments, which matters because that is what the primary consumer's comments look like.
#
# Note for anyone adding a `#`-comment language to the table: `# type: ignore`,
# `# noqa`, `# pylint: disable` and `#!/usr/bin/env` are the same class and these rules do
# NOT all carry over — the space-attached test in particular does not fire on them.
_DIRECTIVE_WORDS = ("eslint", "tslint", "stylelint", "jshint", "jslint",
                    "prettier-ignore", "biome-ignore", "istanbul", "c8", "v8",
                    "deno-lint-ignore", "nolint", "noinspection", "clang-format",
                    "swiftlint", "sourcemappingurl")


def _is_directive(body: str, prefix: str) -> bool:
    """True when a whole-line comment is machine-readable rather than prose."""
    rest = body[len(prefix):].lstrip("/!")   # /// and //! are doc forms, still prose
    attached = bool(rest) and not rest[:1].isspace()
    text = rest.lstrip()
    if not text:
        return False
    if text[0] in "@<+":
        return True
    if text[0] == "#" and attached:
        # `//#sourceMappingURL=…` is a directive; `/// # Examples` is a markdown heading in
        # a Rust doc comment, which is prose and very common in the repos this targets.
        # Only the attached form is the directive.
        return True
    word = text.split()[0].lower()
    # `//go:build`, `//nolint:all` — no space, and a colon inside the first word. The
    # no-space half is what keeps `// NOTE: …` prose; `//TODO: …` reads as a directive and
    # buys a review it does not need, which is the harmless direction.
    if attached and ":" in word:
        return True
    return word.startswith(_DIRECTIVE_WORDS)


def is_comment_only_patch(patch: str, prefix: str) -> bool:
    """True when every ADDED or REMOVED line in a unified-diff patch is blank or an
    entire-line comment.

    Deliberately no `+++`/`--- ` header skipping. A compare-API `patch` starts at the
    first `@@` hunk header and carries no file headers, so there is nothing to skip —
    while skipping them would misread a genuine added line whose own content starts with
    `++` (it appears as `+++ …` in the patch) as a header and wave it through. The
    asymmetry settles it: mistaking a header for content costs one review, mistaking
    content for a header costs the review.

    A trailing comment on a code line (`foo(); // now n+1`) counts as CODE — the line does
    not *start* with the token, so its file leaves the trivial set. Ditto a line that
    opens a block comment (`/* …`), which is conservative and stays that way, and a
    whole-line *directive* (`//go:build`, `// eslint-disable-next-line`), which is a
    comment to the eye and an instruction to a toolchain — see `_is_directive`."""
    for line in patch.splitlines():
        if not line or line[0] not in "+-":
            continue  # hunk headers, context lines, "\ No newline at end of file"
        body = line[1:].strip()
        if not body:
            continue
        if body.startswith(prefix) and not _is_directive(body, prefix):
            continue
        return False
    return True


def classify_file(entry: dict, trivial_globs: list[str], prefixes: dict) -> str:
    """"" when this changed file is trivial, else a sentence naming why it is not."""
    if not isinstance(entry, dict):
        return "the compare payload carried a malformed file entry"
    path = entry.get("filename") or ""
    if not path:
        return "the compare payload carried a file entry with no filename"
    if entry.get("previous_filename"):
        # A rename's patch describes the delta against the NEW path, so a pure rename can
        # look empty. Whether the move itself matters is a judgement about the repo, not
        # about the patch — so it goes to the reviewer.
        return f"{path} was renamed"
    if matches_glob(path, trivial_globs):
        return ""
    prefix = prefixes.get(_ext(path))
    if not prefix:
        return f"{path} is neither a docs path nor a language this gate can read comments in"
    status = entry.get("status")
    if status != "modified":
        # added / removed / copied / changed / unchanged, or something GitHub adds later.
        # A whole new file of comments is still a new file, and a deleted one may have
        # been code; only an in-place edit is provably comment-only from its patch.
        return f"{path} is {status or 'of an unknown status'}, not an in-place modification"
    patch = entry.get("patch")
    if not isinstance(patch, str) or not patch:
        # GitHub omits `patch` for binary files and for diffs it considers too large. No
        # patch means no evidence, and absence of evidence buys a review.
        return f"{path} came back without a patch, so its content is unknown"
    if not is_comment_only_patch(patch, prefix):
        return f"{path} changes code, not only comments"
    return ""


def classify_compare(payload, trivial_globs: list[str] | None = None,
                     prefixes: dict | None = None) -> Verdict:
    """Classify a GitHub compare payload (`GET /repos/{repo}/compare/{base}...{head}`).

    Never raises on a shape it did not expect: a payload this cannot read is a reason to
    review, not a reason to crash the reviewer."""
    globs = DEFAULT_TRIVIAL_GLOBS if trivial_globs is None else trivial_globs
    table = LINE_COMMENT_PREFIXES if prefixes is None else prefixes
    if not isinstance(payload, dict):
        return Verdict(False, "malformed-compare", "the compare payload was not an object")
    status = payload.get("status")
    if status != "ahead":
        # "behind" / "diverged" / "identical" all mean the reviewed head is not simply an
        # ancestor of this one — a force-push, a rebase, or a base change. The listed
        # files then describe something other than "what was pushed since the review".
        return Verdict(False, "history-diverged",
                       f"compare status is {status!r}, not 'ahead' — the reviewed head is "
                       f"not an ancestor of this one (force-push, rebase, or base change)")
    files = payload.get("files")
    if not isinstance(files, list):
        return Verdict(False, "malformed-compare", "the compare payload carried no file list")
    if len(files) >= COMPARE_FILE_LIMIT:
        return Verdict(False, "file-list-truncated",
                       f"the compare API returned {len(files)} files, its per-page maximum, "
                       f"so the change may be larger than this list")
    if not files:
        # `ahead` with no files: an empty commit, or a commit whose content the compare
        # collapsed. There is nothing a reviewer could read.
        return Verdict(True, "empty-delta", "empty — no files changed at all")
    for entry in files:
        why = classify_file(entry, globs, table)
        if why:
            return Verdict(False, "code-in-delta", why)
    # Phrased as a noun clause, not a sentence: it is quoted mid-sentence in the skip
    # comment a human reads ("the delta … is <detail>"), and also stands alone in the log.
    return Verdict(True, "docs-or-comment-only",
                   f"{len(files)} changed file(s), all documentation or comment-only")
