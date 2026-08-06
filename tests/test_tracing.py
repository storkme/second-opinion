"""Unit tests for the optional OTLP trace exporter: disabled means no network, the
payload matches OTLP/JSON's typed-attribute encoding (the usual cause of a collector
400), spans reconstructed from a pi transcript have sane structure, and nothing here can
ever raise into a posted review. requests is stubbed — no network.
"""

import json
import os
import sys
import types

os.environ.setdefault("GITHUB_REPO", "o/r")
sys.modules.setdefault("requests", types.ModuleType("requests"))

from second_opinion import tracing  # noqa: E402


class _Resp:
    def __init__(self, fail=False):
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError("400 bad request")


def _configured(**attrs):
    saved = {k: getattr(tracing, k) for k in attrs}
    for k, v in attrs.items():
        setattr(tracing, k, v)
    return saved


def _pass(status="ok", timeline=(), tokens=100, cost=0.01, text="finding"):
    return types.SimpleNamespace(status=status, timeline=timeline, tokens=tokens,
                                 cost=cost, text=text)


def test_disabled_makes_no_network_call():
    # Same offline invariant as the Loki emitter: PROVIDER=local with no OTLP_ENDPOINT
    # must not touch the network, so the guard has to precede any requests use.
    calls = []
    tracing.requests.post = lambda *a, **k: calls.append(1)
    saved = _configured(OTLP_ENDPOINT="")
    try:
        assert tracing.enabled() is False
        tracing.export([tracing.span("s", "a" * 32, "b" * 16, None, 1, 2)])
        assert calls == []
    finally:
        _configured(**saved)


def test_attribute_encoding_is_typed_and_bools_beat_ints():
    # bool is an int subclass in Python, so an isinstance(v, int) check placed first
    # would silently ship merged=1 instead of a boolValue. Collectors accept it, and the
    # attribute then can't be filtered as a boolean in TraceQL.
    attrs = tracing._attrs({"s": "x", "i": 7, "f": 1.5, "b": True, "skip": None})
    by_key = {a["key"]: a["value"] for a in attrs}
    assert by_key["s"] == {"stringValue": "x"}
    assert by_key["i"] == {"intValue": "7"}, "OTLP/JSON requires int64 as a string"
    assert by_key["f"] == {"doubleValue": 1.5}
    assert by_key["b"] == {"boolValue": True}
    assert "skip" not in by_key, "None attributes must be dropped, not stringified"


def test_span_clamps_negative_durations():
    # A pass's recorded start and a transcript timestamp come from different clocks, so
    # end < start is reachable. Collectors reject or mis-render negative durations.
    s = tracing.span("x", "a" * 32, "b" * 16, None, start_ns=1000, end_ns=500)
    assert s["startTimeUnixNano"] == "1000"
    assert s["endTimeUnixNano"] == "1000"


def test_export_never_raises_and_posts_one_request():
    posts = []

    def post(url, **kw):
        posts.append((url, kw))
        return _Resp()

    tracing.requests.post = post
    saved = _configured(OTLP_ENDPOINT="http://collector", OTLP_USER="1", OTLP_TOKEN="t")
    try:
        spans = [tracing.span(f"s{i}", "a" * 32, f"{i:016x}", None, 1, 2) for i in range(3)]
        tracing.export(spans, resource_attrs={"deployment.environment": "action"})
        assert len(posts) == 1, "a trace is one request, not one per span"
        url, kw = posts[0]
        assert url == "http://collector/v1/traces"
        assert kw["auth"] == ("1", "t")
        rs = kw["json"]["resourceSpans"][0]
        res = {a["key"]: a["value"]["stringValue"] for a in rs["resource"]["attributes"]}
        assert res["service.name"] == "second-opinion"
        assert res["deployment.environment"] == "action"
        assert len(rs["scopeSpans"][0]["spans"]) == 3

        def boom(*a, **k):
            raise OSError("connection refused")

        tracing.requests.post = boom
        tracing.export(spans)                       # must not raise
        tracing.requests.post = lambda url, **kw: _Resp(fail=True)
        tracing.export(spans)                       # must not raise
    finally:
        _configured(**saved)


def test_empty_span_list_makes_no_request():
    posts = []
    tracing.requests.post = lambda url, **kw: (posts.append(kw), _Resp())[1]
    saved = _configured(OTLP_ENDPOINT="http://collector")
    try:
        tracing.export([])
        assert posts == []
    finally:
        _configured(**saved)


def _write_transcript(tmpdir, entries):
    path = os.path.join(tmpdir, "session.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return path


def test_parse_timeline_derives_turns_and_tools(tmp_path):
    # Mirrors a real pi transcript: assistant message, then its tool results as separate
    # messages. Tool calls issued together share a start, which is what pi actually does.
    _write_transcript(str(tmp_path), [
        {"type": "session", "timestamp": "2026-08-06T23:28:30.000Z"},
        {"type": "message", "timestamp": "2026-08-06T23:28:30.000Z",
         "message": {"role": "user"}},
        {"type": "message", "timestamp": "2026-08-06T23:28:32.000Z",
         "message": {"role": "assistant", "model": "m", "provider": "openrouter",
                     "stopReason": "toolUse", "usage": {"input": 8000, "output": 185}}},
        {"type": "message", "timestamp": "2026-08-06T23:28:32.500Z",
         "message": {"role": "toolResult", "toolName": "bash", "toolCallId": "c1"}},
        {"type": "message", "timestamp": "2026-08-06T23:28:32.500Z",
         "message": {"role": "toolResult", "toolName": "read", "toolCallId": "c2",
                     "isError": True}},
    ])
    tl = tracing.parse_timeline(str(tmp_path))
    kinds = [e["kind"] for e in tl]
    assert kinds == ["turn", "tool", "tool"]

    turn = tl[0]
    assert turn["end_ns"] - turn["start_ns"] == 2_000_000_000, "2s of model time"
    assert turn["attrs"]["gen_ai.usage.output_tokens"] == 185
    assert turn["attrs"]["gen_ai.model"] == "m"

    # Both tools start at the assistant message, not at each other's end.
    assert tl[1]["start_ns"] == turn["end_ns"] == tl[2]["start_ns"]
    assert tl[1]["name"] == "tool bash" and tl[1]["error"] is False
    assert tl[2]["error"] is True, "isError must survive as a span status"
    assert all(e["end_ns"] >= e["start_ns"] for e in tl)


def test_parse_timeline_survives_a_truncated_or_missing_transcript(tmp_path):
    # A killed pass leaves a half-written final line, and a scrubbed/throwaway dir may not
    # exist at all. Either must cost the inner spans, not the whole review's trace.
    assert tracing.parse_timeline(str(tmp_path / "nope")) == []
    p = tmp_path / "s"
    p.mkdir()
    with open(p / "session.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "message", "timestamp": "2026-08-06T23:28:30.000Z",
                             "message": {"role": "assistant"}}) + "\n")
        fh.write('{"type": "message", "timestamp": "2026-08-0')   # truncated mid-write
    tl = tracing.parse_timeline(str(p))
    assert len(tl) == 1, tl
    # A bad timestamp is skipped rather than raising.
    with open(p / "session.jsonl", "a", encoding="utf-8") as fh:
        fh.write("\n" + json.dumps({"type": "message", "timestamp": "not-a-date",
                                    "message": {"role": "assistant"}}) + "\n")
    assert len(tracing.parse_timeline(str(p))) == 1


def test_build_review_trace_nests_passes_and_inner_spans():
    tl = [{"kind": "turn", "name": "llm turn", "start_ns": 100, "end_ns": 200,
           "attrs": {"gen_ai.model": "m"}, "error": False},
          {"kind": "tool", "name": "tool bash", "start_ns": 200, "end_ns": 210,
           "attrs": {"tool.name": "bash"}, "error": False}]
    spans = tracing.build_review_trace(
        repo="o/r", pr=1, sha="deadbeef", model="m", provider="openrouter", k=2,
        outcome="posted", start_ns=50, end_ns=500,
        passes=[_pass(timeline=tl), _pass(status="timeout", timeline=())],
        elapsed={0: 0.1, 1: 0.2},
        merge={"start_ns": 300, "end_ns": 400, "attempts": 2, "merged": False,
               "failures": "402; empty", "tokens": 10, "cost": 0.5})
    by_name = {s["name"]: s for s in spans}
    root = by_name["review"]
    assert "parentSpanId" not in root
    assert {s["traceId"] for s in spans} == {root["traceId"]}, "one trace per review"

    p1, p2 = by_name["pass 1/2"], by_name["pass 2/2"]
    assert p1["parentSpanId"] == root["spanId"]
    # Pass 1 has a transcript, so its span uses the transcript's real instants...
    assert p1["startTimeUnixNano"] == "100" and p1["endTimeUnixNano"] == "210"
    # ...pass 2 has none (a killed pass), so it falls back to start + elapsed.
    assert p2["startTimeUnixNano"] == "50"
    assert p2["endTimeUnixNano"] == str(50 + int(0.2 * 1_000_000_000))
    assert p2["status"]["code"] == tracing.STATUS_ERROR, "a timeout is an error span"

    assert by_name["llm turn"]["parentSpanId"] == p1["spanId"]
    assert by_name["tool bash"]["parentSpanId"] == p1["spanId"]
    assert by_name["llm turn"]["kind"] == tracing.KIND_CLIENT, "model calls are CLIENT"

    merge = by_name["merge"]
    assert merge["parentSpanId"] == root["spanId"]
    assert merge["status"]["code"] == tracing.STATUS_ERROR, "a fallback merge is an error"


def test_build_review_trace_omits_merge_at_k1_and_marks_degraded_root():
    spans = tracing.build_review_trace(
        repo="o/r", pr=1, sha="c0ffee", model="m", provider="openrouter", k=1,
        outcome="degraded", start_ns=0, end_ns=10,
        passes=[_pass(status="empty", text="")], elapsed={0: 0.01}, merge=None)
    names = [s["name"] for s in spans]
    assert "merge" not in names, "no merge runs at K=1"
    root = [s for s in spans if s["name"] == "review"][0]
    assert root["status"]["code"] == tracing.STATUS_ERROR
