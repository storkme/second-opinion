"""OPTIONAL distributed tracing: one OTLP trace per review, so a review's *shape* is
visible — not just its totals.

The Loki events (metrics.py) answer "what did this review cost and did it work". They
cannot answer "where did the 194 seconds go", because a review is a tree: it fans out to
K parallel passes, each of which is a back-and-forth of model turns and tool calls, and
then a merge. That tree is exactly what a trace is for.

    review  (pr, sha, outcome)
      ├── pass 1  (status, tokens, cost)
      │     ├── turn 1   ← model inference; THIS is where the time goes
      │     ├── tool bash
      │     ├── turn 2
      │     └── ...
      ├── pass 2 …            (K>1 on OpenRouter runs these CONCURRENTLY, and the
      └── merge (attempts)     waterfall shows that overlap directly)

Measured on a real review (second-opinion#36): 194.3s wall clock, 12 tool calls, and
**0.06s of tool execution**. Tool spans are emitted because they are free and they show
what the agent looked at, but they are markers on the timeline, not the substance — the
substance is turn latency and the token growth alongside it.

WHY HAND-ROLLED, NOT THE OTEL SDK: this package's only runtime dependency is `requests`,
and the Action image is rebuilt on every CI run. OTLP over HTTP accepts plain JSON, and
every span here is constructed retroactively from timings already recorded, so there is
nothing an SDK's context propagation would buy — no live context, no sampling decisions,
one POST at the end. metrics.py hand-rolls the Loki push for the same reasons.

Spans are built AFTER the fact from recorded timestamps, which keeps the same contract
metrics.py has: nothing is emitted before the review is posted, and the export can never
delay or fail a review.

Env:
  OTLP_ENDPOINT  base OTLP/HTTP endpoint, e.g.
                 https://otlp-gateway-prod-gb-south-1.grafana.net/otlp
                 ("/v1/traces" is appended). Unset = disabled, no network call.
  OTLP_USER      basic-auth user (Grafana Cloud: the numeric *instance* id — note this
                 is a DIFFERENT id from the Loki one, a genuinely easy mix-up).
  OTLP_TOKEN     basic-auth password (scope it to traces:write only).
"""
from __future__ import annotations

import datetime
import json
import os
import secrets
import time

import requests

OTLP_ENDPOINT = os.environ.get("OTLP_ENDPOINT", "").strip().rstrip("/")
OTLP_USER = os.environ.get("OTLP_USER", "").strip()
OTLP_TOKEN = os.environ.get("OTLP_TOKEN", "").strip()
# Matches metrics.py: a stalled collector must not hold the job open. Traces are larger
# than a log line but still small (tens of KB), and this runs after the review is posted.
CONNECT_TIMEOUT_S = 3.05
EXPORT_TIMEOUT_S = 5

SERVICE_NAME = "second-opinion"

# OTLP span kinds. INTERNAL for our own structure, CLIENT for the model calls (they are
# outbound requests to a remote API, which is what makes turn latency meaningful).
KIND_INTERNAL = 1
KIND_CLIENT = 3

STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2


def enabled() -> bool:
    return bool(OTLP_ENDPOINT)


def _hex(n: int) -> str:
    return secrets.token_hex(n)


def new_trace_id() -> str:
    return _hex(16)


def new_span_id() -> str:
    return _hex(8)


def _attrs(d: dict) -> list:
    """OTLP attribute encoding. Values are typed, and getting this wrong is the usual
    reason a collector 400s a payload, so keep the mapping explicit and total."""
    out = []
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, bool):          # BEFORE int — bool is an int subclass
            val = {"boolValue": v}
        elif isinstance(v, int):
            val = {"intValue": str(v)}   # OTLP/JSON wants 64-bit ints as strings
        elif isinstance(v, float):
            val = {"doubleValue": v}
        else:
            val = {"stringValue": str(v)}
        out.append({"key": k, "value": val})
    return out


def span(name: str, trace_id: str, span_id: str, parent_id: str | None,
         start_ns: int, end_ns: int, attrs: dict | None = None,
         kind: int = KIND_INTERNAL, error: bool = False) -> dict:
    """One OTLP span. end_ns is clamped to start_ns: a negative-duration span is rejected
    by some collectors and renders as garbage in the rest, and clock skew between a
    recorded start and a transcript timestamp can produce one."""
    s = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(int(start_ns)),
        "endTimeUnixNano": str(int(max(end_ns, start_ns))),
        "attributes": _attrs(attrs or {}),
        "status": {"code": STATUS_ERROR if error else STATUS_OK},
    }
    if parent_id:
        s["parentSpanId"] = parent_id
    return s


def export(spans: list, resource_attrs: dict | None = None) -> None:
    """POST spans to the collector. Never raises — same contract as metrics.emit_events,
    and for the same reason: a reviewer that breaks when its telemetry does has the
    dependency backwards."""
    if not OTLP_ENDPOINT or not spans:
        return
    try:
        payload = {"resourceSpans": [{
            "resource": {"attributes": _attrs({"service.name": SERVICE_NAME,
                                               **(resource_attrs or {})})},
            "scopeSpans": [{"scope": {"name": "second-opinion"}, "spans": spans}],
        }]}
        auth = (OTLP_USER, OTLP_TOKEN) if (OTLP_USER or OTLP_TOKEN) else None
        resp = requests.post(f"{OTLP_ENDPOINT}/v1/traces", json=payload, auth=auth,
                             timeout=(CONNECT_TIMEOUT_S, EXPORT_TIMEOUT_S))
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 — fail-soft is the module's contract
        try:
            print(f"[{time.strftime('%H:%M:%S')}] tracing: export failed "
                  f"({' '.join(str(e).split())[:120]}) — continuing", flush=True)
        except Exception:  # noqa: BLE001 — a broken stdout pipe must not breach never-raise
            pass


def _ts_ns(value) -> int | None:
    """pi writes ISO-8601 timestamps with a trailing Z, which datetime.fromisoformat
    rejected before 3.11. Returns None rather than raising: a transcript with one odd
    line should cost that line, not the whole trace."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def parse_timeline(session_dir: str, exclude=()) -> list:
    """Reconstruct a pass's inner timeline from pi's JSONL session transcript.

    Returns [{kind, name, start_ns, end_ns, attrs, error}] in start order. Each entry is
    derived, not measured directly — pi records a timestamp per *message*, not a duration
    per operation — so:

      turn  = previous message → this assistant message. That interval is the model
              generating, which on real reviews is ~99.9% of the wall clock.
      tool  = the assistant message that requested it → the toolResult. Parallel tool
              calls in one turn therefore share a start, which is accurate: pi issues
              them together.

    Returns [] on any problem. A trace is a nice-to-have; a transcript that has been
    scrubbed, truncated by a killed pass, or written by a future pi version must degrade
    to "no inner spans" rather than taking the review's trace with it.
    """
    try:
        files = [os.path.join(session_dir, f) for f in sorted(os.listdir(session_dir))
                 if f.endswith(".jsonl") and os.path.join(session_dir, f) not in exclude]
    except OSError:
        return []

    msgs = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue  # a partial final line is normal for a killed pass
                    if obj.get("type") != "message":
                        continue
                    ns = _ts_ns(obj.get("timestamp"))
                    m = obj.get("message")
                    if ns is None or not isinstance(m, dict):
                        continue
                    msgs.append((ns, m))
        except OSError:
            continue
    msgs.sort(key=lambda x: x[0])

    out: list = []
    last_ns = msgs[0][0] if msgs else None
    # The assistant message a tool result belongs to: pi emits the request, then the
    # results arrive as separate messages, so the request's timestamp is the tool start.
    last_assistant_ns = last_ns
    for ns, m in msgs:
        role = m.get("role")
        if role == "assistant":
            usage = m.get("usage") if isinstance(m.get("usage"), dict) else {}
            out.append({
                "kind": "turn", "name": "llm turn",
                "start_ns": last_ns, "end_ns": ns, "error": False,
                "attrs": {k: v for k, v in {
                    "gen_ai.model": m.get("model"),
                    "gen_ai.provider": m.get("provider"),
                    "gen_ai.usage.input_tokens": usage.get("input"),
                    "gen_ai.usage.output_tokens": usage.get("output"),
                    "stop_reason": m.get("stopReason"),
                }.items() if v is not None},
            })
            last_assistant_ns = ns
        elif role == "toolResult":
            name = m.get("toolName") or "tool"
            out.append({
                "kind": "tool", "name": f"tool {name}",
                "start_ns": last_assistant_ns, "end_ns": ns,
                "error": bool(m.get("isError")),
                "attrs": {"tool.name": name, "tool.call_id": m.get("toolCallId")},
            })
        last_ns = ns
    return out


def build_review_trace(*, repo: str, pr: int, sha: str, model: str, provider: str,
                       k: int, outcome: str, start_ns: int, end_ns: int,
                       passes: list, elapsed: dict, merge: dict | None = None,
                       trace_id: str | None = None) -> list:
    """Assemble the span tree for one review.

    `passes` is the ordered PassResult list, `elapsed` maps pass index -> seconds, and
    `merge` (optional) is {start_ns, end_ns, attempts, merged, failures, tokens, cost}.

    `trace_id` is supplied by the caller so the id can be published BEFORE the trace is
    built — run.py mints it at the top of a review and puts it in the log line and the
    Loki event, which is the only way anything outside Tempo can name this trace. Minted
    here when omitted, which keeps the function usable on its own.

    Deliberately random per review rather than derived from repo+sha: --force and a
    re-run on the same head SHA are separate reviews, and a derived id would collide
    them into one unreadable trace.

    Pass span timing prefers the transcript: its first and last entry are real wall-clock
    instants, whereas start_ns + elapsed is an inference that drifts by whatever the
    harness spent around the pass. Falls back to the inference when a pass produced no
    transcript — which is exactly the degraded case (a killed pass), where the span is
    still worth drawing so the failure has a shape.

    For K>1 on OpenRouter the passes run concurrently, so their spans OVERLAP. That is the
    point: the waterfall shows the fan-out directly, and a single straggler is obvious in
    a way "pass_statuses: ok,timeout,ok" never is.
    """
    trace_id = trace_id or new_trace_id()
    root_id = new_span_id()
    spans = [span("review", trace_id, root_id, None, start_ns, end_ns,
                  attrs={"repo": repo, "pr": pr, "sha": sha, "model": model,
                         "provider": provider, "k": k, "outcome": outcome},
                  error=(outcome not in ("posted", "skipped")))]

    for i, p in enumerate(passes):
        tl = list(getattr(p, "timeline", ()) or ())
        if tl:
            p_start = min(s["start_ns"] for s in tl)
            p_end = max(s["end_ns"] for s in tl)
        else:
            p_start = start_ns
            p_end = start_ns + int(elapsed.get(i, 0) * 1_000_000_000)
        pass_id = new_span_id()
        status = getattr(p, "status", "unknown")
        spans.append(span(f"pass {i + 1}/{k}", trace_id, pass_id, root_id, p_start, p_end,
                          attrs={"pass": i + 1, "status": status,
                                 "tokens": getattr(p, "tokens", 0),
                                 "cost_usd": round(getattr(p, "cost", 0.0), 6),
                                 "chars": len(getattr(p, "text", "") or "")},
                          error=status != "ok"))
        for entry in tl:
            spans.append(span(entry["name"], trace_id, new_span_id(), pass_id,
                              entry["start_ns"], entry["end_ns"],
                              attrs=entry.get("attrs"),
                              kind=KIND_CLIENT if entry["kind"] == "turn" else KIND_INTERNAL,
                              error=bool(entry.get("error"))))

    if merge:
        spans.append(span("merge", trace_id, new_span_id(), root_id,
                          merge.get("start_ns", start_ns), merge.get("end_ns", end_ns),
                          attrs={"attempts": merge.get("attempts", 0),
                                 "merged": merge.get("merged", True),
                                 "failures": merge.get("failures", ""),
                                 "tokens": merge.get("tokens", 0),
                                 "cost_usd": round(merge.get("cost", 0.0), 6)},
                          kind=KIND_CLIENT,
                          error=not merge.get("merged", True)))
    return spans
