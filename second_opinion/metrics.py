"""Optional runtime monitoring: one structured JSON event per review/sweep, pushed to a
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
PUSH_TIMEOUT_S = 10

# "action" | "daemon" | "oneshot" — set by run.main() once the run mode is known.
DELIVERY = "oneshot"


def enabled() -> bool:
    return bool(LOKI_URL)


def emit_event(event: str, labels: dict, fields: dict) -> None:
    """Push one event to Loki. Never raises: a failed push is one log line, because a
    reviewer that breaks when its monitoring does has the dependency backwards."""
    if not LOKI_URL:
        return
    try:
        stream = {"service": "second-opinion", "delivery": DELIVERY, "event": event}
        stream.update({k: str(v) for k, v in labels.items() if v not in (None, "")})
        line = json.dumps({"event": event, **fields}, default=str)
        payload = {"streams": [{"stream": stream,
                                "values": [[str(time.time_ns()), line]]}]}
        auth = (LOKI_USER, LOKI_TOKEN) if (LOKI_USER or LOKI_TOKEN) else None
        resp = requests.post(LOKI_URL, json=payload, auth=auth, timeout=PUSH_TIMEOUT_S)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 — fail-soft is the module's contract
        print(f"[{time.strftime('%H:%M:%S')}] metrics: push failed "
              f"({' '.join(str(e).split())[:120]}) — continuing", flush=True)
