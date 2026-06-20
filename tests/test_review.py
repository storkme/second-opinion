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


def test_filter_diff_excludes_globs_and_lists_files():
    out, files, truncated = rv.filter_diff(DIFF, ["**/*.lock"], 60000)
    assert files == ["src/app.py"]      # pkg.lock dropped
    assert "pkg.lock" not in out
    assert not truncated


def test_filter_diff_truncates_at_whole_file_boundary():
    out, files, truncated = rv.filter_diff(DIFF, [], max_chars=80)
    assert truncated
    assert files == ["src/app.py"]      # second file dropped whole, not cut mid-hunk
    assert "[... diff truncated for length ...]" in out


def test_glob_semantics():
    assert rv._excluded("a/b/c.png", ["**/*.png"])
    assert rv._excluded("x.png", ["**/*.png"])          # leading **/ matches root
    assert rv._excluded("build/out/x.js", ["**/build/**"])
    assert not rv._excluded("src/a/b.js", ["src/*.js"])  # * does not cross /
    assert rv._excluded("src/b.js", ["src/*.js"])


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
