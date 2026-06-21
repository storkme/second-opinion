"""Unit tests for bootstrap.py — corpus mining, structural decorrelation, prompt assembly.
gh (subprocess) and the model call are stubbed; no network. Run with pytest."""
import os
import sys
import types

os.environ.setdefault("GITHUB_REPO", "o/r")
os.environ.setdefault("GITHUB_TOKEN", "t")
os.environ.setdefault("OPENROUTER_API_KEY", "sk")
sys.modules.setdefault("requests", types.ModuleType("requests"))

from second_opinion import bootstrap as b  # noqa: E402


def test_pr_findings_parses_and_only_mines_pulls_endpoints():
    """Findings come from inline comments + review summaries; decorrelation is structural —
    we only hit the *pulls* endpoints, never *issues* (where second-opinion posts)."""
    endpoints = []

    def fake_gh(args, timeout_s=120):
        if args and args[0] == "api":
            endpoints.append(args[1])
        joined = " ".join(args)
        if "/comments" in joined:
            return '{"author":"claude[bot]","path":"a.py","body":"off-by-one on the boundary"}\n'
        if "/reviews" in joined:
            return '{"author":"alice","body":"this migration looks risky"}\n'
        return "[]"

    b._gh = fake_gh
    bodies = [f["body"] for f in b.pr_findings("o/r", 5)]
    assert any("off-by-one" in x for x in bodies)             # inline finding kept
    assert any("migration looks risky" in x for x in bodies)   # review summary kept
    # structural decorrelation: never read issues/{n}/comments (where our advisory lives)
    assert endpoints and all("/pulls/" in e for e in endpoints)
    assert not any("/issues/" in e for e in endpoints)


def test_sample_evenly_spans_full_range():
    prs = [{"number": i} for i in range(100, 0, -1)]  # 100 PRs, newest-first (#100..#1)
    s = b._sample_evenly(prs, 10)
    nums = [p["number"] for p in s]
    assert nums[0] == 100 and nums[-1] == 1            # both ends — newest AND oldest
    assert len(s) <= 10
    assert nums == sorted(nums, reverse=True)          # newest-first order preserved
    assert b._sample_evenly(prs, 500) == prs           # k >= n -> all
    assert len(b._sample_evenly(prs, 1)) == 1          # k == 1 -> most recent only
    assert b._sample_evenly(prs, 0) == []              # k <= 0 -> none (hybrid's "rest" tail)


def test_pr_findings_survives_gh_errors():
    def boom(args, timeout_s=120):
        raise RuntimeError("gh api: 404")
    b._gh = boom
    assert b.pr_findings("o/r", 9) == []  # both calls fail -> empty, no crash


def test_build_corpus_formats_caps_and_counts_included_only():
    items = [
        (1, "Title A", [{"author": "claude[bot]", "path": "x.py", "body": "bug one"}]),
        (2, "Title B", [{"author": "bob", "path": None, "body": "bug two"}]),
    ]
    corpus, n_prs, n_findings = b.build_corpus(items, max_chars=100000)
    assert "=== PR #1: Title A ===" in corpus
    assert "[claude[bot]] x.py: bug one" in corpus
    assert "[bob] bug two" in corpus
    assert (n_prs, n_findings) == (2, 2)
    # tiny cap drops whole oversized blocks; counts reflect only what's actually included
    assert b.build_corpus(items, max_chars=1) == ("", 0, 0)


def test_build_corpus_caps_findings_per_pr():
    many = [{"author": "r", "path": "f.py", "body": f"finding {i}"} for i in range(20)]
    corpus, n_prs, n_findings = b.build_corpus([(1, "Hot PR", many)], max_chars=100000, max_per_pr=5)
    assert n_findings == 5                                              # only 5 included
    assert sum(1 for ln in corpus.splitlines() if ln.startswith("- [")) == 5
    assert "+15 more findings on this PR" in corpus                     # richness still noted


def test_synthesize_appends_corpus_raw_and_tolerates_braces():
    seen = {}
    b._chat = lambda base, key, model, prompt: seen.update(prompt=prompt) or "## g\n- X"
    out = b.synthesize("=== PR #1 ===\n- [r] code with { braces } here", "proj", "m", 1, 1)
    assert out == "## g\n- X"
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


def test_synthesize_writes_artifacts_to_save_dir():
    import pathlib
    import tempfile
    b._chat = lambda *a, **k: "## guidance\n- check X"
    d = pathlib.Path(tempfile.mkdtemp())
    b.synthesize("THE CORPUS", "proj", "m", 2, 3, save_dir=d)
    assert (d / "corpus.txt").read_text() == "THE CORPUS"
    assert (d / "prompt.txt").read_text().endswith("THE CORPUS")
    assert (d / "response.md").read_text() == "## guidance\n- check X"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
