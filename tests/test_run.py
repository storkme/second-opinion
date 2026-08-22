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


def _stub_pi(fn):
    """Adapt an old-style `subprocess.run` stub to Popen.

    Production uses Popen so the spend watchdog can observe a pass while it runs; these
    tests were written against subprocess.run and assert the same things (argv, stdin,
    return codes, raised exceptions), so adapt rather than fork either side."""
    class _P:
        def __init__(self, cmd, **kw):
            self._cmd, self._kw = cmd, kw
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            res = fn(self._cmd, input=input, timeout=timeout, **self._kw)
            self.returncode = res.returncode
            return (res.stdout, res.stderr)

        def kill(self):
            pass

    run.subprocess.Popen = _P
    return _P


def _capture_annotations():
    calls = []
    run._annotate = lambda level, msg: calls.append((level, msg))
    return calls


def test_run_pass_ok_returns_text_and_status():
    _stub_pi(lambda *a, **k: FakeProc(0, stdout="  real findings  "))
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "ok" and res.text == "real findings"
    assert res.status not in run.DEGRADED
    assert ann == []  # a good pass never annotates


def test_run_pass_timeout_is_degraded_and_warns():
    def boom(*a, **k):
        raise run.subprocess.TimeoutExpired(cmd="pi", timeout=run.PASS_TIMEOUT_S)

    _stub_pi(boom)
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

    _stub_pi(boom)
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "timeout"
    assert "partial output" in ann[0][1]
    assert "some stderr tail" in ann[0][1]


def test_run_pass_nonzero_exit_surfaces_stderr_verbatim():
    # The 402 out-of-credits message must reach the operator via the error annotation.
    msg = "402 This request requires more credits, or fewer max_tokens"
    _stub_pi(lambda *a, **k: FakeProc(1, stderr=msg + "\n"))
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "error" and res.text == "" and res.status in run.DEGRADED
    assert ann[0][0] == "error" and "exited 1" in ann[0][1] and "402" in ann[0][1]


def test_run_pass_empty_clean_exit_is_degraded():
    _stub_pi(lambda *a, **k: FakeProc(0, stdout="   \n  "))
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "empty" and res.text == "" and res.status in run.DEGRADED
    assert ann[0][0] == "warning" and "no review output" in ann[0][1]


def test_run_pass_empty_surfaces_stderr_for_diagnosis():
    # A silent exit-0 pass can still carry a tale in stderr (a provider warning / empty
    # assistant message pi relayed) — it must reach the annotation, not vanish inline.
    _stub_pi(lambda *a, **k: FakeProc(
        0, stdout="", stderr="upstream returned empty completion\n"))
    ann = _capture_annotations()
    res = run.run_pass("/wt", "m", "sys", "usr")
    assert res.status == "empty"
    assert "no review output" in ann[0][1]
    assert "empty completion" in ann[0][1]


def test_transcript_is_redacted_even_when_the_pass_raises():
    # Redaction lives only in _finish_pass, which an exception escaping _run_pass_argv
    # never reaches — so a non-TimeoutExpired failure used to leave pi's partial JSONL
    # RAW in a persisted session dir. That surfaces as a FAILED job, not a cancelled one,
    # so no workflow-side guard covers it; the consumer publishes the raw file.
    import shutil
    key = "sk-or-v1-" + "a" * 40
    real_env = os.environ.get("OPENROUTER_API_KEY")
    real_run, real_popen = run.subprocess.run, run.subprocess.Popen
    session = tempfile.mkdtemp(prefix="so-raise-")
    os.environ["OPENROUTER_API_KEY"] = key
    os.environ["PI_SESSION_DIR"] = session
    try:
        def writes_then_explodes(cmd, **k):
            sd = cmd[cmd.index("--session-dir") + 1]
            os.makedirs(sd, exist_ok=True)
            with open(os.path.join(sd, "s.jsonl"), "w") as fh:
                fh.write('{"message":{"content":"the key is ' + key + '"}}\n')
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad stderr byte")

        _stub_pi(writes_then_explodes)
        try:
            run.run_pass("/wt", "m", "sys", "usr")
        except UnicodeDecodeError:
            pass  # the exception still propagates; only the scrub is guaranteed
        else:
            raise AssertionError("expected the exception to propagate")
        left = [os.path.join(r, f) for r, _d, fs in os.walk(session) for f in fs]
        assert left, "no transcript written — test would pass vacuously"
        blob = "".join(open(f, encoding="utf-8").read() for f in left)
        assert key not in blob, "raw API key survived in a persisted transcript"
        assert "REDACTED" in blob, blob[:200]
    finally:
        run.subprocess.run, run.subprocess.Popen = real_run, real_popen
        os.environ.pop("PI_SESSION_DIR", None)
        if real_env is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = real_env
        shutil.rmtree(session, ignore_errors=True)


def _spend_env(**kw):
    """Set spend-ceiling env vars and re-derive run.py's module-level constants."""
    saved = {k: getattr(run, k) for k in ("MAX_PASS_TOKENS", "MAX_PASS_COST_USD")}
    for k, v in kw.items():
        setattr(run, k, v)
    return saved


def test_runaway_pass_is_killed_at_the_token_ceiling():
    # A wall clock bounds TIME, not money. The observed runaway burned 12.6M tokens and
    # $1.96 producing nothing, and doubling the timeout only doubled the bill. The
    # ceiling has to be measured in spend, and it must report as its own cause — not as
    # a timeout, which is what made the three failure modes indistinguishable.
    import shutil
    saved = _spend_env(MAX_PASS_TOKENS=1000, MAX_PASS_COST_USD=0.0)
    real_run, real_popen = run.subprocess.run, run.subprocess.Popen
    real_poll = run.BUDGET_POLL_S
    run.BUDGET_POLL_S = 0.05
    session = tempfile.mkdtemp(prefix="so-runaway-")
    os.environ["PI_SESSION_DIR"] = session
    killed = {"n": 0}

    class FakePopen:
        returncode = -9

        def __init__(self, cmd, **k):
            sd = cmd[cmd.index("--session-dir") + 1]
            os.makedirs(sd, exist_ok=True)
            # Burn well past the ceiling, as a looping agent would.
            with open(os.path.join(sd, "runaway.jsonl"), "w") as fh:
                for _ in range(5):
                    fh.write('{"message":{"usage":{"totalTokens":900,"input":900}}}\n')

        def poll(self):
            # None while running: the watchdog must only trip on a live process, or a
            # pass that finished naturally over the ceiling would lose its review.
            return None if not killed["n"] else -9

        def communicate(self, input=None, timeout=None):
            import time
            for _ in range(200):           # outlive the watchdog, never the test
                if killed["n"]:
                    return ("", "")
                time.sleep(0.02)
            raise AssertionError("watchdog never killed the runaway")

        def kill(self):
            killed["n"] += 1

    run.subprocess.Popen = FakePopen
    ann = _capture_annotations()
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
        assert res.status == "runaway", res
        assert res.status in run.DEGRADED
        assert killed["n"] >= 1, "process was never killed"
        msg = " ".join(m for _l, m in ann)
        assert "ceiling" in msg and "4,500" in msg, msg   # names the spend that tripped it
        assert "timed out" not in msg, "a runaway must not report as a timeout"
        # PI_SESSION_DIR is set here, so the transcript survives and may be pointed at.
        assert "retained session transcript" in msg, msg


    finally:
        run.subprocess.run, run.subprocess.Popen = real_run, real_popen
        run.BUDGET_POLL_S = real_poll
        for k, v in saved.items():
            setattr(run, k, v)
        os.environ.pop("PI_SESSION_DIR", None)
        run._annotate = _REAL_ANNOTATE
        shutil.rmtree(session, ignore_errors=True)


def test_runaway_does_not_promise_a_transcript_it_is_about_to_delete():
    # With no session-dir configured (the default), _finish_pass rmtree's the throwaway
    # session a line after the annotation. Telling the operator to go read it would be
    # the #29 failure — "the numbers survived, the evidence did not" — rebuilt.
    saved = _spend_env(MAX_PASS_TOKENS=1000, MAX_PASS_COST_USD=0.0)
    real_popen, real_poll = run.subprocess.Popen, run.BUDGET_POLL_S
    run.BUDGET_POLL_S = 0.05
    os.environ.pop("PI_SESSION_DIR", None)          # the default: no persistence
    killed = {"n": 0}

    class FakePopen:
        returncode = -9

        def __init__(self, cmd, **k):
            sd = cmd[cmd.index("--session-dir") + 1]
            os.makedirs(sd, exist_ok=True)
            with open(os.path.join(sd, "r.jsonl"), "w") as fh:
                for _ in range(5):
                    fh.write('{"message":{"usage":{"totalTokens":900,"input":900}}}\n')

        def poll(self):
            return None if not killed["n"] else -9

        def communicate(self, input=None, timeout=None):
            import time
            for _ in range(200):
                if killed["n"]:
                    return ("", "")
                time.sleep(0.02)
            raise AssertionError("watchdog never killed the runaway")

        def kill(self):
            killed["n"] += 1

    run.subprocess.Popen = FakePopen
    ann = _capture_annotations()
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
        assert res.status == "runaway", res
        msg = " ".join(m for _l, m in ann)
        assert "no transcript was retained" in msg, msg
        assert "set `session-dir`" in msg, msg
    finally:
        run.subprocess.Popen, run.BUDGET_POLL_S = real_popen, real_poll
        for k, v in saved.items():
            setattr(run, k, v)
        run._annotate = _REAL_ANNOTATE


def test_a_pass_that_finishes_over_the_ceiling_keeps_its_review():
    # The race: a pass can finish naturally with final usage above the ceiling. kill() is
    # a no-op on a reaped process, so a watchdog that set its flag anyway would make the
    # main thread discard a perfectly good review as a "runaway".
    import shutil
    saved = _spend_env(MAX_PASS_TOKENS=1000, MAX_PASS_COST_USD=0.0)
    real_popen, real_poll = run.subprocess.Popen, run.BUDGET_POLL_S
    run.BUDGET_POLL_S = 0.02
    session = tempfile.mkdtemp(prefix="so-finished-")
    os.environ["PI_SESSION_DIR"] = session

    class FinishedPopen:
        returncode = 0                     # exited on its own, not killed

        def __init__(self, cmd, **k):
            sd = cmd[cmd.index("--session-dir") + 1]
            os.makedirs(sd, exist_ok=True)
            with open(os.path.join(sd, "s.jsonl"), "w") as fh:
                fh.write('{"message":{"usage":{"totalTokens":9999,"input":9999}}}\n')

        polls = {"n": 0}

        def poll(self):
            # ALIVE on the first poll, so the watchdog genuinely sets breach["why"],
            # then reaped. That is the real race: the flag is set, and the process
            # finishes on its own before the kill lands. If poll() only ever returned 0
            # the watchdog would never trip and the main thread's returncode guard —
            # the thing that actually saves the review — would be dead-untested.
            self.polls["n"] += 1
            return None if self.polls["n"] == 1 else 0

        def communicate(self, input=None, timeout=None):
            import time
            time.sleep(0.12)               # let the watchdog poll while "running"
            return ("a real review", "")

        def kill(self):
            pass                           # no-op, exactly as on a reaped process

    run.subprocess.Popen = FinishedPopen
    _capture_annotations()
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
        assert res.status == "ok", res
        assert res.text == "a real review", res
    finally:
        run.subprocess.Popen, run.BUDGET_POLL_S = real_popen, real_poll
        for k, v in saved.items():
            setattr(run, k, v)
        os.environ.pop("PI_SESSION_DIR", None)
        run._annotate = _REAL_ANNOTATE
        shutil.rmtree(session, ignore_errors=True)


def test_cost_ceiling_kills_and_names_the_dollars():
    # Both reviewers flagged this: every other spend test drives MAX_PASS_TOKENS, so the
    # cost branch — the subtler one, and the one the docs caution most about — shipped
    # entirely unexercised.
    import shutil
    saved = _spend_env(MAX_PASS_TOKENS=0, MAX_PASS_COST_USD=0.50)
    real_popen, real_poll = run.subprocess.Popen, run.BUDGET_POLL_S
    run.BUDGET_POLL_S = 0.05
    session = tempfile.mkdtemp(prefix="so-cost-")
    os.environ["PI_SESSION_DIR"] = session
    killed = {"n": 0}

    class FakePopen:
        returncode = -9

        def __init__(self, cmd, **k):
            sd = cmd[cmd.index("--session-dir") + 1]
            os.makedirs(sd, exist_ok=True)
            # pi's own cost.total, so no pricing lookup is needed for the primary path.
            with open(os.path.join(sd, "c.jsonl"), "w") as fh:
                for _ in range(4):
                    fh.write('{"message":{"usage":{"totalTokens":10,'
                             '"cost":{"total":0.25}}}}\n')

        def poll(self):
            return None if not killed["n"] else -9

        def communicate(self, input=None, timeout=None):
            import time
            for _ in range(200):
                if killed["n"]:
                    return ("", "")
                time.sleep(0.02)
            raise AssertionError("cost ceiling never tripped")

        def kill(self):
            killed["n"] += 1

    run.subprocess.Popen = FakePopen
    ann = _capture_annotations()
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
        assert res.status == "runaway", res
        msg = " ".join(m for _l, m in ann)
        assert "$1.0000 over the $0.50 ceiling" in msg, msg
    finally:
        run.subprocess.Popen, run.BUDGET_POLL_S = real_popen, real_poll
        for k, v in saved.items():
            setattr(run, k, v)
        os.environ.pop("PI_SESSION_DIR", None)
        run._annotate = _REAL_ANNOTATE
        shutil.rmtree(session, ignore_errors=True)


def test_cost_only_ceiling_warns_when_pricing_is_unavailable():
    # PROVIDER=local never prices (offline invariant) and a failed OpenRouter lookup is
    # left uncached — either way cost stays 0.0 and the ceiling can never trip. Silently
    # not protecting is the false assurance to avoid.
    saved = _spend_env(MAX_PASS_TOKENS=0, MAX_PASS_COST_USD=1.0)
    real_popen, real_prices = run.subprocess.Popen, run._model_prices
    run._model_prices = lambda model: None          # no pricing available

    class Quick:
        returncode = 0

        def __init__(self, cmd, **k):
            pass

        def poll(self):
            return 0

        def communicate(self, input=None, timeout=None):
            return ("a finding", "")

        def kill(self):
            pass

    run.subprocess.Popen = Quick
    ann = _capture_annotations()
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
        assert res.status == "ok", res
        msg = " ".join(m for _l, m in ann)
        assert "cost ceiling AT RISK" in msg, msg
        # Must not claim the ceiling is dead: it reads pi's own cost first.
        assert "INACTIVE" not in msg, msg
        assert "MAX_PASS_TOKENS" in msg, msg
    finally:
        run.subprocess.Popen, run._model_prices = real_popen, real_prices
        for k, v in saved.items():
            setattr(run, k, v)
        run._annotate = _REAL_ANNOTATE


def test_a_currency_formatted_ceiling_disables_loudly_instead_of_crashing():
    # `max-pass-cost-usd: "$1.96"` is a natural operator mistake; float() would raise at
    # import and red the job as a raw traceback rather than a legible failure.
    errs = list(run._CONFIG_ERRORS)
    try:
        os.environ["MAX_PASS_COST_USD"] = "$1.96"
        run._CONFIG_ERRORS.clear()
        assert run._num_env("MAX_PASS_COST_USD", float, 0.0) == 0.0
        assert run._CONFIG_ERRORS and "not a number" in run._CONFIG_ERRORS[0]
        assert "INACTIVE" in run._CONFIG_ERRORS[0]
        os.environ["MAX_PASS_TOKENS"] = "5000000"
        assert run._num_env("MAX_PASS_TOKENS", int, 0) == 5000000
    finally:
        os.environ.pop("MAX_PASS_COST_USD", None)
        os.environ.pop("MAX_PASS_TOKENS", None)
        run._CONFIG_ERRORS[:] = errs


def test_no_ceiling_configured_means_no_watchdog():
    # Opt-in: with no ceiling set, behaviour must be byte-identical to before — killing a
    # legitimately long pass is its own failure, and one incident is thin evidence.
    saved = _spend_env(MAX_PASS_TOKENS=0, MAX_PASS_COST_USD=0.0)
    real_popen = run.subprocess.Popen
    seen = {"killed": False}

    class FakePopen:
        returncode = 0

        def __init__(self, cmd, **k):
            pass

        def communicate(self, input=None, timeout=None):
            return ("a finding", "")

        def kill(self):
            seen["killed"] = True

    run.subprocess.Popen = FakePopen
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
        assert res.status == "ok" and res.text == "a finding", res
        assert not seen["killed"], "no ceiling configured — nothing should be killed"
    finally:
        run.subprocess.Popen = real_popen
        for k, v in saved.items():
            setattr(run, k, v)


def test_degraded_annotations_report_what_the_pass_spent():
    # "timed out after 1800s" is true and useless: it reads the same for a pass that was
    # working flat out, one that was hung at 30 tok/s, and one looping at 7000 tok/s.
    import shutil
    session = tempfile.mkdtemp(prefix="so-spend-")
    os.environ["PI_SESSION_DIR"] = session
    real_popen = run.subprocess.Popen

    class FakePopen:
        returncode = 0

        def __init__(self, cmd, **k):
            sd = cmd[cmd.index("--session-dir") + 1]
            os.makedirs(sd, exist_ok=True)
            with open(os.path.join(sd, "s.jsonl"), "w") as fh:
                fh.write('{"message":{"usage":{"totalTokens":123456,"input":123456}}}\n')

        def communicate(self, input=None, timeout=None):
            return ("   ", "")           # exit 0, no output -> degraded "empty"

        def kill(self):
            pass

    run.subprocess.Popen = FakePopen
    ann = _capture_annotations()
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
        assert res.status == "empty"
        msg = " ".join(m for _l, m in ann)
        assert "123,456 tok" in msg, msg
    finally:
        run.subprocess.Popen = real_popen
        os.environ.pop("PI_SESSION_DIR", None)
        run._annotate = _REAL_ANNOTATE
        shutil.rmtree(session, ignore_errors=True)


def test_timeout_fallback_survives_bytes_from_timeoutexpired():
    # On POSIX, TimeoutExpired carries BYTES in .output/.stderr even under text=True.
    # The fallback only runs when the post-kill communicate() itself raises, and _peek
    # does " ".join(x.split()) — bytes there is a TypeError that would crash the pass
    # instead of returning a clean degraded "timeout".
    real_popen = run.subprocess.Popen

    class BytesTimeout:
        returncode = -9

        def __init__(self, cmd, **k):
            self._n = 0

        def poll(self):
            return None

        def communicate(self, input=None, timeout=None):
            self._n += 1
            if self._n == 1:
                raise run.subprocess.TimeoutExpired(
                    cmd="pi", timeout=1, output=b"partial stdout\n",
                    stderr=b"\xff\xfe non-utf8 tail\n")
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")  # forces the fallback

        def kill(self):
            pass

    run.subprocess.Popen = BytesTimeout
    ann = _capture_annotations()
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
        assert res.status == "timeout", res          # degraded, not a crash
        msg = " ".join(m for _l, m in ann)
        assert "timed out" in msg and "partial output" in msg, msg
    finally:
        run.subprocess.Popen = real_popen
        run._annotate = _REAL_ANNOTATE


def test_an_exception_still_kills_and_reaps_the_child():
    # subprocess.run wrapped its Popen in a `with` and killed the child on ANY exception.
    # A raw Popen does not — and by the time an exception reaches the outer finally the
    # watchdog has already been retired by `stop.set()`, so an orphaned pi would keep
    # burning tokens with no ceiling: the failure this whole feature exists to bound.
    real_popen = run.subprocess.Popen
    state = {"killed": 0, "waited": 0, "alive": True}

    class Orphan:
        returncode = None

        def __init__(self, cmd, **k):
            pass

        def poll(self):
            return None if state["alive"] else -9

        def communicate(self, input=None, timeout=None):
            raise OSError("read failed mid-stream")   # not TimeoutExpired

        def kill(self):
            state["killed"] += 1
            state["alive"] = False

        def wait(self, timeout=None):
            state["waited"] += 1
            return -9

    run.subprocess.Popen = Orphan
    try:
        try:
            run.run_pass("/wt", "m", "sys", "usr")
        except OSError:
            pass                                       # must still propagate
        else:
            raise AssertionError("expected the OSError to propagate")
        assert state["killed"] == 1, "child was left running with no watchdog"
        assert state["waited"] == 1, "child was never reaped"
    finally:
        run.subprocess.Popen = real_popen


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
    assert "1 of 6 changed file(s)" in body, body[-600:]


def test_truncation_emits_exactly_one_coverage_annotation():
    # Two warnings that contradict each other is worse than one that is accurate: the
    # optimistic one is the one a reader believes.
    import shutil
    diff = _multifile_diff(n_files=6, chunk_chars=2000)
    for make_wt in (True, False):
        _, ann, wt = _drive_review_pr(4247 if make_wt else 4248, diff, 2500,
                                      make_worktree=make_wt)
        try:
            cov = [m for lvl, m in ann if lvl == "warning" and "changed file(s)" in m]
            assert len(cov) == 1, cov
            if make_wt:
                assert "supplied on disk" in cov[0]
            else:
                assert "could NOT be written" in cov[0] and "excerpt ONLY" in cov[0]
        finally:
            shutil.rmtree(wt, ignore_errors=True)


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
    # filter_diff keeps a big early file and drops what follows, so it starves
    # every file behind it (spaghettio#575: 1 of 16 files, 1.7%, green check). The excerpt
    # may be capped, but the remainder must stay reachable.
    import shutil
    diff = _multifile_diff(n_files=6, chunk_chars=12000)   # >50KB total: paging applies
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
        warn = [m for lvl, m in ann if lvl == "warning" and "changed file(s)" in m]
        assert warn, ann
        assert "of 6 changed file(s)" in warn[0], warn[0]
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def test_full_diff_puts_the_unseen_files_first_within_one_read():
    # pi's read returns at most 2000 lines / 50KB, and this file is bigger than that by
    # construction. In git path order the first read would return the SAME files the
    # excerpt already carried, so the agent could "read the complete diff" and see
    # nothing new. The files it is missing must lead.
    import shutil
    diff = _multifile_diff(n_files=6, chunk_chars=12000)   # >50KB total: paging applies
    prompt, _ann, wt = _drive_review_pr(4249, diff, max_chars=2500)
    try:
        full = open(os.path.join(wt, run.FULL_DIFF_NAME), encoding="utf-8").read()
        order = [ln.split(" b/")[-1] for ln in full.splitlines()
                 if ln.startswith("diff --git ")]
        # f00 is the only file the excerpt carried, so it must NOT lead the file.
        assert order[0] != "src/f00.rs", order
        assert order[-1] == "src/f00.rs", order
        assert set(order) == {f"src/f{i:02d}.rs" for i in range(6)}, order
        # The header says so too, and warns one read is not the whole file.
        assert "ordered FIRST" in full, full[:400]
        assert "larger than a single" in full, full[:400]
        # ...and the prompt does not imply a single read suffices.
        assert "LARGER than a single read returns" in prompt
        assert "One read of that file is NOT the whole diff" in prompt
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def test_single_file_clipped_mid_hunk_is_not_described_as_full_coverage():
    # filter_diff's single-chunk branch: one file overflows the cap on its own, so it
    # lands in `files` and nothing is "dropped". Saying "1 of 1 changed files" there reads
    # as FULL coverage while the file is cut mid-hunk — this PR's own headline bug in the
    # one shape it hadn't covered.
    import shutil
    diff = _multifile_diff(n_files=1, chunk_chars=6000)
    prompt, ann, wt = _drive_review_pr(4250, diff, max_chars=2500)
    try:
        assert "1 of 1 changed file(s)" not in prompt, prompt[-400:]
        assert "is cut off mid-file" in prompt, prompt[-400:]
        # No dropped files, so no empty "absent from the excerpt" list.
        assert "absent from the excerpt" not in prompt, prompt[-400:]
        cov = [m for lvl, m in ann if lvl == "warning" and "excerpt" in m]
        assert cov and "is cut off mid-file" in cov[0], cov
        assert "Not in the excerpt:" not in cov[0], cov[0]
        # The file's own header must agree with all of the above. With nothing dropped
        # the reorder is a no-op, so the TOP repeats the excerpt and the missing material
        # is the TAIL — pointing the agent at the top would be actively wrong.
        head = open(os.path.join(wt, run.FULL_DIFF_NAME), encoding="utf-8").read()[:700]
        assert "is at the END of this file" in head, head
        # This fixture fits in one read, so the header must NOT tell the agent to page —
        # the prompt says it fits, and a "page down" here would contradict it outright.
        assert "Page DOWN" not in head, head
        assert "fits in a single read" in prompt, prompt[-300:]
        assert "ordered FIRST" not in head, head
        assert "1 of 1 changed file(s)" not in head, head
    finally:
        shutil.rmtree(wt, ignore_errors=True)

    body, wt2 = _drive_review_pr_body(4251, diff, max_chars=2500)
    try:
        assert "covers 1 of 1 changed file(s)" not in body, body[-500:]
        assert "is cut off mid-file" in body, body[-500:]
    finally:
        shutil.rmtree(wt2, ignore_errors=True)


def test_eval_review_diff_discloses_truncation_like_production():
    # #26: eval had its own hand-copied user_turn with no truncation handling, so on any
    # diff over the cap it measured the PRE-remediation reviewer while its docstring
    # promised "the reviewer AS CONFIGURED". Both callers must now use the same helpers.
    import shutil
    from second_opinion import eval as ev
    real = (run.K, run.run_pass, run.merge_reviews, run.MAX_DIFF_CHARS, run._git, run._guidance)
    run.K, run.MAX_DIFF_CHARS = 1, 2500
    run._git = lambda args, check=True: FakeProc(0)
    run._guidance = lambda: ""
    wt = os.path.join(tempfile.gettempdir(), "second-opinion-eval-pr77")
    shutil.rmtree(wt, ignore_errors=True)
    os.makedirs(wt, exist_ok=True)
    seen = {}
    run.run_pass = lambda w, m, s, u, session_dir=None: (
        seen.__setitem__("prompt", u), run.PassResult("finding", "ok"))[1]
    try:
        rec = {"pr": 77, "title": "t", "target": "cafebabe",
               "diff": _multifile_diff(n_files=6, chunk_chars=12000)}
        ev.review_diff(rec, "m")
        prompt = seen["prompt"]
        assert "TRUNCATED" in prompt, prompt[-400:]
        assert "1 of 6 changed file(s)" in prompt, prompt[-400:]
        assert "src/f05.rs" in prompt, "missing files must be named"
        # ...and the full diff is on disk for it, exactly as in production.
        assert os.path.exists(os.path.join(wt, run.FULL_DIFF_NAME))
        assert run.FULL_DIFF_NAME in prompt
    finally:
        run.K, run.run_pass, run.merge_reviews, run.MAX_DIFF_CHARS, run._git, run._guidance = real
        shutil.rmtree(wt, ignore_errors=True)


def test_notice_flags_a_later_hunk_of_an_already_seen_file():
    # #27: a path with two diff chunks where the first fits and the second doesn't is
    # neither "wholly missing" nor "clipped" — it's present but incomplete. A filename-set
    # difference called it neither, so nothing was disclosed at all.
    one = ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n+x\n")
    two = one + ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -9 +9 @@\n"
                 + "".join(f"+pad {i}\n" for i in range(300)))
    fd = run.rv.filter_diff(two, [], len(one) + 10)
    assert fd.partial_files == ["src/a.py"], fd
    notice = run.truncation_notice(fd, run.FULL_DIFF_NAME)
    assert "a later hunk of src/a.py is missing" in notice, notice
    # It must NOT be described as wholly absent, nor as a mid-file clip.
    assert "absent from the excerpt" not in notice, notice
    assert "is cut off mid-file" not in notice, notice


def test_notice_does_not_claim_paging_when_the_full_diff_fits_one_read():
    # #27: "LARGER than a single read returns" was asserted unconditionally. True at the
    # default cap, false if an operator lowers MAX_DIFF_CHARS below pi's read limit.
    small = _multifile_diff(n_files=3, chunk_chars=300)
    fd = run.rv.filter_diff(small, [], 400)
    assert fd.truncated
    assert len(fd.full_text.encode()) < run.AGENT_READ_BYTES
    assert fd.full_text.count("\n") < run.AGENT_READ_LINES
    notice = run.truncation_notice(fd, run.FULL_DIFF_NAME)
    assert "fits in a single read" in notice, notice
    assert "LARGER than a single read" not in notice, notice


def test_a_line_dense_diff_under_the_byte_cap_still_needs_paging():
    # pi truncates a read at 2000 LINES *or* 50KB, whichever hits first. Gating the
    # "fits in a single read" claim on bytes alone lets a line-dense diff — under the byte
    # cap but over the line cap — be advertised as readable in one go, so the agent stops
    # after seeing two-thirds of it. That is this PR's own bug class, in the path this PR
    # added to fix it.
    # 450 tiny chunks x 5 lines = 2250 lines in ~37KB: under the byte cap, over the lines cap.
    dense = "".join(
        f"diff --git a/s/f{i:03d}.rs b/s/f{i:03d}.rs\n"
        f"--- a/s/f{i:03d}.rs\n+++ b/s/f{i:03d}.rs\n@@ -1 +1 @@\n+x\n"
        for i in range(450))
    fd = run.rv.filter_diff(dense, [], 20000)
    assert fd.truncated
    assert len(fd.full_text.encode()) < run.AGENT_READ_BYTES      # under the byte cap...
    assert fd.full_text.count("\n") > run.AGENT_READ_LINES        # ...but over the line cap
    notice = run.truncation_notice(fd, run.FULL_DIFF_NAME)
    assert "fits in a single read" not in notice, notice
    assert "LARGER than a single read" in notice, notice


def test_prompt_and_on_disk_header_agree_on_paging_at_the_boundary():
    # The written file is header + diff, so a diff just under either cap flips over it
    # once the header lands. If the prompt and the header disagreed there, one of them
    # would claim the file fits in a read that truncates it. Both budget for the header.
    import shutil
    # ~1995 lines: under the 2000 cap on its own, over it once a ~7-line header prepends.
    boundary = "".join(
        f"diff --git a/s/f{i:03d}.rs b/s/f{i:03d}.rs\n"
        f"--- a/s/f{i:03d}.rs\n+++ b/s/f{i:03d}.rs\n@@ -1 +1 @@\n+x\n"
        for i in range(399))
    fd = run.rv.filter_diff(boundary, [], 20000)
    lines = fd.full_text.count("\n")
    assert run.AGENT_READ_LINES - run.FULL_DIFF_HEADER_PAD_LINES < lines <= run.AGENT_READ_LINES, lines
    notice = run.truncation_notice(fd, run.FULL_DIFF_NAME)
    prompt_says_paging = "LARGER than a single read" in notice
    assert prompt_says_paging, "header pushes it over the line cap — must not claim it fits"

    wt = os.path.join(tempfile.gettempdir(), "second-opinion-boundary")
    shutil.rmtree(wt, ignore_errors=True)
    os.makedirs(wt)
    try:
        run.write_full_diff(fd, wt, 1)
        head = open(os.path.join(wt, run.FULL_DIFF_NAME), encoding="utf-8").read()[:600]
        file_says_paging = "larger than a single read" in head
        assert file_says_paging == prompt_says_paging, (prompt_says_paging, head)
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def test_untruncated_diff_adds_no_file_and_no_truncation_note():
    import shutil
    diff = _multifile_diff(n_files=2, chunk_chars=200)
    prompt, ann, wt = _drive_review_pr(4243, diff, max_chars=1_000_000)
    try:
        assert not os.path.exists(os.path.join(wt, run.FULL_DIFF_NAME))
        assert "TRUNCATED" not in prompt and run.FULL_DIFF_NAME not in prompt
        assert not [m for _l, m in ann if "changed file(s)" in m], ann
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
    assert "1 of 6 changed file(s)" in prompt
    assert "src/f05.rs" in prompt
    # It is told to read the checkout, and explicitly NOT to diff (shallow, no base ref).
    assert "checked out at the reviewed commit" in prompt
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

    _stub_pi(capture)
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

    _stub_pi(capture)
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

    _stub_pi(no_run)
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

    _stub_pi(fake_run)
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

        _stub_pi(fake_run)
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

    _stub_pi(fake_run)
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


def test_chat_takes_the_cached_part_out_of_the_prompt_count():
    # The chat API counts cached tokens INSIDE prompt_tokens; pi reports them alongside
    # input. Without the subtraction the same 800 tokens are billed to two classes and
    # the cache-hit panel — the whole point of splitting — reads high by its own numerator.
    run.requests.post = lambda *a, **k: _Resp({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020,
                  "prompt_tokens_details": {"cached_tokens": 800}},
    })
    meta = {}
    run._chat(run.OPENROUTER_BASE, "k", "m", "p", meta)
    assert meta["tokens_cache_read"] == 800
    assert meta["tokens_input"] == 200, "fresh input is prompt minus the cached part"
    assert meta["tokens_output"] == 20
    # A merge is one stateless call: it writes no cache entry it then reads back.
    assert meta["tokens_cache_write"] == 0


def test_chat_never_reports_negative_input_when_cached_exceeds_prompt():
    # Two independently reported fields, so the subtraction can invert on a malformed
    # response. A negative token count would poison any sum it landed in.
    run.requests.post = lambda *a, **k: _Resp({
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1,
                  "prompt_tokens_details": {"cached_tokens": 99}},
    })
    meta = {}
    run._chat(run.OPENROUTER_BASE, "k", "m", "p", meta)
    assert meta["tokens_input"] == 0


def test_chat_keeps_valid_content_when_usage_metadata_is_malformed():
    run.requests.post = lambda *a, **k: _Resp({
        "choices": [{"message": {"content": "usable review"}}],
        "usage": {"total_tokens": {"bad": "shape"}, "cost": "not-a-number",
                  "completion_tokens_details": ["not", "a", "mapping"]},
    })
    meta = {}
    out = run._chat(run.OPENROUTER_BASE, "k", "m", "p", meta)
    assert out == "usable review"
    assert meta == {"cost": 0.0, "tokens": 0, "reasoning_tokens": 0,
                    "tokens_input": 0, "tokens_output": 0,
                    "tokens_cache_read": 0, "tokens_cache_write": 0}


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
            # A SYNTHETIC folding provider — one that includes cached tokens in its input
            # count already. Summing components would report 160, but the authoritative
            # total for the message is 110, and this pins that the total wins.
            #
            # NOT a sample of what pi sends. Real pi usage records are disjoint
            # (totalTokens == input + output + cacheRead + cacheWrite; verified across
            # every local session record carrying a cacheRead), and the sibling test
            # below pins that shape. Reading this fixture as canonical is what led two
            # separate reviews of the token-split PR to opposite wrong conclusions —
            # that cache reads sit outside `tokens`, and that they sit inside `input`.
            f.write('{"message":{"usage":{"input":100,"output":10,"cacheRead":50,'
                    '"cacheWrite":0,"totalTokens":110,"cost":{"total":0.03}}}}\n')
        usage = run._read_session_usage(d)
        result = run._finish_pass("m", d, False, "review", "ok")
        assert usage["total_tokens"] == 110
        assert result.tokens == 110
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)



def test_finish_pass_carries_the_token_breakdown():
    # The four classes bill at rates an order of magnitude apart, so a pooled total
    # cannot distinguish a price change from a cache regression. This is the read that
    # used to be thrown away one line before the PassResult was built.
    d = tempfile.mkdtemp(prefix="so-sess-split-test-")
    try:
        with open(os.path.join(d, "pass.jsonl"), "w") as f:
            f.write('{"message":{"usage":{"input":100,"output":10,"cacheRead":50,'
                    '"cacheWrite":5,"totalTokens":110,"cost":{"total":0.03}}}}\n')
        result = run._finish_pass("m", d, False, "review", "ok")
        assert result.tokens_input == 100
        assert result.tokens_output == 10
        assert result.tokens_cache_read == 50
        assert result.tokens_cache_write == 5
        # Deliberately NOT 165. pi's authoritative totalTokens wins (the sibling test
        # pins why), so the components are the provider's component counts and nothing
        # more — a panel that derives one by subtracting the others is inventing data.
        assert result.tokens == 110
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_finish_pass_reads_the_disjoint_shape_pi_actually_sends():
    # The shape every real pi usage record has: totalTokens counts all four classes, so
    # `input` is fresh input alone and cache reads are inside the total without being
    # inside `input`. Both halves are load-bearing for the mix panels — the first makes
    # tokens_cache_read/tokens a genuine share, the second keeps the stacked panel free
    # of overlap — and until this test there was no fixture asserting either.
    d = tempfile.mkdtemp(prefix="so-sess-disjoint-test-")
    try:
        with open(os.path.join(d, "pass.jsonl"), "w") as f:
            f.write('{"message":{"usage":{"input":100,"output":10,"cacheRead":50,'
                    '"cacheWrite":5,"totalTokens":165,"cost":{"total":0.03}}}}\n')
        r = run._finish_pass("m", d, False, "review", "ok")
        assert r.tokens == 165
        assert (r.tokens_input + r.tokens_output
                + r.tokens_cache_read + r.tokens_cache_write) == r.tokens
        # The ratio the cache-hit panel charts, on the shape it will actually meet.
        assert r.tokens_cache_read / r.tokens < 1
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_finish_pass_leaves_a_folding_providers_numbers_alone():
    # The deliberate non-normalisation. This provider folds its 50 cached tokens into
    # input, so the components overshoot the authoritative total (160 vs 110) — and the
    # split is still emitted exactly as reported, not "corrected" by subtracting cacheRead
    # from input the way _chat does for the OpenAI-shaped merge usage.
    #
    # That asymmetry is the point: _chat KNOWS its shape folds, because the chat API
    # documents prompt_tokens as cache-inclusive. Here the shape varies by provider and
    # nothing in the record says which one this is, so inventing a fresh-input figure
    # would put a number in the metrics that no provider ever sent. A stat that does not
    # add up is diagnosable; a fabricated one is not.
    d = tempfile.mkdtemp(prefix="so-sess-folding-test-")
    try:
        with open(os.path.join(d, "pass.jsonl"), "w") as f:
            f.write('{"message":{"usage":{"input":100,"output":10,"cacheRead":50,'
                    '"cacheWrite":0,"totalTokens":110,"cost":{"total":0.03}}}}\n')
        result = run._finish_pass("m", d, False, "review", "ok")
        assert result.tokens_input == 100, "reported as-is, not 100-50"
        assert result.tokens_cache_read == 50
        assert result.tokens == 110
        assert (result.tokens_input + result.tokens_output
                + result.tokens_cache_read + result.tokens_cache_write) == 160
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

    _stub_pi(fake_run)
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


def _capture_metrics():
    """Swap BOTH emitter entry points for recorders; returns (events, restore).

    Both, because review_pr batches its review + per-pass events through emit_events
    while the skip/error/sweep paths still emit singly — stubbing only one would let the
    other reach the real emitter. That is not a network risk (LOKI_URL is empty in
    tests, so it returns before touching requests) but it would silently drop events the
    assertions are looking for. Recording into one flat list keeps every existing
    assertion written against (event, labels, fields) triples working unchanged.

    restore() is a callable rather than a returned original because there are now two
    attributes to put back, and a test that restored only one would leak a stub into
    every test that ran after it.
    """
    events = []
    real_one, real_many = run.metrics.emit_event, run.metrics.emit_events
    run.metrics.emit_event = lambda ev, labels, fields: events.append((ev, labels, fields))
    run.metrics.emit_events = lambda batch: events.extend(batch)

    def restore():
        run.metrics.emit_event, run.metrics.emit_events = real_one, real_many

    return events, restore


def test_review_pr_emits_one_posted_metrics_event_with_the_run_numbers():
    real = (run.K, run.run_pass, run.PROVIDER)
    run.K, run.PROVIDER = 1, "openrouter"
    real_deps = _stub_review_pr_deps()
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult(
        "finding", "ok", cost=0.03, tokens=1200)
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        # dry_run=False: the "post" goes through the stubbed _gh, so this drives the
        # real posted path — the only place the posted event may be emitted.
        out = run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=False)
        assert out.posted is True
        review_events = [e for e in events if e[0] == "review"]
        assert len(review_events) == 1, events
        _ev, labels, fields = review_events[0]
        assert labels == {"repo": run.REPO, "outcome": "posted"}
        assert fields["pr"] == 9 and fields["sha"] == "cafebabe00"
        assert fields["pass_statuses"] == "ok" and fields["passes_ok"] == 1
        assert fields["tokens"] == 1200 and fields["cost_usd"] == 0.03
        assert "duration_s" in fields and "diff_chars" in fields
    finally:
        run.K, run.run_pass, run.PROVIDER = real
        _restore_review_pr_deps(real_deps)
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_review_pr_emits_one_pass_event_per_pass_with_its_own_spend():
    # The point of per-pass events: with K>1 the review event pools tokens/cost/seconds,
    # so "which pass timed out, and what did it burn before it did" is unanswerable from
    # the aggregate. Passes here differ in every number so pooling would be detectable.
    real = (run.K, run.run_pass, run.PROVIDER)
    run.K, run.PROVIDER = 3, "local"  # local => sequential, so pass order is deterministic
    real_deps = _stub_review_pr_deps()
    results = iter([run.PassResult("finding one", "ok", cost=0.01, tokens=100),
                    run.PassResult("", "timeout", cost=0.50, tokens=900_000),
                    run.PassResult("finding three", "ok", cost=0.02, tokens=300)])
    run.run_pass = lambda wt, m, s, u, session_dir=None: next(results)
    real_merge = run.merge_reviews
    run.merge_reviews = lambda pr, title, passes, model, meta=None: "merged body"
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            out = run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=False)
        assert out.posted is True
        pass_events = [e for e in events if e[0] == "pass"]
        assert len(pass_events) == 3, events
        by_index = {f["pass"]: (labels, f) for _e, labels, f in pass_events}
        assert set(by_index) == {1, 2, 3}, "passes must be identifiable, not anonymous"
        # The timed-out pass is the one worth finding, and it must carry ITS spend —
        # 900k tokens — not a third of the review total. This is the #29 signature.
        labels, timed_out = by_index[2]
        assert labels["outcome"] == "timeout"
        assert timed_out["tokens"] == 900_000 and timed_out["cost_usd"] == 0.50
        assert timed_out["chars"] == 0
        assert by_index[1][1]["tokens"] == 100 and by_index[3][1]["tokens"] == 300
        # pr/sha stay FIELDS. As labels they would mint a Loki stream per PR.
        assert all("pr" not in labels and "sha" not in labels
                   for labels, _f in by_index.values())
        assert all(f["k"] == 3 for _l, f in by_index.values())
    finally:
        run.K, run.run_pass, run.PROVIDER = real
        run.merge_reviews = real_merge
        _restore_review_pr_deps(real_deps)
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_review_event_token_split_covers_the_passes_and_the_merge():
    # The review event pools spend across K passes plus the merge call. Its split has to
    # cover exactly the same calls, or the cache-hit ratio charted from it describes a
    # different review than the cost beside it. Every number here is distinct so any
    # dropped or double-counted contributor changes a total.
    real = (run.K, run.run_pass, run.PROVIDER)
    run.K, run.PROVIDER = 2, "local"  # local => sequential, deterministic pass order
    real_deps = _stub_review_pr_deps()
    results = iter([
        run.PassResult("finding one", "ok", cost=0.01, tokens=1000,
                       tokens_input=100, tokens_output=20,
                       tokens_cache_read=800, tokens_cache_write=80),
        run.PassResult("finding two", "ok", cost=0.02, tokens=2000,
                       tokens_input=200, tokens_output=40,
                       tokens_cache_read=1600, tokens_cache_write=160)])
    run.run_pass = lambda wt, m, s, u, session_dir=None: next(results)
    real_merge = run.merge_reviews

    def fake_merge(pr, title, passes, model, meta=None):
        if meta is not None:
            meta.update({"cost": 0.05, "tokens": 300, "merged": True, "attempts": 1,
                         "tokens_input": 30, "tokens_output": 6,
                         "tokens_cache_read": 240, "tokens_cache_write": 24})
        return "merged body"

    run.merge_reviews = fake_merge
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            out = run.review_pr(11, "t", "cafebabe00", "m", "m", dry_run=False)
        assert out.posted is True
        review = [f for e, _l, f in events if e == "review"]
        assert len(review) == 1, events
        f = review[0]
        # 100 + 200 passes + 30 merge, and so on for each class.
        assert f["tokens_input"] == 330
        assert f["tokens_output"] == 66
        assert f["tokens_cache_read"] == 2640
        assert f["tokens_cache_write"] == 264
        # Same contributors as the pooled figures beside them: 1000 + 2000 + 300.
        assert f["tokens"] == 3300
        assert f["cost_usd"] == 0.08
        # The merge is a call of its own and carries its own split, so "what did merging
        # cost" stays answerable without subtracting the passes from the review.
        merge = [mf for e, _l, mf in events if e == "merge"]
        assert len(merge) == 1
        assert merge[0]["tokens_cache_read"] == 240 and merge[0]["tokens_input"] == 30
    finally:
        run.K, run.run_pass, run.PROVIDER = real
        run.merge_reviews = real_merge
        _restore_review_pr_deps(real_deps)
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_degraded_review_event_carries_the_split_of_what_it_burned():
    # An all-passes-failed review is where the split matters most: the review event is
    # the only record of the spend, and "2M cache reads" and "2M fresh input" are the
    # same pooled number at 5x the price.
    real = (run.K, run.run_pass)
    run.K = 1
    real_deps = _stub_review_pr_deps()
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult(
        "", "timeout", cost=0.40, tokens=900_000,
        tokens_input=500_000, tokens_output=0,
        tokens_cache_read=400_000, tokens_cache_write=0)
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            run.review_pr(12, "t", "cafebabe00", "m", "m", dry_run=False)
        review = [(labels, f) for e, labels, f in events if e == "review"]
        assert len(review) == 1, events
        labels, f = review[0]
        assert labels["outcome"] == "degraded"
        assert f["tokens_input"] == 500_000
        assert f["tokens_cache_read"] == 400_000
        assert f["tokens"] == 900_000
    finally:
        run.K, run.run_pass = real
        _restore_review_pr_deps(real_deps)
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_review_and_pass_events_ride_one_push():
    # Instrumenting K passes must not turn one HTTP round trip into K+1 — the emitter
    # runs after the review is posted with a 3s read timeout per call, so a loop would
    # multiply the worst-case stall by K on a black-holed endpoint.
    real = (run.K, run.run_pass, run.PROVIDER)
    run.K, run.PROVIDER = 2, "local"
    real_deps = _stub_review_pr_deps()
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult(
        "finding", "ok", cost=0.01, tokens=100)
    real_merge = run.merge_reviews
    run.merge_reviews = lambda pr, title, passes, model, meta=None: "merged body"
    batches = []
    real_one, real_many = run.metrics.emit_event, run.metrics.emit_events
    run.metrics.emit_event = lambda ev, labels, fields: batches.append([(ev, labels, fields)])
    run.metrics.emit_events = lambda batch: batches.append(list(batch))
    _capture_annotations()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=False)
        assert len(batches) == 1, f"expected a single push, got {len(batches)}: {batches}"
        # K=2 so a union merge runs: review + 2 passes + the merge, still one push.
        assert [e[0] for e in batches[0]] == ["review", "pass", "pass", "merge"]
    finally:
        run.K, run.run_pass, run.PROVIDER = real
        run.merge_reviews = real_merge
        _restore_review_pr_deps(real_deps)
        run.metrics.emit_event, run.metrics.emit_events = real_one, real_many
        run._annotate = _REAL_ANNOTATE


def test_every_event_carries_the_id_of_the_trace_that_was_exported():
    # The join key. A dashboard row links to a waterfall by putting `trace_id` in the
    # Loki event, so the id on the events and the id on the exported spans have to be
    # the SAME id — that is the entire mechanism, and nothing else checks it end to end.
    real = (run.K, run.run_pass, run.PROVIDER)
    run.K, run.PROVIDER = 2, "local"
    real_deps = _stub_review_pr_deps()
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult(
        "finding", "ok", cost=0.01, tokens=100)
    real_merge = run.merge_reviews
    run.merge_reviews = lambda pr, title, passes, model, meta=None: "merged body"
    events, real_emit = _capture_metrics()
    exported = []
    real_tracing = (run.tracing.enabled, run.tracing.export)
    run.tracing.enabled = lambda: True
    run.tracing.export = lambda spans, resource_attrs=None: exported.append(spans)
    _capture_annotations()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=False)
        ids = {f.get("trace_id") for _ev, _lab, f in events}
        assert len(ids) == 1 and None not in ids, f"every event needs the id: {events}"
        assert {s["traceId"] for s in exported[0]} == ids, "events point at another trace"
    finally:
        run.K, run.run_pass, run.PROVIDER = real
        run.merge_reviews = real_merge
        _restore_review_pr_deps(real_deps)
        run.tracing.enabled, run.tracing.export = real_tracing
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_no_trace_id_field_when_tracing_is_off():
    # Absent, not blank. The dashboard turns this field into a link, so a row carrying an
    # id for a trace that was never exported is a dead link — worse than no link, because
    # it reads as "the trace is missing" rather than "tracing is off".
    real = (run.K, run.run_pass, run.PROVIDER)
    run.K, run.PROVIDER = 1, "openrouter"
    real_deps = _stub_review_pr_deps()
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult("finding", "ok")
    events, real_emit = _capture_metrics()
    real_enabled = run.tracing.enabled
    run.tracing.enabled = lambda: False
    _capture_annotations()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=False)
        assert events, "the review event should still be emitted"
        assert all("trace_id" not in f for _ev, _lab, f in events), events
    finally:
        run.K, run.run_pass, run.PROVIDER = real
        _restore_review_pr_deps(real_deps)
        run.tracing.enabled = real_enabled
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_review_pr_dry_run_emits_no_metrics_events():
    # A --dry-run is a preview, not a review — it must not pollute the dashboard's
    # counts or costs.
    real = (run.K, run.run_pass, run.PROVIDER)
    run.K, run.PROVIDER = 1, "openrouter"
    real_deps = _stub_review_pr_deps()
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult("finding", "ok")
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=True)
        assert events == [], events
    finally:
        run.K, run.run_pass, run.PROVIDER = real
        _restore_review_pr_deps(real_deps)
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_review_pr_checkout_failure_emits_a_degraded_event():
    # _stub_review_pr_deps captures the originals FIRST, then the overrides go on top —
    # swapping _gh/_git directly with no restore is the cross-test pollution the
    # helper's docstring warns about.
    real_deps = _stub_review_pr_deps()
    run._git = lambda args, check=True: FakeProc(returncode=1, stderr="fatal: bad object\n")
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        out = run.review_pr(7, "t", "deadbeef00", "m", "m", dry_run=False)
        assert out == run.ReviewOutcome(posted=False, degraded=True)
        assert len(events) == 1, events
        _ev, labels, fields = events[0]
        assert labels["outcome"] == "degraded"
        assert fields["reason"] == "checkout_failed" and fields["pr"] == 7
    finally:
        _restore_review_pr_deps(real_deps)
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_review_pr_all_passes_empty_emits_a_no_output_degraded_event():
    real = (run.K, run.run_pass, run.PROVIDER)
    run.K, run.PROVIDER = 1, "openrouter"
    real_deps = _stub_review_pr_deps()
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult(
        "", "empty", cost=0.01, tokens=500)
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            out = run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=False)
        assert out == run.ReviewOutcome(posted=False, degraded=True)
        review_events = [e for e in events if e[0] == "review"]
        assert len(review_events) == 1, events
        _ev, labels, fields = review_events[0]
        assert labels["outcome"] == "degraded"
        assert fields["reason"] == "no_output"
        assert fields["pass_statuses"] == "empty"
        assert fields["tokens"] == 500 and fields["cost_usd"] == 0.01
        # The all-passes-failed path is exactly where per-pass spend is the whole
        # question: pooled into the review totals, three passes that died instantly and
        # three that each burned millions look identical.
        pass_events = [e for e in events if e[0] == "pass"]
        assert len(pass_events) == 1, events
        _ev, labels, fields = pass_events[0]
        assert labels["outcome"] == "empty"
        assert fields["pass"] == 1 and fields["status"] == "empty"
        assert fields["tokens"] == 500 and fields["cost_usd"] == 0.01
        assert fields["chars"] == 0, "an empty pass must be visible as zero output"
    finally:
        run.K, run.run_pass, run.PROVIDER = real
        _restore_review_pr_deps(real_deps)
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_sweep_emits_one_error_review_event_and_one_sweep_event():
    # The exactly-one-outcome invariant's subtlest path: review_pr raising must land the
    # PR under outcome="error" (not silence), and the sweep event must report the round
    # with silent_failure — plus the skip counters that reconcile candidates vs reviewed.
    import argparse
    real = (run.review_pr, run.resolve_model, run.write_models_json, run.pr_meta)
    run.resolve_model = lambda: "m"
    run.write_models_json = lambda model: None
    run.pr_meta = lambda n: {"number": n, "headRefOid": "cafebabe00",
                             "title": "t", "isDraft": False}

    def boom(pr, title, sha, model, merge_model, dry_run):
        raise RuntimeError("gh exploded mid-review")

    run.review_pr = boom
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        args = argparse.Namespace(pr=5, dry_run=False, force=True)
        assert run.sweep(args) is True
        review_events = [e for e in events if e[0] == "review"]
        sweep_events = [e for e in events if e[0] == "sweep"]
        assert len(review_events) == 1 and len(sweep_events) == 1, events
        _ev, labels, fields = review_events[0]
        assert labels["outcome"] == "error" and fields["pr"] == 5
        assert "gh exploded" in fields["error"]
        assert "duration_s" in fields   # every review event carries it, error included
        _ev, labels, fields = sweep_events[0]
        assert labels["outcome"] == "silent_failure"
        assert fields["candidates"] == 1 and fields["reviewed"] == 0
        assert fields["skipped_already_reviewed"] == 0 and fields["skipped_draft"] == 0
    finally:
        run.review_pr, run.resolve_model, run.write_models_json, run.pr_meta = real
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_review_pr_duration_spans_the_whole_review_not_just_the_last_pass():
    # Regression for the t0 shadowing both review bots caught on PR #34: the sequential
    # pass loop reused `t0` as its per-pass timer, so the emitted duration_s measured
    # from the start of the LAST pass — under-reporting by ~(K-1)/K on the local
    # provider (K=3 default), the exact panel the metric exists to feed. A fake clock
    # makes the span assertable: presence-of-key alone let this slip through.
    real = (run.K, run.run_pass, run.PROVIDER, run.merge_reviews)
    run.K, run.PROVIDER = 2, "local"     # K>1 + non-openrouter → the sequential branch
    real_deps = _stub_review_pr_deps()
    run.merge_reviews = lambda pr, title, passes, merge_model=None, meta=None: "merged"
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult("finding", "ok")
    real_mono = run.time.monotonic
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 1.0
        return clock["t"]

    run.time.monotonic = tick
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        out = run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=False)
        assert out.posted is True
        fields = [e for e in events if e[0] == "review"][0][2]
        # Review start is the first tick (1.0); each sequential pass ticks twice; the
        # emit ticks once more (6.0). The full span is 5.0 — a shadowed t0 would
        # measure from the last pass's start (4.0) and report 2.0.
        assert fields["duration_s"] >= 4.0, fields["duration_s"]
    finally:
        run.K, run.run_pass, run.PROVIDER, run.merge_reviews = real
        run.time.monotonic = real_mono
        _restore_review_pr_deps(real_deps)
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_review_pr_empty_filtered_diff_emits_a_skipped_event():
    # Every candidate lands under exactly one review outcome. Without this event, a PR
    # whose whole diff is excluded by globs is counted in the sweep's candidates but
    # never appears in any review stream — an unexplained gap on the dashboard.
    real_deps = _stub_review_pr_deps()
    run._gh = lambda args, timeout_s=60: ""     # diff filters to nothing
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        out = run.review_pr(11, "t", "cafebabe00", "m", "m", dry_run=False)
        assert out == run.ReviewOutcome(posted=False, degraded=False)
        assert len(events) == 1, events
        _ev, labels, fields = events[0]
        assert labels["outcome"] == "skipped"
        assert fields["reason"] == "empty_diff" and fields["pr"] == 11
    finally:
        _restore_review_pr_deps(real_deps)
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_watch_loop_emits_a_sweep_error_event_when_a_sweep_throws():
    # A sweep dying BEFORE its end-of-sweep event (open_prs on an API blip, pr_meta,
    # write_models_json) is swallowed by the watch loop — without an error event here,
    # the liveness panel shows a gap identical to a dead daemon. "Up but erroring"
    # must be distinguishable from "gone".
    class _Stop(Exception):
        pass

    def boom(args):
        raise RuntimeError("open_prs exploded")

    def stop_the_loop(_seconds):
        raise _Stop()

    real = (run.sweep, run.time.sleep, sys.argv)
    run.sweep = boom
    run.time.sleep = stop_the_loop
    events, real_emit = _capture_metrics()
    sys.argv = ["run.py", "--watch", "--interval", "1"]
    try:
        import contextlib
        import io
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                run.main()
        except _Stop:
            pass    # one loop iteration is all this test needs
        sweep_errors = [e for e in events
                        if e[0] == "sweep" and e[1].get("outcome") == "error"]
        assert len(sweep_errors) == 1, events
        assert "open_prs exploded" in sweep_errors[0][2]["error"]
    finally:
        run.sweep, run.time.sleep, sys.argv = real
        real_emit()


def test_loki_token_is_stripped_from_the_pi_subprocess_env():
    # Same defense-in-depth as GITHUB_TOKEN: only the parent pushes metrics, so the
    # agent's unsandboxed bash must never see the Loki credential — nor the URL/user:
    # an unauthenticated self-hosted LOKI_URL would let an injected bash forge events
    # or pivot to an internal endpoint.
    real_popen = run.subprocess.Popen
    os.environ["LOKI_TOKEN"] = "glc_metrics_secret"
    os.environ["LOKI_URL"] = "http://internal-loki:3100/loki/api/v1/push"
    os.environ["LOKI_USER"] = "123456"
    seen = {}

    class CapturePopen:
        returncode = 0

        def __init__(self, cmd, **kw):
            seen["env"] = kw.get("env")

        def communicate(self, input=None, timeout=None):
            return ("finding", "")

        def kill(self):
            pass

    run.subprocess.Popen = CapturePopen
    try:
        res = run.run_pass("/wt", "m", "sys", "usr")
        assert res.status == "ok"
        assert "LOKI_TOKEN" not in seen["env"], "Loki token leaked into the pi subprocess"
        assert "LOKI_URL" not in seen["env"] and "LOKI_USER" not in seen["env"]
        assert "GITHUB_TOKEN" not in seen["env"]
    finally:
        run.subprocess.Popen = real_popen
        for k in ("LOKI_TOKEN", "LOKI_URL", "LOKI_USER"):
            os.environ.pop(k, None)


def test_loki_token_is_scrubbed_from_persisted_transcripts():
    # A prompt-injected agent echoing env into a tool result must not leave the metrics
    # credential in a transcript a consumer uploads as an artifact.
    os.environ["LOKI_TOKEN"] = "glc_transcript_secret"
    try:
        assert "glc_transcript_secret" in run._secret_values()
        scrubbed = run._redact_text("token is glc_transcript_secret here")
        assert "glc_transcript_secret" not in scrubbed and "[REDACTED]" in scrubbed
    finally:
        os.environ.pop("LOKI_TOKEN", None)


# Keep this LAST: it iterates globals() at execution time, so any test defined
# below it would be silently skipped by the `python -m tests.test_run` runner
# (found by PR #18's own review bots — the appended tests were being skipped).
if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)


def test_merge_meta_records_a_recovered_first_attempt():
    # merged_on_retry is the leading indicator: today a merge that fails once and
    # recovers annotates nothing (the annotation fires only when BOTH attempts fail), so
    # the provider degrading is visible solely as a stdout line nobody reads.
    calls = []

    def post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return _Resp({"choices": []})
        return _Resp({"choices": [{"message": {"content": "merged"}}]})

    run.requests.post = post
    ann = _capture_annotations()
    meta: dict = {}
    try:
        assert run.merge_reviews(1, "t", ["a", "b"], meta=meta) == "merged"
        assert meta["attempts"] == 2
        assert len(meta["failures"]) == 1, meta
        assert "no usable content" in meta["failures"][0]
        assert meta.get("merged", True) is True, "it DID merge, on the retry"
        assert ann == [], "a recovered merge still must not annotate"
        # The event this feeds must call it out as distinct from a clean first-try merge.
        _ev, labels, fields = run._merge_event(1, "sha", "m", meta)
        assert labels["outcome"] == "merged_on_retry"
        assert fields["attempts"] == 2 and fields["merged"] is True
    finally:
        run._annotate = _REAL_ANNOTATE


def test_merge_event_keeps_both_failure_reasons_on_fallback():
    # Credits exhaustion looks like a 402 then an empty 200 — persistent, recurring every
    # sweep. Keeping only the last reason files it as "the model flaked", which is the
    # wrong diagnosis and the wrong remedy.
    calls = []

    def post(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("402 insufficient credits")
        return _Resp({"choices": [{"message": {"content": ""}}]})

    run.requests.post = post
    _capture_annotations()
    meta: dict = {}
    try:
        out = run.merge_reviews(1, "t", ["pass one", "pass two"], meta=meta)
        assert "pass one" in out and "pass two" in out, "fallback posts the raw passes"
        assert meta["merged"] is False
        _ev, labels, fields = run._merge_event(7, "deadbeef", "mm", meta)
        assert labels["outcome"] == "fallback"
        assert fields["attempts"] == 2 and fields["merged"] is False
        assert "402" in fields["failures"], fields
        assert "no usable content" in fields["failures"], fields
        # A flat string, not a list: `| json` does not promote array elements to fields.
        assert isinstance(fields["failures"], str)
    finally:
        run._annotate = _REAL_ANNOTATE


def test_merge_event_absent_when_k_is_one():
    # At K=1 no merge runs, and a zeroed merge event would put "0 attempts, $0" rows into
    # the merge panels for every single-pass repo.
    real = (run.K, run.run_pass, run.PROVIDER)
    run.K, run.PROVIDER = 1, "openrouter"
    real_deps = _stub_review_pr_deps()
    run.run_pass = lambda wt, m, s, u, session_dir=None: run.PassResult("finding", "ok")
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            run.review_pr(9, "t", "cafebabe00", "m", "m", dry_run=False)
        assert [e for e in events if e[0] == "merge"] == [], events
        assert [e[0] for e in events] == ["review", "pass"], events
    finally:
        run.K, run.run_pass, run.PROVIDER = real
        _restore_review_pr_deps(real_deps)
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_sweep_event_reports_config_degradation_after_the_annotation_drain():
    # _CONFIG_ERRORS is drained so a daemon doesn't annotate red every tick — correct for
    # annotations, wrong for monitoring: the ceiling stays inactive for the process's whole
    # life. The sweep event must keep saying so on EVERY sweep, not just the first.
    saved_errors = list(run._CONFIG_ERRORS)
    saved_degraded = list(run._CONFIG_DEGRADED)
    run._CONFIG_ERRORS[:] = ["MAX_PASS_TOKENS='5,000,000' is not a number — ignoring it"]
    run._CONFIG_DEGRADED[:] = []
    real = (run.resolve_model, run.write_models_json, run.open_prs)
    run.resolve_model = lambda: "m"
    run.write_models_json = lambda model: None
    run.open_prs = lambda: []
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        import argparse
        import contextlib
        import io
        args = argparse.Namespace(pr=None, dry_run=False, force=False,
                                  watch=True, interval=1)
        with contextlib.redirect_stdout(io.StringIO()):
            run.sweep(args)
            first = [e for e in events if e[0] == "sweep"][-1]
            assert run._CONFIG_ERRORS == [], "the annotation queue must still drain"
            assert first[2]["config_degraded"] == 1, first
            assert "MAX_PASS_TOKENS" in first[2]["config_problems"], first
            # Second sweep: the drain emptied _CONFIG_ERRORS, so a naive implementation
            # reports a healthy config here while the ceiling is still inactive.
            run.sweep(args)
        second = [e for e in events if e[0] == "sweep"][-1]
        assert second[2]["config_degraded"] == 1, second
        assert "MAX_PASS_TOKENS" in second[2]["config_problems"], second
    finally:
        run.resolve_model, run.write_models_json, run.open_prs = real
        run._CONFIG_ERRORS[:] = saved_errors
        run._CONFIG_DEGRADED[:] = saved_degraded
        real_emit()
        run._annotate = _REAL_ANNOTATE


def test_sweep_event_always_carries_config_degraded_even_when_zero():
    # An alert on config_degraded > 0 shouldn't have to distinguish "no problems" from
    # "field absent" — Loki's `| json` yields no value at all for a missing field.
    saved = list(run._CONFIG_DEGRADED)
    run._CONFIG_DEGRADED[:] = []
    real = (run.resolve_model, run.write_models_json, run.open_prs)
    run.resolve_model = lambda: "m"
    run.write_models_json = lambda model: None
    run.open_prs = lambda: []
    events, real_emit = _capture_metrics()
    _capture_annotations()
    try:
        import argparse
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            run.sweep(argparse.Namespace(pr=None, dry_run=False, force=False,
                                         watch=False, interval=1))
        sweeps = [e for e in events if e[0] == "sweep"]
        assert sweeps and sweeps[-1][2]["config_degraded"] == 0, sweeps
        assert "config_problems" not in sweeps[-1][2], "text only when there is one"
    finally:
        run.resolve_model, run.write_models_json, run.open_prs = real
        run._CONFIG_DEGRADED[:] = saved
        real_emit()
        run._annotate = _REAL_ANNOTATE
