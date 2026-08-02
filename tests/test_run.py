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


def test_merge_reviews_raises_clean_on_malformed_200():
    # empty choices / error envelope / moderation-shaped — must be a clean RuntimeError,
    # not a raw KeyError/IndexError leaking to the caller.
    for payload in ({"choices": []}, {"error": {"message": "bad"}}, {}, {"choices": [{}]}):
        run.requests.post = lambda *a, p=payload, **k: _Resp(p)
        try:
            run.merge_reviews(1, "t", ["a"])
        except RuntimeError as e:
            assert "no usable content" in str(e)
        else:
            raise AssertionError(f"expected RuntimeError for payload {payload}")


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

# Keep this LAST: it iterates globals() at execution time, so any test defined
# below it would be silently skipped by the `python -m tests.test_run` runner
# (found by PR #18's own review bots — the appended tests were being skipped).
if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
