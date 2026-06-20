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
