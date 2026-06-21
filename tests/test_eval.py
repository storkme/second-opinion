"""Unit tests for eval.py — deterministic scoring + diff reconstruction (gh/git stubbed)."""
import json
import os
import sys
import types

os.environ.setdefault("GITHUB_REPO", "o/r")
os.environ.setdefault("GITHUB_TOKEN", "t")
os.environ.setdefault("OPENROUTER_API_KEY", "sk")
sys.modules.setdefault("requests", types.ModuleType("requests"))

from second_opinion import eval as ev  # noqa: E402
from second_opinion import run  # noqa: E402


class _CP:  # fake subprocess.CompletedProcess
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_score_recomputes_recall_fp_validextras():
    sc = {
        "matched": [{"severity": "high"}, {"severity": "low"}],
        "missed": [{"severity": "medium", "category": "cross_file_reach"},
                   {"severity": "high", "category": "domain_depth"}],
        "extra": [{"assessment": "valid"}, {"assessment": "false_positive"}, {"assessment": "stylistic"}],
        "verdict": "ok",
    }
    card = ev._score(42, sc)
    assert card["recallSubstantive"] == round(1 / 3, 3)   # 1 high+med matched / (1 + 2 missed)
    assert card["matchedSubstantive"] == 1                 # the high; the low match is excluded
    assert card["recallAll"] == round(2 / 4, 3)           # 2 matched / (2 + 2)
    assert card["falsePositives"] == 1
    assert card["validExtras"] == 1
    assert card["missByCategory"] == {"cross_file_reach": 1, "domain_depth": 1}
    assert card["substantiveGroundTruth"] == 3


def test_score_handles_empty_scorecard():
    card = ev._score(1, {})
    assert card["recallSubstantive"] is None and card["recallAll"] is None
    assert card["falsePositives"] == 0 and card["validExtras"] == 0


def test_target_commit_picks_most_commented():
    run._gh = lambda args, timeout_s=60: json.dumps([
        {"commit_id": "aaa", "path": "f", "line": 1, "body": "x"},
        {"commit_id": "bbb", "path": "g", "line": 2, "body": "y"},
        {"commit_id": "bbb", "path": "h", "line": 3, "body": "z"},
    ])
    target, comments = ev.target_commit(7)
    assert target == "bbb" and len(comments) == 3


def test_reconstruct_filters_ground_truth_to_target_and_followups():
    comments = [
        {"commit_id": "T", "path": "a.py", "line": 1, "body": "bug here"},
        {"commit_id": "T", "path": "b.py", "line": 2, "body": "another"},
        {"commit_id": "OLD", "path": "c.py", "line": 3, "body": "on a stale commit"},
    ]
    commits = [{"sha": "OLD", "commit": {"message": "first"}},
               {"sha": "T", "commit": {"message": "review pass"}},
               {"sha": "FIX", "commit": {"message": "address review\nbody"}}]

    def fake_gh(args, timeout_s=60):
        path = args[1]
        if path.endswith("/comments"):
            return json.dumps(comments)
        if path.endswith("/commits"):
            return json.dumps(commits)
        return json.dumps({"title": "T", "base": {"ref": "main"}})  # pulls/{pr}

    def fake_git(args, check=True):
        if args[0] == "merge-base":
            return _CP(stdout="basesha\n")
        if args[0] == "diff":
            return _CP(stdout="DIFFCONTENT")
        return _CP()  # fetch / worktree

    run._gh, run._git = fake_gh, fake_git
    rec = ev.reconstruct(5)
    assert rec["target"] == "T"
    assert rec["base"] == "basesha" and rec["diff"] == "DIFFCONTENT"
    bodies = [f["body"] for f in rec["groundTruth"]]
    assert bodies == ["bug here", "another"]              # only comments on the target commit
    assert "on a stale commit" not in bodies
    assert rec["followups"] == ["address review"]          # only commits AFTER the target


def test_slug_sanitizes_model_id_for_filenames():
    assert ev._slug("anthropic/claude-sonnet-4.5") == "anthropic-claude-sonnet-4.5"
    assert ev._slug("z-ai/glm-5.2") == "z-ai-glm-5.2"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
