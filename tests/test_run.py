"""Smoke tests for the fragile orchestration in run.py: merge HTTP-response parsing
and the marker dedup query. Subprocess/requests are stubbed — no network. Run with
`pytest` (or directly: `python -m tests.test_run`).
"""

import os
import sys
import tempfile
import types

os.environ.setdefault("GITHUB_REPO", "o/r")
os.environ.setdefault("GITHUB_TOKEN", "t")
os.environ.setdefault("OPENROUTER_API_KEY", "sk")
sys.modules.setdefault("requests", types.ModuleType("requests"))

from second_opinion import run  # noqa: E402
from tests.fakes import FakeProc  # noqa: E402

# Snapshot before any test swaps run._annotate for a capture stub (file convention:
# module-global patches are not restored).
_REAL_ANNOTATE = run._annotate


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def test_merge_reviews_parses_and_strips_content():
    run.requests.post = lambda *a, **k: _Resp(
        {"choices": [{"message": {"content": " merged "}}]}
    )
    assert run.merge_reviews(1, "t", ["pass a", "pass b"]) == "merged"


def test_merge_reviews_retries_once_on_empty_then_succeeds():
    # A single flake must not cost the run its review: attempt 1 empty, attempt 2 good.
    calls = []

    def post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return _Resp({"choices": []})
        return _Resp({"choices": [{"message": {"content": "merged"}}]})

    run.requests.post = post
    ann = _capture_annotations()
    try:
        assert run.merge_reviews(1, "t", ["a", "b"]) == "merged"
        assert len(calls) == 2, "expected exactly one retry"
        assert ann == [], "a recovered merge must not annotate"
    finally:
        run._annotate = _REAL_ANNOTATE


def test_merge_reviews_falls_back_to_raw_passes_after_two_failures():
    # Every malformed-but-200 envelope, plus a raising transport. merge_reviews must never
    # raise — the passes are the review, so they get posted unmerged with a loud warning.
    for payload in ({"choices": []}, {"error": {"message": "bad"}}, {}, {"choices": [{}]}):
        run.requests.post = lambda *a, p=payload, **k: _Resp(p)
        ann = _capture_annotations()
        try:
            out = run.merge_reviews(1, "t", ["finding a", "finding b"])
            assert "finding a" in out and "finding b" in out, payload
            assert "union merge unavailable" in out, payload
            assert [lvl for lvl, _ in ann] == ["warning"], payload
        finally:
            run._annotate = _REAL_ANNOTATE

    def boom(*a, **k):
        raise RuntimeError("connection reset")

    run.requests.post = boom
    ann = _capture_annotations()
    try:
        out = run.merge_reviews(1, "t", ["finding a", "finding b"])
        assert "finding a" in out and "finding b" in out
        assert [lvl for lvl, _ in ann] == ["warning"]
        assert "raised RuntimeError" in ann[0][1]
    finally:
        run._annotate = _REAL_ANNOTATE


def test_merge_reviews_annotation_reports_both_attempt_failures():
    # The attempts can fail differently, and the difference is the operator-actionable
    # part: a 402 then an empty 200 is credits exhaustion — a persistent condition that
    # recurs every sweep — not "the model flaked". Collapsing to the last reason hides it.
    calls = []

    def post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("402 requires more credits")
        return _Resp({"choices": []})

    run.requests.post = post
    ann = _capture_annotations()
    try:
        out = run.merge_reviews(1, "t", ["finding a", "finding b"])
        assert "finding a" in out
        msg = ann[0][1]
        assert "attempt 1 raised RuntimeError" in msg and "402" in msg, msg
        assert "attempt 2 returned no usable content" in msg, msg
    finally:
        run._annotate = _REAL_ANNOTATE


def test_merge_reviews_flags_the_unmerged_fallback_to_the_caller():
    # The posted header must not advertise a "union ×K" that never happened.
    run.requests.post = lambda *a, **k: _Resp({"choices": []})
    ann = _capture_annotations()
    try:
        meta: dict = {}
        run.merge_reviews(1, "t", ["a", "b"], meta=meta)
        assert meta.get("merged") is False, meta
    finally:
        run._annotate = _REAL_ANNOTATE

    # ...and a successful merge leaves the flag alone, so the caller's default holds.
    run.requests.post = lambda *a, **k: _Resp(
        {"choices": [{"message": {"content": "merged"}}]})
    meta = {}
    assert run.merge_reviews(1, "t", ["a", "b"], meta=meta) == "merged"
    assert meta.get("merged", True) is True, meta


def test_review_pr_header_says_unmerged_when_the_merge_fell_back():
    import contextlib
    import io
    real_k, real_pass, real_merge = run.K, run.run_pass, run.merge_reviews
    real_provider = run.PROVIDER
    run.PROVIDER = "openrouter"
    run.K = 2
    real_deps = _stub_review_pr_deps()

    def fallback_merge(pr, title, passes, merge_model=None, meta=None):
        if meta is not None:
            meta["merged"] = False
        return "RAW PASSES"

    run.merge_reviews = fallback_merge
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult("text", "ok")
    _capture_annotations()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=True)
        body = buf.getvalue()
        assert "×2 unmerged" in body, body
        assert "union ×2" not in body, body
    finally:
        run.K, run.run_pass, run.merge_reviews = real_k, real_pass, real_merge
        run.PROVIDER = real_provider
        _restore_review_pr_deps(real_deps)
        run._annotate = _REAL_ANNOTATE


def test_posted_body_is_clipped_under_githubs_comment_limit():
    # The unmerged fallback concatenates K raw passes with no dedup, so it is strictly
    # larger than the merged body would have been. Over GitHub's 65536-char cap the post
    # 422s, the exception escapes review_pr, and sweep files it as a silent failure —
    # exit 2 with NO review, the exact outcome the fallback exists to prevent.
    import contextlib
    import io
    real_k, real_pass, real_merge = run.K, run.run_pass, run.merge_reviews
    real_provider = run.PROVIDER
    run.PROVIDER = "openrouter"
    run.K = 3
    real_deps = _stub_review_pr_deps()
    huge = "x" * 40000
    run.merge_reviews = lambda pr, title, passes, merge_model=None, meta=None: "\n".join(passes)
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult(huge, "ok")
    ann = _capture_annotations()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            out = run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=True)
        assert out.posted is True
        # The dry-run print wraps the body in banners, so bound the body itself.
        body = buf.getvalue()
        assert len(body) < run.COMMENT_MAX + 2000, len(body)
        assert "truncated to fit GitHub's comment size limit" in body
        assert any("clipped" in m for _lvl, m in ann), ann
    finally:
        run.K, run.run_pass, run.merge_reviews = real_k, real_pass, real_merge
        run.PROVIDER = real_provider
        _restore_review_pr_deps(real_deps)
        run._annotate = _REAL_ANNOTATE


def test_clip_review_body_preserves_short_bodies_and_marker_position():
    # A body that fits is returned byte-identical (no gratuitous rewriting), and clipping
    # only ever touches the body — the marker leads the comment and dedup is a startswith.
    assert run._clip_review_body("short", reserved=100) == "short"
    clipped = run._clip_review_body("y" * 5000, reserved=0, limit=1000)
    assert len(clipped.encode("utf-8")) <= 1000
    assert clipped.startswith("yyy") and "truncated" in clipped
    # The result must never exceed the room it was given, at any limit — including the
    # window where there isn't even room for the truncation notice, and the degenerate
    # case where the header/footer alone fill the cap. Budget is UTF-8 BYTES, so a
    # multi-byte body must not sneak over the cap by being short in code points.
    for body in ("z" * 5000, "🤖" * 2000, "é—×" * 1500):
        for limit in range(0, 400, 7):
            out = run._clip_review_body(body, reserved=0, limit=limit)
            assert len(out.encode("utf-8")) <= max(0, limit), (body[:2], limit, out)
            out.encode("utf-8").decode("utf-8")  # never splits a character


def test_posted_comment_fits_the_cap_in_bytes_not_just_code_points():
    # The assembled comment is not ASCII — the header alone carries 🤖/—/× — so a
    # code-point budget would pass here while the real byte length overflowed.
    import contextlib
    import io
    real_k, real_pass, real_merge = run.K, run.run_pass, run.merge_reviews
    real_provider = run.PROVIDER
    run.PROVIDER = "openrouter"
    run.K = 1
    real_deps = _stub_review_pr_deps()
    # Every char is 4 UTF-8 bytes: 40000 code points = 160000 bytes, way over the cap.
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult("🤖" * 40000, "ok")
    _capture_annotations()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            out = run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=True)
        assert out.posted is True
        printed = buf.getvalue()
        start = printed.index("<!-- second-opinion sha=")
        # The dry-run print wraps the body in "\n{body}\n"; production writes exactly
        # `body` via --body-file. Strip the added newline so the assertion matches the
        # real cap rather than rejecting a body one byte under it.
        comment = printed[start:].rstrip("\n")
        assert len(comment.encode("utf-8")) <= run.COMMENT_MAX, len(comment.encode("utf-8"))
    finally:
        run.K, run.run_pass, run.merge_reviews = real_k, real_pass, real_merge
        run.PROVIDER = real_provider
        _restore_review_pr_deps(real_deps)
        run._annotate = _REAL_ANNOTATE


def test_merge_reviews_accumulates_cost_across_a_retried_attempt():
    # The expensive failure mode is a reasoning-burn empty: it bills full freight and
    # returns nothing. Reporting only the winning attempt would understate spend on
    # exactly the runs that cost the most, so meta must accumulate across attempts.
    calls = []

    def post(*a, **k):
        calls.append(1)
        usage = {"cost": 0.02, "total_tokens": 1000,
                 "completion_tokens_details": {"reasoning_tokens": 900}}
        if len(calls) == 1:
            return _Resp({"choices": [{"message": {"content": ""}}], "usage": usage})
        return _Resp({"choices": [{"message": {"content": "merged"}}], "usage": usage})

    run.requests.post = post
    meta: dict = {}
    assert run.merge_reviews(1, "t", ["a", "b"], meta=meta) == "merged"
    assert len(calls) == 2
    assert meta["cost"] == 0.04, meta          # both attempts, not just the winner
    assert meta["tokens"] == 2000, meta
    assert meta["reasoning_tokens"] == 1800, meta


def _capture_annotations():
    calls = []
    run._annotate = lambda level, msg: calls.append((level, msg))
    return calls


def test_run_pass_ok_returns_text_and_status():
    run.subprocess.run = lambda *a, **k: FakeProc(0, stdout="  real findings  ")
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "ok" and res.text == "real findings"
    assert res.status not in run.DEGRADED
    assert ann == []  # a good pass never annotates


def test_run_pass_timeout_is_degraded_and_warns():
    def boom(*a, **k):
        raise run.subprocess.TimeoutExpired(cmd="pi", timeout=run.PASS_TIMEOUT_S)

    run.subprocess.run = boom
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "timeout" and res.text == "" and res.status in run.DEGRADED
    assert ann[0][0] == "warning" and "timed out" in ann[0][1]


def test_run_pass_timeout_surfaces_partial_output():
    # A blocked pass must not be a black box: TimeoutExpired carries partial stdout/stderr
    # that the timeout branch now surfaces for diagnosis.
    def boom(*a, **k):
        raise run.subprocess.TimeoutExpired(
            cmd="pi",
            timeout=run.PASS_TIMEOUT_S,
            output="read tool call in flight ...",
            stderr="some stderr tail",
        )

    run.subprocess.run = boom
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "timeout"
    assert "partial output" in ann[0][1]
    assert "some stderr tail" in ann[0][1]


def test_run_pass_nonzero_exit_surfaces_stderr_verbatim():
    # The 402 out-of-credits message must reach the operator via the error annotation.
    msg = "402 This request requires more credits, or fewer max_tokens"
    run.subprocess.run = lambda *a, **k: FakeProc(1, stderr=msg + "\n")
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "error" and res.text == "" and res.status in run.DEGRADED
    assert ann[0][0] == "error" and "exited 1" in ann[0][1] and "402" in ann[0][1]


def test_run_pass_empty_clean_exit_is_degraded():
    run.subprocess.run = lambda *a, **k: FakeProc(0, stdout="   \n  ")
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "empty" and res.text == "" and res.status in run.DEGRADED
    assert ann[0][0] == "warning" and "no review output" in ann[0][1]


def test_run_pass_empty_surfaces_stderr_for_diagnosis():
    # A silent exit-0 pass can still carry a tale in stderr (a provider warning / empty
    # assistant message pi relayed) — it must reach the annotation, not vanish inline.
    run.subprocess.run = lambda *a, **k: FakeProc(
        0, stdout="", stderr="upstream returned empty completion\n"
    )
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "empty"
    assert "no review output" in ann[0][1]
    assert "empty completion" in ann[0][1]


def test_should_fail_only_on_degraded_without_post():
    # The exit-2 contract: a degraded pass that posted nothing is the only failure.
    assert (
        run._should_fail(posted=False, degraded=True) is True
    )  # silent failure → exit 2
    assert run._should_fail(posted=True, degraded=True) is False  # posted review wins
    assert run._should_fail(posted=False, degraded=False) is False  # clean empty sweep
    assert run._should_fail(posted=True, degraded=False) is False


def test_annotate_escapes_percent_and_newlines():
    # The runner percent-decodes %25/%0D/%0A in the message — a literal "%25" in pi's
    # stderr must render verbatim, not as "%".
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _REAL_ANNOTATE("error", "50%25 off\nnext")
    assert buf.getvalue().strip() == "::error title=second-opinion::50%2525 off%0Anext"


def test_review_pr_worktree_add_failure_is_degraded_and_annotates():
    # A failed head-checkout leaves the PR unreviewed — it must trip the tripwire
    # (degraded=True + an error annotation), not slip out green.
    run._gh = lambda args, timeout_s=60: (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    run._git = lambda args, check=True: FakeProc(
        returncode=1, stderr="fatal: bad object\n"
    )
    ann = _capture_annotations()
    out = run.review_pr(7, "t", "deadbeef00", "m", "m", dry_run=True)
    assert out == run.ReviewOutcome(posted=False, degraded=True)
    assert ann[0][0] == "error" and "worktree add failed" in ann[0][1]


def _stub_review_pr_deps():
    """Common stubs for driving review_pr past the diff fetch and worktree add. Returns the
    originals so the caller can restore them — leaving these swapped is latent cross-test
    pollution (it already broke the run_pass tests once during this file's development)."""
    originals = (run._gh, run._git, run.rv.shuffle_inputs)
    run._gh = lambda args, timeout_s=60: (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n")
    run._git = lambda args, check=True: FakeProc(0)
    run.rv.shuffle_inputs = lambda d, i: f"<<{i}>>"
    return originals


def _restore_review_pr_deps(originals):
    run._gh, run._git, run.rv.shuffle_inputs = originals


def _multifile_diff(n_files, chunk_chars):
    """A diff of n_files, each ~chunk_chars, in git's path order."""
    parts = []
    for i in range(n_files):
        path = f"src/f{i:02d}.rs"
        body = "\n".join(["+x" * 20] * (chunk_chars // 41 + 1))
        parts.append(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
                     f"@@ -1 +1 @@\n{body}\n")
    return "".join(parts)


def _drive_review_pr_body(pr, diff, max_chars, make_worktree=True):
    """Like _drive_review_pr but returns the posted comment body (via the dry-run print)."""
    import contextlib
    import io
    import shutil
    real = (run.K, run.run_pass, run.merge_reviews, run.MAX_DIFF_CHARS, run.PROVIDER)
    run.K, run.PROVIDER, run.MAX_DIFF_CHARS = 1, "openrouter", max_chars
    real_deps = _stub_review_pr_deps()
    run._gh = lambda args, timeout_s=60: diff
    wt = os.path.join(tempfile.gettempdir(), f"second-opinion-pr{pr}")
    shutil.rmtree(wt, ignore_errors=True)
    if make_worktree:
        os.makedirs(wt, exist_ok=True)
    run.run_pass = lambda w, m, s, u, session_dir=None: run.PassResult("finding", "ok")
    _capture_annotations()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            run.review_pr(pr, "t", "cafebabe00", "m", "m", dry_run=True)
        return buf.getvalue(), wt
    finally:
        run.K, run.run_pass, run.merge_reviews, run.MAX_DIFF_CHARS, run.PROVIDER = real
        _restore_review_pr_deps(real_deps)
        run._annotate = _REAL_ANNOTATE
        shutil.rmtree(wt, ignore_errors=True)


def test_footer_does_not_claim_an_on_disk_diff_that_was_never_written():
    # The failure this whole change exists to kill is "partial coverage reads as full".
    # A failed write must not leave the footer advertising coverage the agent never had.
    diff = _multifile_diff(n_files=6, chunk_chars=2000)
    body, _ = _drive_review_pr_body(4245, diff, max_chars=2500, make_worktree=False)
    assert "could not be supplied" in body, body[-600:]
    assert "supplied to the agent on disk" not in body, body[-600:]
    assert "covers that excerpt" in body

    # ...and when the write succeeds the footer says so, with the real numbers.
    body, _ = _drive_review_pr_body(4246, diff, max_chars=2500, make_worktree=True)
    assert "supplied to the agent on disk" in body, body[-600:]
    assert "could not be supplied" not in body
    assert "1 of 6 changed files" in body, body[-600:]


def test_truncation_emits_exactly_one_coverage_annotation():
    # Two warnings that contradict each other is worse than one that is accurate: the
    # optimistic one is the one a reader believes.
    diff = _multifile_diff(n_files=6, chunk_chars=2000)
    for make_wt in (True, False):
        _, ann, _ = _drive_review_pr(4247 if make_wt else 4248, diff, 2500,
                                     make_worktree=make_wt)
        cov = [m for lvl, m in ann if lvl == "warning" and "changed files" in m]
        assert len(cov) == 1, cov
        if make_wt:
            assert "supplied on disk" in cov[0]
        else:
            assert "could NOT be written" in cov[0] and "excerpt ONLY" in cov[0]


def _drive_review_pr(pr, diff, max_chars, make_worktree=True):
    """Run review_pr against a stubbed world; return (prompt, annotations, worktree)."""
    import shutil
    real = (run.K, run.run_pass, run.merge_reviews, run.MAX_DIFF_CHARS, run.PROVIDER)
    run.K, run.PROVIDER, run.MAX_DIFF_CHARS = 1, "openrouter", max_chars
    real_deps = _stub_review_pr_deps()
    run._gh = lambda args, timeout_s=60: diff
    wt = os.path.join(tempfile.gettempdir(), f"second-opinion-pr{pr}")
    shutil.rmtree(wt, ignore_errors=True)
    if make_worktree:
        os.makedirs(wt, exist_ok=True)
    seen = {}
    run.run_pass = lambda w, m, s, u, session_dir=None: (
        seen.__setitem__("prompt", u), run.PassResult("finding", "ok"))[1]
    ann = _capture_annotations()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            run.review_pr(pr, "t", "cafebabe00", "m", "m", dry_run=True)
        return seen.get("prompt", ""), list(ann), wt
    finally:
        run.K, run.run_pass, run.merge_reviews, run.MAX_DIFF_CHARS, run.PROVIDER = real
        _restore_review_pr_deps(real_deps)
        run._annotate = _REAL_ANNOTATE


def test_truncated_diff_writes_the_full_diff_and_points_the_agent_at_it():
    # filter_diff stops at the first chunk that overflows, so a big early file starves
    # every file behind it (spaghettio#575: 1 of 16 files, 1.7%, green check). The excerpt
    # may be capped, but the remainder must stay reachable.
    import shutil
    diff = _multifile_diff(n_files=6, chunk_chars=2000)
    prompt, ann, wt = _drive_review_pr(4242, diff, max_chars=2500)
    try:
        path = os.path.join(wt, run.FULL_DIFF_NAME)
        assert os.path.exists(path), "full diff was not written into the worktree"
        full = open(path, encoding="utf-8").read()
        # Everything the excerpt dropped is present in the on-disk copy.
        for i in range(6):
            assert f"src/f{i:02d}.rs" in full, i
        assert len(full) > len(prompt), "on-disk diff should exceed the truncated excerpt"
        # The agent is told it is truncated, where the rest is, and what is missing.
        assert "TRUNCATED" in prompt
        assert run.FULL_DIFF_NAME in prompt
        assert "src/f05.rs" in prompt, "dropped files must be named in the prompt"
        # ...and the operator sees it in the checks UI, with numbers, not just a vibe.
        warn = [m for lvl, m in ann if lvl == "warning" and "changed files" in m]
        assert warn, ann
        assert "of 6 changed files" in warn[0], warn[0]
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def test_untruncated_diff_adds_no_file_and_no_truncation_note():
    import shutil
    diff = _multifile_diff(n_files=2, chunk_chars=200)
    prompt, ann, wt = _drive_review_pr(4243, diff, max_chars=1_000_000)
    try:
        assert not os.path.exists(os.path.join(wt, run.FULL_DIFF_NAME))
        assert "TRUNCATED" not in prompt and run.FULL_DIFF_NAME not in prompt
        assert not [m for _l, m in ann if "changed files" in m], ann
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def test_failed_write_still_tells_the_agent_the_diff_is_truncated():
    # Disclosure and pointer are separate concerns. A failed write means the agent cannot
    # READ the rest — it does not mean the agent should be left believing the excerpt is
    # the whole change. Gating both on the pointer put the original bug back one branch
    # over, and the previous version of this test asserted that bug as correct.
    diff = _multifile_diff(n_files=6, chunk_chars=2000)
    prompt, ann, wt = _drive_review_pr(4244, diff, max_chars=2500, make_worktree=False)
    assert not os.path.exists(os.path.join(wt, run.FULL_DIFF_NAME))
    # The pointer is withdrawn — never send the agent to a file that isn't there...
    assert run.FULL_DIFF_NAME not in prompt, "prompt points at an unwritten file"
    # ...but the truncation itself is still disclosed, with the missing files named.
    assert "TRUNCATED" in prompt, "agent was handed a partial diff with no disclosure"
    assert "1 of 6 changed files" in prompt
    assert "src/f05.rs" in prompt
    # It is told to read the checkout, and explicitly NOT to diff (shallow, no base ref).
    assert "checked out at the PR's head commit" in prompt
    assert "cannot diff them against the base" in prompt
    assert [m for lvl, m in ann
            if lvl == "warning" and "could NOT be written" in m and "excerpt ONLY" in m], ann


def test_review_pr_parallel_passes_run_concurrently_and_keep_order():
    # All K passes must be in flight AT ONCE — the barrier times out loudly if any pass
    # waits on another (i.e. the loop silently went sequential, losing the wall-clock win
    # this path exists for). Results keep index order, empties drop, and a degraded sibling
    # of a posted union still reports degraded=True per the exit contract.
    import threading
    barrier = threading.Barrier(3, timeout=10)
    real_k, real_pass, real_merge = run.K, run.run_pass, run.merge_reviews
    # Pin the provider: the parallel branch gates on PROVIDER == "openrouter", so under a
    # PROVIDER=local environment (a supported config) these would silently test the
    # sequential path and fail on the barrier/session-dir assertions.
    real_provider = run.PROVIDER
    run.PROVIDER = "openrouter"
    run.K = 3
    real_deps = _stub_review_pr_deps()
    merged = {}

    def fake_merge(pr, title, passes, merge_model=None, meta=None):
        merged["passes"] = passes
        return "MERGED"

    run.merge_reviews = fake_merge

    def fake_pass(wt, model, system, user, session_dir=None):
        barrier.wait()  # BrokenBarrierError after 10s if the passes are serialized
        tag = user.split("<<")[1].split(">>")[0] if "<<" in user else "0"
        if tag == "1":
            return run.PassResult("", "timeout")  # one degraded sibling
        return run.PassResult(f"pass-{tag}", "ok")

    run.run_pass = fake_pass
    _capture_annotations()
    try:
        out = run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=True)
        assert out == run.ReviewOutcome(posted=True, degraded=True)
        assert merged["passes"] == ["pass-0", "pass-2"]  # index order kept, empty dropped
    finally:
        run.K, run.run_pass, run.merge_reviews = real_k, real_pass, real_merge
        run.PROVIDER = real_provider
        _restore_review_pr_deps(real_deps)
        run._annotate = _REAL_ANNOTATE


def test_review_pr_parallel_passes_get_distinct_session_dirs():
    # Concurrent passes sharing one session dir would interleave transcripts and let each
    # pass absorb its siblings' usage (the prior-files exclusion is a snapshot taken per
    # pass), double-counting cost. Each pass must get its own subdir under PI_SESSION_DIR.
    real_k, real_pass, real_merge = run.K, run.run_pass, run.merge_reviews
    # Pin the provider: the parallel branch gates on PROVIDER == "openrouter", so under a
    # PROVIDER=local environment (a supported config) these would silently test the
    # sequential path and fail on the barrier/session-dir assertions.
    real_provider = run.PROVIDER
    run.PROVIDER = "openrouter"
    run.K = 3
    real_deps = _stub_review_pr_deps()
    run.merge_reviews = lambda pr, title, passes, merge_model=None, meta=None: "MERGED"
    seen = []

    def fake_pass(wt, model, system, user, session_dir=None):
        seen.append(session_dir)
        return run.PassResult("text", "ok")

    run.run_pass = fake_pass
    _capture_annotations()
    with tempfile.TemporaryDirectory() as base:
        os.environ["PI_SESSION_DIR"] = base
        try:
            run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=True)
            assert len(seen) == 3 and all(d for d in seen), seen
            assert len(set(seen)) == 3, f"passes shared a session dir: {seen}"
            assert sorted(os.path.basename(d) for d in seen) == \
                ["pass-1", "pass-2", "pass-3"], seen
        finally:
            os.environ.pop("PI_SESSION_DIR", None)
            run.K, run.run_pass, run.merge_reviews = real_k, real_pass, real_merge
            run.PROVIDER = real_provider
            _restore_review_pr_deps(real_deps)
            run._annotate = _REAL_ANNOTATE


def test_already_reviewed_matches_marker_at_start_only():
    seen = {}

    def fake_gh(args, timeout_s=60):
        seen["jq"] = args[args.index("--jq") + 1]
        return ""  # no matching comment

    run._gh = fake_gh
    assert run.already_reviewed(5, "abc123") is False
    # the dedup must be a startswith on the body, carrying the sha — not a loose substring
    assert "startswith" in seen["jq"] and "abc123" in seen["jq"]


def test_run_pass_small_prompt_stays_inline_argv():
    # Below PROMPT_ARG_MAX the invocation must be byte-identical to the historical
    # one: the prompt itself as the argv element after -p, stdin untouched
    # (input=None → inherited, exactly as before the E2BIG fix).
    captured = {}

    def capture(cmd, **k):
        captured["cmd"] = cmd
        captured["input"] = k.get("input")
        return FakeProc(0, stdout="ok")

    run.subprocess.run = capture
    res = run.run_pass("/wt", "m", "sys", "small prompt")
    assert res.status == "ok"
    assert captured["cmd"][-2:] == ["-p", "small prompt"]
    assert captured["input"] is None


def test_run_pass_oversized_prompt_goes_via_stdin_verbatim():
    # Above PROMPT_ARG_MAX (Linux MAX_ARG_STRLEN guard) the prompt must be piped
    # via stdin VERBATIM — pi uses piped stdin as the raw initial message, so the
    # model sees the same bytes as the inline path (unlike @file, which wraps the
    # content in <file name="..."> markup and leaks the temp path into context).
    # argv must end with a bare -p (no positional message) and carry no oversized
    # or @-prefixed element.
    big = "x" * (run.PROMPT_ARG_MAX + 1)
    captured = {}

    def capture(cmd, **k):
        captured["cmd"] = cmd
        captured["input"] = k.get("input")
        return FakeProc(0, stdout="ok")

    run.subprocess.run = capture
    res = run.run_pass("/wt", "m", "sys", big)
    assert res.status == "ok"
    assert captured["cmd"][-1] == "-p", "oversized prompt must not ride argv"
    assert captured["input"] == big, "stdin must carry the prompt verbatim"
    assert all(not a.startswith("@") for a in captured["cmd"])
    assert all(len(a.encode()) <= run.PROMPT_ARG_MAX for a in captured["cmd"])


def test_run_pass_oversized_system_prompt_fails_legibly_not_e2big():
    # Operator GUIDANCE rides argv via --append-system-prompt and is unbounded; an
    # oversized one must become a legible degraded pass (error annotation), never
    # an opaque execve E2BIG crash — and never a silent clip of the guidance.
    called = {"run": False}

    def no_run(*a, **k):
        called["run"] = True
        return FakeProc(0, stdout="ok")

    run.subprocess.run = no_run
    ann = _capture_annotations()
    big_sys = "g" * (run.PROMPT_ARG_MAX + 1)
    res = run.run_pass("/wt", "m", big_sys, "usr")
    assert res.status == "error" and res.status in run.DEGRADED
    assert called["run"] is False, "pi must not be spawned with an oversized system arg"
    assert ann[0][0] == "error" and "GUIDANCE" in ann[0][1]




def test_run_pass_session_dir_targets_dir_and_drops_no_session():
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc(0, stdout="review ok")

    run.subprocess.run = fake_run
    prev = os.environ.get("PI_SESSION_DIR")
    os.environ["PI_SESSION_DIR"] = "/tmp/so-test-sess"
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
    finally:
        if prev is None:
            os.environ.pop("PI_SESSION_DIR", None)
        else:
            os.environ["PI_SESSION_DIR"] = prev
    assert res.status == "ok"
    assert "--session-dir" in seen["cmd"]
    assert "/tmp/so-test-sess" in seen["cmd"]
    assert "--no-session" not in seen["cmd"]


def test_run_pass_relative_session_dir_is_shared_with_worktree_process():
    # The parent reads sessions from its cwd, but pi runs with cwd=the PR worktree. A relative
    # path therefore has to become absolute before it is passed to pi, or usage is reported as
    # zero and the only transcript is deleted with the worktree.
    seen = {}
    previous_session_dir = os.environ.get("PI_SESSION_DIR")
    previous_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="so-relative-session-") as root:
        parent = os.path.join(root, "parent")
        worktree = os.path.join(root, "worktree")
        os.makedirs(parent)
        os.makedirs(worktree)

        def fake_run(cmd, **kw):
            session_arg = cmd[cmd.index("--session-dir") + 1]
            seen["session_arg"] = session_arg
            actual_dir = (session_arg if os.path.isabs(session_arg)
                          else os.path.join(kw["cwd"], session_arg))
            os.makedirs(actual_dir, exist_ok=True)
            with open(os.path.join(actual_dir, "pass.jsonl"), "w") as f:
                f.write('{"message":{"usage":{"input":10,"output":5,'
                        '"totalTokens":15,"cost":{"total":0.01}}}}\n')
            return FakeProc(0, stdout="review ok")

        run.subprocess.run = fake_run
        os.chdir(parent)
        os.environ["PI_SESSION_DIR"] = "relative-sessions"
        try:
            res = run.run_pass(worktree, "m", "sys", "usr")
        finally:
            os.chdir(previous_cwd)
            if previous_session_dir is None:
                os.environ.pop("PI_SESSION_DIR", None)
            else:
                os.environ["PI_SESSION_DIR"] = previous_session_dir

        expected = os.path.join(parent, "relative-sessions")
        assert seen["session_arg"] == expected
        assert res.tokens == 15 and abs(res.cost - 0.01) < 1e-9
        assert os.path.isfile(os.path.join(expected, "pass.jsonl"))
        assert not os.path.exists(os.path.join(worktree, "relative-sessions"))


def test_run_pass_default_captures_throwaway_session():
    # Default (no PI_SESSION_DIR) always writes a session to a per-pass temp dir so the
    # pass's real token usage/cost is readable, then scrubs it. No --no-session, and the
    # temp dir is gone afterward.
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc(0, stdout="review ok")

    run.subprocess.run = fake_run
    prev = os.environ.get("PI_SESSION_DIR")
    os.environ.pop("PI_SESSION_DIR", None)
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
    finally:
        if prev is not None:
            os.environ["PI_SESSION_DIR"] = prev
    assert "--no-session" not in seen["cmd"]
    assert "--session-dir" in seen["cmd"]
    sd = seen["cmd"][seen["cmd"].index("--session-dir") + 1]
    assert sd.startswith(tempfile.gettempdir())
    assert not os.path.isdir(sd)  # throwaway dir cleaned up
    assert res.cost == 0.0 and res.tokens == 0  # no real session -> no usage


def test_chat_captures_usage_cost_in_meta():
    run.requests.post = lambda *a, **k: _Resp({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"total_tokens": 100, "cost": 0.0123,
                  "completion_tokens_details": {"reasoning_tokens": 55}},
    })
    meta = {}
    out = run._chat(run.OPENROUTER_BASE, "k", "m", "p", meta)
    assert out == "ok"
    assert meta["cost"] == 0.0123
    assert meta["tokens"] == 100
    assert meta["reasoning_tokens"] == 55


def test_chat_keeps_valid_content_when_usage_metadata_is_malformed():
    run.requests.post = lambda *a, **k: _Resp({
        "choices": [{"message": {"content": "usable review"}}],
        "usage": {"total_tokens": {"bad": "shape"}, "cost": "not-a-number",
                  "completion_tokens_details": ["not", "a", "mapping"]},
    })
    meta = {}
    out = run._chat(run.OPENROUTER_BASE, "k", "m", "p", meta)
    assert out == "usable review"
    assert meta == {"cost": 0.0, "tokens": 0, "reasoning_tokens": 0}


def test_failure_notice_includes_run_url():
    prev = {k: os.environ.get(k) for k in
            ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID")}
    os.environ["GITHUB_SERVER_URL"] = "https://github.com"
    os.environ["GITHUB_REPOSITORY"] = "o/r"
    os.environ["GITHUB_RUN_ID"] = "12345"
    try:
        text = run._failure_notice_text("somesha")
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert "https://github.com/o/r/actions/runs/12345" in text


def test_failure_notice_omits_url_without_run():
    prev = os.environ.get("GITHUB_RUN_ID")
    os.environ.pop("GITHUB_RUN_ID", None)
    try:
        text = run._failure_notice_text("somesha")
    finally:
        if prev is not None:
            os.environ["GITHUB_RUN_ID"] = prev
    assert "actions/runs" not in text


def test_failure_notice_describes_configured_check_behavior():
    previous = run.FAIL_ON_DEGRADED
    try:
        run.FAIL_ON_DEGRADED = True
        failing = run._failure_notice_text("somesha")
        run.FAIL_ON_DEGRADED = False
        non_failing = run._failure_notice_text("somesha")
    finally:
        run.FAIL_ON_DEGRADED = previous
    assert "fails the check" in failing
    assert "does not fail the check" in non_failing
    assert "deliberately red" not in non_failing


def test_failure_notice_posts_when_dedup_lookup_fails():
    calls = []
    previous_lookup = run._already_noticed_failure
    previous_gh = run._gh

    def failed_lookup(*args, **kwargs):
        raise RuntimeError("temporary comments API failure")

    run._already_noticed_failure = failed_lookup
    run._gh = lambda args, timeout_s=60: calls.append(args) or ""
    try:
        run._post_failure_notice(7, "somesha", dry_run=False)
    finally:
        run._already_noticed_failure = previous_lookup
        run._gh = previous_gh
    assert len(calls) == 1
    assert calls[0][:3] == ["pr", "comment", "7"]


def test_cost_footer_formats_and_stays_empty_when_no_spend():
    assert run._cost_footer(0, 0, False) == ""
    assert "$0.0123" in run._cost_footer(0.01234, 1000, True)
    assert "1,000 tokens" in run._cost_footer(0.01234, 1000, True)
    assert run._cost_footer(0, 500, False) != ""


def test_read_session_usage_excludes_prior_files():
    # A shared persisted session-dir accumulates files across K sequential passes. Each pass
    # must only count ITS OWN transcript, not the cumulative usage of earlier passes.
    d = tempfile.mkdtemp(prefix="so-sess-test-")
    try:
        f1 = os.path.join(d, "pass1.jsonl")
        f2 = os.path.join(d, "pass2.jsonl")
        with open(f1, "w") as f:
            f.write('{"message":{"usage":{"input":10,"output":5,"cacheRead":1,"cacheWrite":0,"cost":{"total":0.02}}}}\n')
        with open(f2, "w") as f:
            f.write('{"message":{"usage":{"input":100,"output":50,"cacheRead":4,"cacheWrite":0,"cost":{"total":0.08}}}}\n')
        both = run._read_session_usage(d)
        assert both["input"] == 110 and both["output"] == 55
        assert both["cache_read"] == 5 and both["cache_write"] == 0
        assert both["total_tokens"] == 170
        assert abs(both["cost_total"] - 0.10) < 1e-9
        second = run._read_session_usage(d, exclude={f1})
        assert second["input"] == 100 and second["output"] == 50
        assert second["cache_read"] == 4 and second["total_tokens"] == 154
        assert abs(second["cost_total"] - 0.08) < 1e-9
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_read_session_usage_prefers_authoritative_total_tokens():
    d = tempfile.mkdtemp(prefix="so-sess-total-test-")
    try:
        with open(os.path.join(d, "pass.jsonl"), "w") as f:
            # Some providers include cached tokens in input already. Summing components would
            # report 160, but pi's authoritative total for the message is 110.
            f.write('{"message":{"usage":{"input":100,"output":10,"cacheRead":50,'
                    '"cacheWrite":0,"totalTokens":110,"cost":{"total":0.03}}}}\n')
        usage = run._read_session_usage(d)
        result = run._finish_pass("m", d, False, "review", "ok")
        assert usage["total_tokens"] == 110
        assert result.tokens == 110
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)



def test_run_pass_uses_provided_session_dir():
    # Parallel passes pass an explicit per-pass session dir: it must be used as-is and
    # persisted (not scrubbed), and --no-session must not be added.
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc(0, stdout="review ok")

    run.subprocess.run = fake_run
    d = tempfile.mkdtemp(prefix="so-provided-session-")
    res = run.run_pass("/wt", "m", "sys", "usr", session_dir=d)
    assert res.status == "ok"
    assert "--session-dir" in seen["cmd"]
    assert d in seen["cmd"]
    assert "--no-session" not in seen["cmd"]
    assert os.path.isdir(d)  # persisted, not scrubbed



def test_redact_text_replaces_env_key_and_openrouter_pattern():
    prev = os.environ.get("OPENROUTER_API_KEY")
    fake = "sk-or-v1-aaaabbbbccccddddeeeeffff0000111122223333"
    os.environ["OPENROUTER_API_KEY"] = fake
    try:
        out = run._redact_text(
            "KEY=" + fake + " OTHER=" + "sk-or-v1-999999999999998888888888777777")
    finally:
        if prev is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = prev
    assert fake not in out
    assert "999999999999998888888888777777" not in out
    assert out.count("[REDACTED]") >= 2


def test_finish_pass_redacts_persisted_transcript():
    # A persisted (non-internal) transcript must be scrubbed of the key before the consumer
    # uploads it as an artifact; usage/token parsing still works on the pre-redact data.
    prev = os.environ.get("OPENROUTER_API_KEY")
    fake = "sk-or-v1-abcdef0123456789abcdef0123456789abcdef0123"
    os.environ["OPENROUTER_API_KEY"] = fake
    d = tempfile.mkdtemp(prefix="so-redact-")
    fp = os.path.join(d, "s.jsonl")
    try:
        open(fp, "w").write(
            '{"message":{"usage":{"input":1,"output":1,"totalTokens":2,"cost":{"total":0.01}}}}\n'
            + "b64 KEY=" + fake + "\n")
        res = run._finish_pass("m", d, False, "review", "ok")
        assert res.tokens == 2 and res.cost == 0.01
        body = open(fp).read()
        assert fake not in body
        assert "[REDACTED]" in body
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        if prev is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = prev



def test_model_prices_retries_after_transient_failure():
    # Pin the provider: _model_prices short-circuits to a cached None under
    # PROVIDER=local (the offline invariant — never hit a cloud pricing endpoint), so
    # without this the whole test asserts the wrong code path and fails.
    real_provider = run.PROVIDER
    run.PROVIDER = "openrouter"
    run._PRICE_CACHE.clear()
    try:
        # transient network failure: not cached, so the daemon retries next sweep
        def boom(*a, **k):
            raise TimeoutError("provider blip")
        run.requests.get = boom
        assert run._model_prices("m") is None
        assert "m" not in run._PRICE_CACHE
        # a successful lookup (model absent from list) IS cached as None -- no price, no retry
        run.requests.get = lambda *a, **k: _Resp({"data": [{"id": "other-model"}]})
        assert run._model_prices("m") is None
        assert "m" in run._PRICE_CACHE
    finally:
        run.PROVIDER = real_provider


def test_model_prices_stays_offline_under_local_provider():
    # The repo invariant: PROVIDER=local never reaches a cloud pricing endpoint. Locking
    # it down, since the test above now pins the provider and would otherwise be the only
    # coverage of _model_prices.
    real_provider = run.PROVIDER
    run.PROVIDER = "local"
    run._PRICE_CACHE.clear()

    def fail(*a, **k):
        raise AssertionError("PROVIDER=local must not hit the pricing endpoint")

    run.requests.get = fail
    try:
        assert run._model_prices("m") is None
    finally:
        run.PROVIDER = real_provider
        run._PRICE_CACHE.clear()


def test_parse_max_tokens_rejects_non_numeric_and_defaults_blank():
    from second_opinion import providers as run_providers
    assert run_providers._parse_max_tokens("", 65536) == 65536
    assert run_providers._parse_max_tokens("48000", 65536) == 48000
    try:
        run_providers._parse_max_tokens("abc", 65536)
        raise AssertionError("expected SystemExit for non-numeric PI_MAX_TOKENS")
    except SystemExit as e:
        assert "must be an integer" in str(e)


# Keep this LAST: it iterates globals() at execution time, so any test defined
# below it would be silently skipped by the `python -m tests.test_run` runner
# (found by PR #18's own review bots — the appended tests were being skipped).
if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
