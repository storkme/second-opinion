"""Optional runtime monitoring: structured JSON events per review/pass/sweep, pushed to a
Loki endpoint (Grafana Cloud or self-hosted — the push API is identical) so runtime,
cost, degraded-rate, and review rounds per PR are graphable without any scrape
infrastructure. Push, not scrape, because the GitHub Action delivery is an ephemeral
single-shot job nothing can scrape; the daemon uses the same path so both deliveries
instrument once, in run.py.

Off by default: a no-op unless LOKI_URL is set, which keeps PROVIDER=local fully
offline (repo invariant). Strictly fail-soft: emit_event never raises — monitoring
must never degrade, delay, or fail a review — and it runs after the review is posted,
never before.

Env:
  LOKI_URL     full push endpoint, e.g. https://logs-prod-XXX.grafana.net/loki/api/v1/push
  LOKI_USER    basic-auth user (Grafana Cloud: the numeric Loki instance id). Optional —
               a self-hosted Loki without auth needs only LOKI_URL.
  LOKI_TOKEN   basic-auth password (Grafana Cloud: an access-policy token scoped to
               logs:write ONLY — write-only bounds the loss if the container is
               compromised; see README Security). Optional, as above.

Label discipline: stream labels carry the LOW-cardinality dimensions only (service,
delivery, repo, event, outcome). Everything per-review — PR number, sha, cost, tokens,
durations — rides inside the JSON line, where LogQL's `| json` parser promotes it at
query time. A PR number in a *label* would mint a new Loki stream per PR, the classic
cardinality mistake.
"""
from __future__ import annotations

import json
import os
import time

import requests

LOKI_URL = os.environ.get("LOKI_URL", "").strip()
LOKI_USER = os.environ.get("LOKI_USER", "").strip()
LOKI_TOKEN = os.environ.get("LOKI_TOKEN", "").strip()
# (connect, read), both small: a stalled endpoint (connection accepted, response
# never sent — a black-holed proxy, a saturated Loki) costs the read budget on EVERY
# push, N+1 events per daemon sweep, so a generous budget lets the monitoring
# throttle the loop it monitors. A push is a few hundred bytes answered in
# milliseconds by a healthy Loki; 3s is ample. Connection-refused fails fast anyway.
CONNECT_TIMEOUT_S = 3.05
PUSH_TIMEOUT_S = 3

# "action" | "daemon" | "oneshot" — set by run.main() once the run mode is known.
DELIVERY = "oneshot"


def emit_event(event: str, labels: dict, fields: dict) -> None:
    """Push one event to Loki. Never raises: a failed push is one log line, because a
    reviewer that breaks when its monitoring does has the dependency backwards."""
    emit_events([(event, labels, fields)])


def emit_events(events) -> None:
    """Push a BATCH of (event, labels, fields) triples in ONE request.

    Batching is not an optimisation here, it is what makes per-pass events free: a K=3
    review emits 1 review + 3 pass events, and looping emit_event would turn one HTTP
    round trip into four — four chances to hit the read timeout, on a path whose whole
    contract is "never delay the review". One request, one timeout budget, one failure.

    Entries sharing a label set are merged into a single stream with their values in
    ascending timestamp order. Loki accepts repeated identical `stream` objects in a
    payload, but per-stream ordering is a rule worth honouring rather than relying on
    the server's out-of-order tolerance, which is a configurable window, not a promise.

    Never raises — same contract as emit_event, and for the same reason.
    """
    if not LOKI_URL:
        return
    try:
        grouped: dict = {}
        for event, labels, fields in events:
            stream = {"service": "second-opinion", "delivery": DELIVERY, "event": event}
            stream.update({k: str(v) for k, v in labels.items() if v not in (None, "")})
            line = json.dumps({"event": event, **fields}, default=str)
            # Sorted items as the key so two identical label sets built in different
            # orders still land in one stream.
            grouped.setdefault(tuple(sorted(stream.items())), []).append(
                [str(time.time_ns()), line])
        if not grouped:
            return  # an empty batch is a no-op, not an empty push Loki would 400 on
        # Sort on the integer, not the string: ns timestamps are equal-width today, so
        # lexicographic order happens to match, but that is an accident of the epoch.
        payload = {"streams": [{"stream": dict(k),
                                "values": sorted(v, key=lambda e: int(e[0]))}
                               for k, v in grouped.items()]}
        auth = (LOKI_USER, LOKI_TOKEN) if (LOKI_USER or LOKI_TOKEN) else None
        resp = requests.post(LOKI_URL, json=payload, auth=auth,
                             timeout=(CONNECT_TIMEOUT_S, PUSH_TIMEOUT_S))
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 — fail-soft is the module's contract
        try:
            print(f"[{time.strftime('%H:%M:%S')}] metrics: push failed "
                  f"({' '.join(str(e).split())[:120]}) — continuing", flush=True)
        except Exception:  # noqa: BLE001 — a broken stdout pipe must not breach never-raise
            pass
