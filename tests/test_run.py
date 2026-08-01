"""Smoke tests for the fragile orchestration in run.py: merge HTTP-response parsing
and the marker dedup query. Subprocess/requests are stubbed — no network. Run with
`pytest` (or directly: `python -m tests.test_run`).
"""
import os
import sys
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
    run.requests.post = lambda *a, **k: _Resp({"choices": [{"message": {"content": " merged "}}]})
    assert run.merge_reviews(1, "t", ["pass a", "pass b"]) == "merged"


def test_merge_reviews_retries_once_on_empty_then_succeeds():
    # A flaky-empty first attempt (reasoning models on long merge prompts —
    # spaghettio#561) must be retried, and a successful retry never annotates.
    calls = []

    def post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return _Resp({"choices": []})
        return _Resp({"choices": [{"message": {"content": " merged "}}]})

    run.requests.post = post
    ann = _capture_annotations()
    assert run.merge_reviews(1, "t", ["pass a", "pass b"]) == "merged"
    assert len(calls) == 2 and ann == []


def test_merge_reviews_falls_back_to_raw_passes_after_two_failures():
    # Malformed-200 shapes (empty choices / error envelope / moderation-shaped) and a
    # raising transport alike: nothing may leak to the caller — both attempts fail,
    # the review degrades to the unmerged raw passes, and a warning annotates so the
    # operator sees the malfunction without the delivery being lost.
    def raising(*a, **k):
        raise ValueError("boom")

    posts = [lambda *a, p=p, **k: _Resp(p)
             for p in ({"choices": []}, {"error": {"message": "bad"}}, {}, {"choices": [{}]})]
    for post in posts + [raising]:
        run.requests.post = post
        ann = _capture_annotations()
        out = run.merge_reviews(1, "t", ["pass a", "pass b"])
        assert "pass a" in out and "pass b" in out and "unmerged" in out
        assert ann[0][0] == "warning" and "merge failed twice" in ann[0][1]


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


def test_should_fail_only_on_degraded_without_post():
    # The exit-2 contract: a degraded pass that posted nothing is the only failure.
    assert run._should_fail(posted=False, degraded=True) is True   # silent failure → exit 2
    assert run._should_fail(posted=True, degraded=True) is False   # posted review wins
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
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n")
    run._git = lambda args, check=True: FakeProc(returncode=1, stderr="fatal: bad object\n")
    ann = _capture_annotations()
    out = run.review_pr(7, "t", "deadbeef00", "m", "m", dry_run=True)
    assert out == run.ReviewOutcome(posted=False, degraded=True)
    assert ann[0][0] == "error" and "worktree add failed" in ann[0][1]


def test_review_pr_parallel_passes_run_concurrently_and_keep_order():
    # With PARALLEL_PASSES, all K passes must be in flight AT ONCE — the barrier
    # times out loudly if any pass waits for another (i.e. the loop went
    # sequential). Results keep index order, empties are dropped, and a degraded
    # sibling of a posted union still reports degraded=True per the exit contract.
    import threading
    barrier = threading.Barrier(3, timeout=10)
    run.K, run.PARALLEL_PASSES = 3, True
    run._gh = lambda args, timeout_s=60: (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n")
    run._git = lambda args, check=True: FakeProc(0)
    run.rv.shuffle_inputs = lambda d, i: f"<<{i}>>"
    merged = {}

    def fake_merge(pr, title, passes, merge_model=None):
        merged["passes"] = passes
        return "MERGED"

    run.merge_reviews = fake_merge

    def fake_pass(wt, model, system, user):
        barrier.wait()
        tag = user.split("<<")[1].split(">>")[0] if "<<" in user else "0"
        if tag == "1":
            return run.PassResult("", "timeout")  # one degraded sibling
        return run.PassResult(f"pass-{tag}", "ok")

    run.run_pass = fake_pass
    _capture_annotations()
    out = run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=True)
    assert out == run.ReviewOutcome(posted=True, degraded=True)
    assert merged["passes"] == ["pass-0", "pass-2"]  # index order kept, empty dropped


def test_already_reviewed_matches_marker_at_start_only():
    seen = {}

    def fake_gh(args, timeout_s=60):
        seen["jq"] = args[args.index("--jq") + 1]
        return ""  # no matching comment

    run._gh = fake_gh
    assert run.already_reviewed(5, "abc123") is False
    # the dedup must be a startswith on the body, carrying the sha — not a loose substring
    assert "startswith" in seen["jq"] and "abc123" in seen["jq"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
