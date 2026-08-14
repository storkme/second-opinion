"""Trivial-delta rules: what counts as "not worth another review", and — the half that
actually matters — everything that does not.

Every test that expects `review` is a test of the fail-open doctrine, so each one names
the ambiguity it stands for. A regression here does not fail loudly at runtime: it skips
a review, and the only evidence is a bug nobody reported.
"""

from second_opinion import delta


def _f(filename, status="modified", patch=None, **kw):
    entry = {"filename": filename, "status": status}
    if patch is not None:
        entry["patch"] = patch
    entry.update(kw)
    return entry


def _cmp(files, status="ahead"):
    return {"status": status, "files": files}


# --------------------------------------------------------------------- trivial

def test_docs_only_delta_is_trivial():
    v = delta.classify_compare(_cmp([
        _f("README.md", patch="@@ -1 +1 @@\n-old\n+new"),
        _f("docs/design.md", status="added"),
    ]))
    assert v.trivial and v.reason == "docs-or-comment-only", v


def test_comment_only_rust_is_trivial():
    patch = ("@@ -10,4 +10,5 @@ fn place(&self) {\n"
             "     let x = 1;\n"
             "-// old note\n"
             "+// new note, rewritten\n"
             "+\n"
             "+    /// doc line, indented\n")
    v = delta.classify_compare(_cmp([_f("src/bus/layout.rs", patch=patch)]))
    assert v.trivial, v


def test_mixed_docs_and_comment_only_code_is_trivial():
    v = delta.classify_compare(_cmp([
        _f("CHANGELOG.md", patch="@@ -1 +1 @@\n+- a bullet"),
        _f("src/lib.rs", patch="@@ -1 +1 @@\n-// a\n+// b"),
    ]))
    assert v.trivial and "2 changed file(s)" in v.detail, v


def test_empty_delta_is_trivial():
    # An empty commit: `ahead`, nothing changed. There is no material for a review to read.
    v = delta.classify_compare(_cmp([]))
    assert v.trivial and v.reason == "empty-delta", v


def test_trivial_globs_are_configurable_and_replace_the_default():
    files = [_f("docs/notes.txt", patch="@@ -1 +1 @@\n+prose")]
    assert not delta.classify_compare(_cmp(files)).trivial
    assert delta.classify_compare(_cmp(files), ["docs/**"]).trivial
    # ...and an override replaces the default rather than adding to it, so a consumer
    # that lists only its own doc tree does not silently keep waving *.md through.
    md = [_f("README.md", patch="@@ -1 +1 @@\n+prose")]
    assert not delta.classify_compare(_cmp(md), ["docs/**"]).trivial


# ---------------------------------------------------------------------- review

def test_a_trailing_comment_edit_on_a_code_line_is_code():
    # The tempting near-miss: the CHANGE is a comment, the LINE is code. Both patch lines
    # are real code lines, and one of them now behaves differently for all anyone knows.
    patch = ("@@ -3,1 +3,1 @@\n"
             "-    let n = rows.len();   // rows\n"
             "+    let n = rows.len() + 1; // rows, plus the header\n")
    v = delta.classify_compare(_cmp([_f("src/lib.rs", patch=patch)]))
    assert not v.trivial and v.reason == "code-in-delta", v
    assert "src/lib.rs" in v.detail


def test_a_new_file_of_pure_comments_still_reviews():
    # `added`, not `modified`: a whole new file is a new file, and its patch is not
    # evidence about what the file DOES in the repo (mod declarations, build wiring).
    v = delta.classify_compare(_cmp([
        _f("src/new.rs", status="added", patch="@@ -0,0 +1,2 @@\n+// a\n+// b")]))
    assert not v.trivial and "not an in-place modification" in v.detail, v


def test_a_deleted_comment_only_file_still_reviews():
    v = delta.classify_compare(_cmp([
        _f("src/gone.rs", status="removed", patch="@@ -1,2 +0,0 @@\n-// a\n-// b")]))
    assert not v.trivial, v


def test_a_rename_reviews_even_when_the_patch_is_empty():
    v = delta.classify_compare(_cmp([
        _f("docs/new-name.md", status="renamed", previous_filename="docs/old-name.md")]))
    assert not v.trivial and "renamed" in v.detail, v


def test_a_language_the_gate_cannot_read_reviews():
    # Python's `#` is not in the default table on purpose: a line starting with `#` inside
    # a docstring is idiomatic, so the "comment" test would be wrong far too often.
    v = delta.classify_compare(_cmp([
        _f("scripts/gen.py", patch="@@ -1 +1 @@\n-# a\n+# b")]))
    assert not v.trivial and "comments in" in v.detail, v


def test_a_prefix_table_is_all_it_takes_to_teach_it_a_language():
    # The extensibility claim, tested rather than asserted in a comment.
    files = [_f("scripts/gen.py", patch="@@ -1 +1 @@\n-# a\n+# b")]
    v = delta.classify_compare(_cmp(files), prefixes={".py": "#"})
    assert v.trivial, v


def test_diverged_history_reviews():
    for status in ("diverged", "behind", "identical", None):
        v = delta.classify_compare(_cmp([_f("README.md")], status=status))
        assert not v.trivial and v.reason == "history-diverged", (status, v)


def test_a_file_list_at_the_api_limit_reviews():
    files = [_f(f"docs/f{i}.md", patch="@@ -1 +1 @@\n+x")
             for i in range(delta.COMPARE_FILE_LIMIT)]
    v = delta.classify_compare(_cmp(files))
    assert not v.trivial and v.reason == "file-list-truncated", v
    # One under the limit is a complete list, so the same files are trivial.
    assert delta.classify_compare(_cmp(files[:-1])).trivial


def test_a_missing_patch_reviews():
    # GitHub omits `patch` for binaries and for diffs it deems too large. No evidence,
    # no skip.
    for entry in (_f("src/lib.rs"), _f("src/lib.rs", patch=""), _f("src/lib.rs", patch=None)):
        v = delta.classify_compare(_cmp([entry]))
        assert not v.trivial and "without a patch" in v.detail, v


def test_a_malformed_payload_reviews_instead_of_raising():
    for payload in (None, [], "nope", {"status": "ahead"}, {"status": "ahead", "files": {}}):
        v = delta.classify_compare(payload)
        assert not v.trivial, payload
    v = delta.classify_compare(_cmp(["not-a-dict"]))
    assert not v.trivial and "malformed" in v.detail, v


def test_one_code_file_among_many_docs_reviews_and_names_it():
    files = [_f(f"docs/f{i}.md", patch="@@ -1 +1 @@\n+x") for i in range(20)]
    files.insert(11, _f("src/solver.rs", patch="@@ -1 +1 @@\n-let a = 1;\n+let a = 2;"))
    v = delta.classify_compare(_cmp(files))
    assert not v.trivial and "src/solver.rs" in v.detail, v


# ------------------------------------------------------- the patch reader itself

def test_added_content_that_looks_like_a_diff_header_is_content():
    # `+++ x` in a patch is an ADDED line whose text is `++ x`, because a compare-API
    # patch starts at the first @@ and carries no file headers. Skipping "headers" here
    # would wave a genuine code line through — the one direction that must never happen.
    assert not delta.is_comment_only_patch("@@ -1 +1 @@\n+++ x\n", "//")
    assert not delta.is_comment_only_patch("@@ -1 +1 @@\n--- y\n", "//")


def test_hunk_headers_context_and_no_newline_markers_are_not_changes():
    patch = ("@@ -1,3 +1,3 @@ impl Foo {\n"
             "     let untouched = 1;\n"
             "-// before\n"
             "+// after\n"
             "\\ No newline at end of file\n")
    assert delta.is_comment_only_patch(patch, "//")


def test_a_blank_added_line_is_not_code():
    assert delta.is_comment_only_patch("@@ -1 +1 @@\n+\n+   \n-\n", "//")


def test_a_block_comment_opener_counts_as_code():
    # Conservative on purpose: recognising /* … */ needs state this gate does not keep.
    assert not delta.is_comment_only_patch("@@ -1 +1 @@\n+/* a block */\n", "//")
    assert not delta.is_comment_only_patch("@@ -1 +1 @@\n+ * continued\n", "//")


def test_extension_matching_is_case_insensitive_and_ignores_dotfiles():
    assert delta._ext("src/Foo.RS") == ".rs"
    assert delta._ext(".gitignore") == ""
    assert delta._ext("Makefile") == ""
    v = delta.classify_compare(_cmp([_f("src/Foo.RS", patch="@@ -1 +1 @@\n-// a\n+// b")]))
    assert v.trivial, v
