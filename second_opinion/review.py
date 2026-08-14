"""Prompt construction + diff filtering — the project-agnostic review core.

Lifted from the sisyphus pr-review reviewer and decoupled from it: no repo-specific
constants, no dependency on the experiment harness. The agentic system-prompt clause
(formerly in the harness) lives here now, since it IS the core prompt.
"""
from __future__ import annotations

import random
import re
from typing import NamedTuple

# The agentic reviewer reads the checked-out repo with tools, so it's told to verify
# in context — and explicitly NOT to read other reviewers' comments (decorrelation).
AGENTIC_CLAUSE = (
    "The full repository is checked out in your current working directory at the "
    "PR's head commit. Read any file and grep freely to understand the change in "
    "context — including callers, existing tests the change might break, and "
    "symbol definitions OUTSIDE the changed files. Do NOT read other reviewers' "
    "comments or use `gh` to fetch PR discussion, and do NOT edit/commit/push "
    "anything — this is a blind, read-only, independent second opinion."
)

SYSTEM_TEMPLATE = """\
You are a rigorous, skeptical code reviewer for {project}.

{context_clause}

Your job is to FIND PROBLEMS, not to confirm the change is fine. Assume the diff
contains at least one bug, risk, or oversight and hunt for it. Verifying that the
code looks correct is NOT a review — if you conclude "no issues", you have
probably not looked hard enough.

Work in TWO passes, in order:

PASS 1 — Open investigation. Independently hunt for issues across:
- Correctness & logic: edge cases, off-by-one, boundary conditions (>= vs >),
  null/empty/zero handling, error and failure paths, concurrency / race conditions.
- Consistency: does the change contradict related code you can see? Two
  definitions of the same thing that disagree, a value computed one way here and
  another there, mismatched units, a constraint/index declared two ways.
- Ripple effects: does it break existing callers or existing tests? Do added
  tests actually exercise the new behaviour, or pass trivially?
- Security: leaked secrets, auth gaps, unsafe input handling.

PASS 2 — Checklist. Go through the project-specific checklist below and, for EACH
item, explicitly check whether it applies to this change. These encode bug classes
that have ACTUALLY shipped in this repo — treat a match as a likely real finding,
not a hypothetical. The checklist is additive; do not let it narrow Pass 1.

{conventions}

Ground every finding in specific code — cite path:line and the concrete failure.
Don't invent speculative problems you can't point to, and skip pure style nits;
but DO report real correctness/consistency issues even when they are minor.

Output format (Markdown):
- One short summary line (your overall verdict).
- Then one bullet per finding, most severe first:
  `**[blocker|major|minor]** path:line — the issue and a concrete fix`
- Only if, after working BOTH passes, you truly find nothing, write
  `No issues found.` followed by a one-line note of what you verified.
"""


def system_prompt(project: str, conventions: str) -> str:
    """The agentic reviewer system prompt for a given project + its guidance."""
    return SYSTEM_TEMPLATE.format(
        project=project or "this codebase",
        context_clause=AGENTIC_CLAUSE,
        conventions=conventions.strip() or "(none specified)",
    )


# --------------------------------------------------------------- diff filtering

def _file_of_chunk(chunk: str) -> str:
    """Path of the file a diff chunk touches. Prefer the +++/--- header (handles
    spaces in names); fall back to the `diff --git` line for binary/rename chunks."""
    new = old = None
    for line in chunk.splitlines():
        if line.startswith("@@"):
            break
        if line.startswith("+++ "):
            new = line[4:].strip()
        elif line.startswith("--- "):
            old = line[4:].strip()
    for cand in (new, old):
        if cand and cand != "/dev/null":
            return cand[2:] if cand.startswith(("a/", "b/")) else cand
    first = chunk.splitlines()[0] if chunk else ""
    m = re.match(r"diff --git a/(.+) b/\1$", first) or re.match(r"diff --git a/(.+) b/", first)
    return m.group(1).strip() if m else first


def _split_by_file(diff: str) -> list[str]:
    chunks: list[str] = []
    cur: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and cur:
            chunks.append("".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        chunks.append("".join(cur))
    return chunks


_GLOB_CACHE: dict[str, re.Pattern] = {}


def _glob_to_re(pat: str) -> re.Pattern:
    """Compile a path glob where `**` crosses `/`, `*`/`?` do not, and a leading
    `**/` also matches zero directories."""
    cached = _GLOB_CACHE.get(pat)
    if cached is not None:
        return cached
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        if pat[i] == "*":
            if pat[i:i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
            elif pat[i:i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    rx = re.compile("^" + "".join(out) + "$")
    _GLOB_CACHE[pat] = rx
    return rx


def matches_glob(path: str, globs: list[str]) -> bool:
    """True when `path` matches any glob in `globs`. Public because two callers now need
    the same matcher over the same syntax: the diff filter's EXCLUDE_GLOBS and the
    trivial-delta gate's TRIVIAL_GLOBS (`delta.py`)."""
    return any(_glob_to_re(g).match(path) for g in globs)


# Appended to a truncated diff. Exported so callers measuring coverage can subtract it
# instead of comparing an excerpt that carries it against a full diff that doesn't.
TRUNCATION_TRAILER = "\n\n[... diff truncated for length ...]\n"


class FilteredDiff(NamedTuple):
    """What `filter_diff` actually did — not merely that it did something.

    The old return was `(text, files, truncated)`, which said an excerpt was capped but
    never *how*: which chunks vanished, whether the last kept one was cut mid-hunk, or
    whether a path had a second chunk that got dropped. Callers had to infer all three,
    and every inference was wrong in some shape, each one surfacing as "partial coverage
    described as full". Reporting the facts here is what stops callers guessing."""
    text: str                 # the excerpt, carrying TRUNCATION_TRAILER when truncated
    files: list[str]          # paths of chunks present in the excerpt, in order
    truncated: bool
    dropped: list[str]        # paths of chunks NOT in the excerpt, in order. A path can
                              # appear here *and* in `files` when it has several chunks.
    clipped: str | None       # path of the chunk cut mid-hunk, if any
    full_text: str            # every non-excluded chunk, uncapped, no trailer

    @property
    def missing_files(self) -> list[str]:
        """Paths absent from the excerpt entirely (deduped, order preserved)."""
        seen = set(self.files)
        out: list[str] = []
        for p in self.dropped:
            if p not in seen and p not in out:
                out.append(p)
        return out

    @property
    def partial_files(self) -> list[str]:
        """Paths present in the excerpt but missing a later chunk (deduped)."""
        seen = set(self.files)
        out: list[str] = []
        for p in self.dropped:
            if p in seen and p not in out:
                out.append(p)
        return out


def filter_diff(diff: str, exclude_globs: list[str], max_chars: int) -> FilteredDiff:
    """Drop excluded/generated files and cap total size at whole-file boundaries.

    Excluded files are not "dropped" — they were never in scope; `dropped` means material
    the cap removed, which is what a caller has to disclose."""
    out: list[str] = []
    files: list[str] = []
    kept: list[str] = []
    dropped: list[str] = []
    clipped: str | None = None
    total = 0
    truncated = False
    capping = False
    for chunk in _split_by_file(diff):
        path = _file_of_chunk(chunk)
        if matches_glob(path, exclude_globs):
            continue
        kept.append(chunk)
        if capping:
            # Past the cap: keep walking so `dropped` and `full_text` are complete rather
            # than stopping at the first overflow and reporting an unknown remainder.
            dropped.append(path)
            continue
        if total + len(chunk) <= max_chars:
            out.append(chunk)
            files.append(path)
            total += len(chunk)
        elif not out:
            head = chunk[:max_chars]
            head = head[:head.rfind("\n") + 1] or head
            out.append(head)
            files.append(path)
            clipped = path
            truncated = capping = True
        else:
            dropped.append(path)
            truncated = capping = True
    joined = "".join(out)
    if truncated:
        joined += TRUNCATION_TRAILER
    return FilteredDiff(text=joined, files=files, truncated=truncated, dropped=dropped,
                        clipped=clipped, full_text="".join(kept))


def reorder_unseen_first(diff: str, unseen: list[str]) -> str:
    """Reorder per-file chunks so the `unseen` paths come first.

    The on-disk full diff is consumed by an agent whose read tool truncates (pi: 2000
    lines or 50KB, whichever hits first) — and that file is larger than the cap by
    construction, since it only exists when the prompt excerpt overflowed. Left in git
    path order, the agent's first read would return the very files the excerpt already
    carried, so it could read "the complete diff" and see nothing new. Putting the
    missing files first means one read lands on material the excerpt lacked.

    The unseen chunks are further ordered SMALLEST FIRST, because the read cap is a
    budget and one huge file spends all of it. Unseen-first alone is not enough: if the
    first unseen chunk is a large generated file it consumes the whole read and the agent
    still sees one file. Smallest-first spends that same budget on many, which is the
    difference between "reachable" and "reachable in practice". The agent is told to page
    for the rest either way."""
    want = set(unseen)
    chunks = _split_by_file(diff)
    first = sorted((c for c in chunks if _file_of_chunk(c) in want), key=len)
    rest = [c for c in chunks if _file_of_chunk(c) not in want]
    return "".join(first + rest)


def shuffle_inputs(filtered: str, seed: int) -> str:
    """Shuffle per-file diff chunk order for between-pass diversity (K>1)."""
    chunks = _split_by_file(filtered)
    random.Random(seed).shuffle(chunks)
    return "".join(chunks)


# Sensible defaults; consumers override via EXCLUDE_GLOBS.
DEFAULT_EXCLUDE_GLOBS = [
    "**/*.lock", "**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml",
    "**/Cargo.lock", "**/go.sum", "**/*.min.js", "**/*.map",
    "**/build/**", "**/dist/**", "**/node_modules/**", "**/vendor/**",
    "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.webp", "**/*.gif", "**/*.svg",
    "**/*.pdf", "**/*.ico",
]
