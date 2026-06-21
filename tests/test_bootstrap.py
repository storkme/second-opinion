"""Unit tests for bootstrap.py — corpus mining, the decorrelation filter, prompt assembly.
gh (subprocess) and the model call are stubbed; no network. Run with pytest."""
import os
import sys
import types

os.environ.setdefault("GITHUB_REPO", "o/r")
os.environ.setdefault("GITHUB_TOKEN", "t")
os.environ.setdefault("OPENROUTER_API_KEY", "sk")
sys.modules.setdefault("requests", types.ModuleType("requests"))

from second_opinion import bootstrap as b  # noqa: E402


def test_pr_findings_parses_and_excludes_our_own_comments():
    def fake_gh(args, timeout_s=120):
        joined = " ".join(args)
        if "/comments" in joined:  # inline review comments
            return (
                '{"author":"claude[bot]","path":"a.py","body":"off-by-one on the boundary"}\n'
                # second-opinion's own comment must be filtered out (decorrelation):
                '{"author":"github-actions[bot]","path":null,"body":"<!-- second-opinion sha=abc -->\\n### second opinion"}\n'
            )
        if "/reviews" in joined:  # review summaries
            return '{"author":"alice","body":"this migration looks risky"}\n'
        return "[]"
    b._gh = fake_gh
    bodies = [f["body"] for f in b.pr_findings("o/r", 5)]
    assert any("off-by-one" in x for x in bodies)            # inline finding kept
    assert any("migration looks risky" in x for x in bodies)  # review summary kept
    assert not any("second opinion" in x for x in bodies)     # our own comment dropped


def test_pr_findings_survives_gh_errors():
    def boom(args, timeout_s=120):
        raise RuntimeError("gh api: 404")
    b._gh = boom
    assert b.pr_findings("o/r", 9) == []  # both calls fail -> empty, no crash


def test_build_corpus_formats_and_caps_at_pr_boundaries():
    items = [
        (1, "Title A", [{"author": "claude[bot]", "path": "x.py", "body": "bug one"}]),
        (2, "Title B", [{"author": "bob", "path": None, "body": "bug two"}]),
    ]
    corpus = b.build_corpus(items, max_chars=100000)
    assert "=== PR #1: Title A ===" in corpus
    assert "[claude[bot]] x.py: bug one" in corpus
    assert "[bob] bug two" in corpus
    # a tiny cap drops whole PR blocks (never cuts mid-block) — here nothing fits
    assert b.build_corpus(items, max_chars=1) == ""


def test_synthesize_appends_corpus_raw_and_tolerates_braces():
    seen = {}
    b._chat = lambda base, key, model, prompt: (seen.update(prompt=prompt), "## guidance\n- check X")[1]
    out = b.synthesize("=== PR #1 ===\n- [r] code with { braces } here", "proj", "m", 1, 1)
    assert out == "## guidance\n- check X"
    # braces in the corpus must not blow up .format(); they reach the model verbatim
    assert "{ braces }" in seen["prompt"]
    assert "proj" in seen["prompt"]


def test_synthesize_raises_clean_on_empty():
    b._chat = lambda *a, **k: ""
    try:
        b.synthesize("c", "p", "m", 1, 1)
    except RuntimeError as e:
        assert "no usable content" in str(e)
    else:
        raise AssertionError("expected RuntimeError on empty synthesis")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
