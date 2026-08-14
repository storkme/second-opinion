"""Unit tests for the project-agnostic review core (review.py).

These lock the diff-filtering / glob-matching / prompt-construction contract that was
copied out of the upstream reviewer, since this package now lives without the harness
as oversight. Run with `pytest`.
"""
from second_opinion import review as rv

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,2 @@\n-old\n+new\n context\n"
    "diff --git a/pkg.lock b/pkg.lock\n"
    "--- a/pkg.lock\n+++ b/pkg.lock\n@@ -1 +1 @@\n-x\n+y\n"
)


def test_file_of_chunk_prefers_header_and_handles_dev_null():
    chunks = rv._split_by_file(DIFF)
    assert rv._file_of_chunk(chunks[0]) == "src/app.py"
    add = "diff --git a/new file.py b/new file.py\n--- /dev/null\n+++ b/new file.py\n"
    assert rv._file_of_chunk(add) == "new file.py"  # spaces + /dev/null fallback


def _chunk(path, body_lines):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1 @@\n" + "".join(f"+{l}\n" for l in body_lines))


def test_filter_diff_excludes_globs_and_lists_files():
    fd = rv.filter_diff(DIFF, ["**/*.lock"], 60000)
    assert fd.files == ["src/app.py"]      # pkg.lock dropped
    assert "pkg.lock" not in fd.text
    assert not fd.truncated
    # An excluded file is not "dropped by truncation" — it was never in scope.
    assert fd.dropped == [] and fd.clipped is None
    # full_text is the complete filtered diff: no cap, no trailer.
    assert "pkg.lock" not in fd.full_text
    assert rv.TRUNCATION_TRAILER not in fd.full_text


def test_filter_diff_truncates_at_whole_file_boundary():
    # Cap set exactly at the first chunk, so it fits whole and the second is dropped
    # entirely — no mid-hunk cut.
    first = rv._split_by_file(DIFF)[0]
    fd = rv.filter_diff(DIFF, [], max_chars=len(first))
    assert fd.truncated
    assert fd.files == ["src/app.py"]
    assert fd.dropped == ["pkg.lock"]
    assert fd.clipped is None           # nothing was cut mid-hunk
    assert "[... diff truncated for length ...]" in fd.text
    assert fd.missing_files == ["pkg.lock"] and fd.partial_files == []


def test_filter_diff_distinguishes_a_clipped_first_chunk_from_a_dropped_file():
    # This case previously read as identical to the one above: `truncated=True` and
    # `files == ["src/app.py"]`. It is not — here app.py is itself cut mid-hunk AND
    # pkg.lock is gone. The old three-tuple could not express the difference, and the
    # test that covered it asserted the wrong mechanism in its comment.
    fd = rv.filter_diff(DIFF, [], max_chars=80)
    assert fd.truncated
    assert fd.files == ["src/app.py"]
    assert fd.clipped == "src/app.py"   # cut mid-hunk, not carried whole
    assert fd.missing_files == ["pkg.lock"]


def test_filter_diff_reports_a_chunk_clipped_mid_hunk():
    # One file overflows the cap on its own: it IS in the excerpt, but cut mid-hunk.
    # `truncated` alone cannot distinguish this from a whole file being dropped.
    big = _chunk("src/big.py", [f"line {i}" for i in range(400)])
    fd = rv.filter_diff(big, [], max_chars=200)
    assert fd.truncated
    assert fd.files == ["src/big.py"]
    assert fd.clipped == "src/big.py"       # <- the fact callers had to guess
    assert fd.dropped == []
    assert fd.missing_files == [] and fd.partial_files == []


def test_filter_diff_tracks_dropped_per_chunk_not_per_filename():
    # A path can appear in several `diff --git` blocks (rename+modify, mode+content).
    # If the first fits and the second doesn't, the path is BOTH present and partial —
    # a filename-set difference reports it as neither.
    two = _chunk("src/a.py", ["one"]) + _chunk("src/a.py", [f"pad {i}" for i in range(200)])
    fd = rv.filter_diff(two, [], max_chars=len(_chunk("src/a.py", ["one"])) + 10)
    assert fd.truncated
    assert fd.files == ["src/a.py"]
    assert fd.dropped == ["src/a.py"]       # a second chunk of the same path vanished
    assert fd.partial_files == ["src/a.py"]  # present, but missing a later hunk
    assert fd.missing_files == []            # not wholly absent — don't say it is


def test_filter_diff_untruncated_full_text_equals_excerpt():
    fd = rv.filter_diff(DIFF, [], 60000)
    assert not fd.truncated
    assert fd.text == fd.full_text
    assert fd.dropped == [] and fd.clipped is None


def test_glob_semantics():
    assert rv.matches_glob("a/b/c.png", ["**/*.png"])
    assert rv.matches_glob("x.png", ["**/*.png"])          # leading **/ matches root
    assert rv.matches_glob("build/out/x.js", ["**/build/**"])
    assert not rv.matches_glob("src/a/b.js", ["src/*.js"])  # * does not cross /
    assert rv.matches_glob("src/b.js", ["src/*.js"])


def test_system_prompt_injects_project_clause_and_guidance():
    p = rv.system_prompt("acme", "- check the frobnicator")
    assert "for acme." in p
    assert "checked out in your current working directory" in p  # AGENTIC_CLAUSE
    assert "check the frobnicator" in p
    assert "for this codebase." in rv.system_prompt("", "x")  # natural fallback
    # empty guidance falls back, never leaves a bare placeholder
    assert "(none specified)" in rv.system_prompt("p", "")


def test_shuffle_inputs_is_deterministic_and_lossless():
    a = rv.shuffle_inputs(DIFF, 1)
    assert a == rv.shuffle_inputs(DIFF, 1)               # seeded → reproducible
    assert set(rv._split_by_file(a)) == set(rv._split_by_file(DIFF))  # no chunk lost
