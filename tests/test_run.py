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


def test_merge_reviews_raises_clean_on_malformed_200():
    # empty choices / error envelope / moderation-shaped — must be a clean RuntimeError,
    # not a raw KeyError/IndexError leaking to the caller.
    for payload in (
        {"choices": []},
        {"error": {"message": "bad"}},
        {},
        {"choices": [{}]},
    ):
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


# Keep this LAST: it iterates globals() at execution time, so any test defined
# below it would be silently skipped by the `python -m tests.test_run` runner
# (found by PR #18's own review bots — the appended tests were being skipped).
if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
