"""Unit tests for the optional Loki event emitter: disabled-by-default really means no
network call, the payload keeps Loki's label discipline (low-cardinality labels, data in
the JSON line), auth wiring, and the never-raise contract. requests is stubbed — no
network. File convention (see test_run.py): module globals are patched directly and
restored in finally.
"""

import json
import os
import sys
import types

os.environ.setdefault("GITHUB_REPO", "o/r")
sys.modules.setdefault("requests", types.ModuleType("requests"))

from second_opinion import metrics  # noqa: E402


class _Resp:
    def __init__(self, fail=False):
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError("400 bad request")


def test_disabled_by_default_never_touches_the_network():
    # The offline invariant hangs on this: PROVIDER=local with no LOKI_URL must make
    # zero cloud calls, so the guard has to come before any requests use.
    calls = []
    metrics.requests.post = lambda *a, **k: calls.append(1)
    real = metrics.LOKI_URL
    metrics.LOKI_URL = ""
    try:
        metrics.emit_event("review", {"repo": "o/r"}, {"pr": 1})
        assert calls == [], "disabled metrics still made a network call"
    finally:
        metrics.LOKI_URL = real


def _configured(**attrs):
    """Set emitter config attrs; returns the originals for the caller's finally."""
    saved = {k: getattr(metrics, k) for k in attrs}
    for k, v in attrs.items():
        setattr(metrics, k, v)
    return saved


def test_event_payload_keeps_label_discipline_and_auth():
    seen = {}

    def post(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return _Resp()

    metrics.requests.post = post
    saved = _configured(LOKI_URL="https://loki.example/loki/api/v1/push",
                        LOKI_USER="123456", LOKI_TOKEN="glc_tok", DELIVERY="action")
    try:
        metrics.emit_event("review", {"repo": "o/r", "outcome": "posted"},
                           {"pr": 42, "sha": "cafebabe", "cost_usd": 0.031, "tokens": 1000})
        assert seen["url"] == "https://loki.example/loki/api/v1/push"
        assert seen["kw"]["auth"] == ("123456", "glc_tok")
        stream = seen["kw"]["json"]["streams"][0]
        # Labels: exactly the low-cardinality set. The PR number must NOT be here —
        # a per-PR label mints a new Loki stream per PR (the cardinality mistake).
        assert stream["stream"] == {"service": "second-opinion", "delivery": "action",
                                    "event": "review", "repo": "o/r", "outcome": "posted"}
        ts, line = stream["values"][0]
        assert ts.isdigit() and len(ts) >= 19, ts   # nanosecond epoch, as a string
        body = json.loads(line)
        assert body == {"event": "review", "pr": 42, "sha": "cafebabe",
                        "cost_usd": 0.031, "tokens": 1000}
    finally:
        _configured(**saved)


def test_no_auth_header_when_user_and_token_are_unset():
    # Self-hosted Loki commonly runs unauthenticated; a ("","") basic-auth pair is not
    # the same as no auth and some proxies reject it.
    seen = {}
    metrics.requests.post = lambda url, **kw: (seen.update(kw), _Resp())[1]
    saved = _configured(LOKI_URL="http://loki:3100/loki/api/v1/push",
                        LOKI_USER="", LOKI_TOKEN="")
    try:
        metrics.emit_event("sweep", {"repo": "o/r"}, {"candidates": 3})
        assert seen["auth"] is None
    finally:
        _configured(**saved)


def test_empty_and_none_labels_are_dropped_and_values_stringified():
    # Loki rejects non-string label values, and an empty label value is just a
    # dead dimension — drop it rather than shipping "".
    seen = {}
    metrics.requests.post = lambda url, **kw: (seen.update(kw), _Resp())[1]
    saved = _configured(LOKI_URL="http://loki/push")
    try:
        metrics.emit_event("review", {"repo": "o/r", "outcome": None, "extra": 3}, {})
        stream = seen["json"]["streams"][0]["stream"]
        assert "outcome" not in stream
        assert stream["extra"] == "3"
    finally:
        _configured(**saved)


def test_emit_never_raises_on_transport_or_http_failure():
    # The module's whole contract: a reviewer that breaks when its monitoring does has
    # the dependency backwards. Both failure shapes — a raising transport and an HTTP
    # error status — must come back as a log line, not an exception.
    saved = _configured(LOKI_URL="http://loki/push")
    try:
        def boom(*a, **k):
            raise OSError("connection refused")

        metrics.requests.post = boom
        metrics.emit_event("review", {"repo": "o/r"}, {"pr": 1})   # must not raise

        metrics.requests.post = lambda url, **kw: _Resp(fail=True)
        metrics.emit_event("review", {"repo": "o/r"}, {"pr": 1})   # must not raise
    finally:
        _configured(**saved)


def test_unjsonable_field_values_do_not_kill_the_event():
    # Fields are assembled from runtime objects; a stray non-JSON value must degrade to
    # a string, not lose the whole event to a TypeError inside the emitter.
    seen = {}
    metrics.requests.post = lambda url, **kw: (seen.update(kw), _Resp())[1]
    saved = _configured(LOKI_URL="http://loki/push")
    try:
        metrics.emit_event("review", {"repo": "o/r"}, {"weird": {1, 2}})
        line = json.loads(seen["json"]["streams"][0]["values"][0][1])
        assert "weird" in line
    finally:
        _configured(**saved)


def test_batch_rides_a_single_request_and_groups_by_label_set():
    # emit_events exists so that instrumenting K passes costs ONE round trip, not K+1.
    # Two of these three events share a label set (same repo, both "ok"), so they must
    # merge into one stream rather than repeating the stream object.
    posts = []
    metrics.requests.post = lambda url, **kw: (posts.append(kw), _Resp())[1]
    saved = _configured(LOKI_URL="http://loki/push", DELIVERY="action")
    try:
        metrics.emit_events([
            ("review", {"repo": "o/r", "outcome": "posted"}, {"pr": 7}),
            ("pass", {"repo": "o/r", "outcome": "ok"}, {"pr": 7, "pass": 1}),
            ("pass", {"repo": "o/r", "outcome": "ok"}, {"pr": 7, "pass": 2}),
        ])
        assert len(posts) == 1, f"batch made {len(posts)} requests"
        streams = posts[0]["json"]["streams"]
        assert len(streams) == 2, streams
        by_event = {s["stream"]["event"]: s for s in streams}
        assert set(by_event) == {"review", "pass"}
        # Both ok-passes land in the shared stream, and their timestamps ascend —
        # per-stream ordering is Loki's rule, not something to leave to its
        # out-of-order window (a configurable, not a promise).
        values = by_event["pass"]["values"]
        assert len(values) == 2, values
        assert [int(v[0]) for v in values] == sorted(int(v[0]) for v in values)
        assert {json.loads(v[1])["pass"] for v in values} == {1, 2}
    finally:
        _configured(**saved)


def test_empty_batch_makes_no_request():
    # A K=0 / no-passes batch must be a no-op, not an empty push for Loki to reject.
    posts = []
    metrics.requests.post = lambda url, **kw: (posts.append(kw), _Resp())[1]
    saved = _configured(LOKI_URL="http://loki/push")
    try:
        metrics.emit_events([])
        assert posts == []
    finally:
        _configured(**saved)


def test_emit_events_never_raises_and_stays_off_when_disabled():
    # Same two contracts as emit_event: monitoring must never break a review, and
    # disabled must mean no network call at all (the PROVIDER=local offline invariant).
    calls = []
    metrics.requests.post = lambda *a, **k: calls.append(1)
    saved = _configured(LOKI_URL="")
    try:
        metrics.emit_events([("pass", {"repo": "o/r"}, {"pr": 1})])
        assert calls == []
    finally:
        _configured(**saved)

    saved = _configured(LOKI_URL="http://loki/push")
    try:
        def boom(*a, **k):
            raise OSError("connection refused")
        metrics.requests.post = boom
        metrics.emit_events([("pass", {"repo": "o/r"}, {"pr": 1})])   # must not raise
        metrics.requests.post = lambda url, **kw: _Resp(fail=True)
        metrics.emit_events([("pass", {"repo": "o/r"}, {"pr": 1})])   # must not raise
        # A malformed triple is a caller bug, but it still must not escape the emitter.
        metrics.emit_events([("pass", {"repo": "o/r"})])              # must not raise
    finally:
        _configured(**saved)
