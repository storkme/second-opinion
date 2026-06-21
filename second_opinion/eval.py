#!/usr/bin/env python3
"""Eval scorecard — measure the reviewer's recall against a real review loop.

For a merged PR that went through review, reconstruct the diff AS THE REVIEWER FIRST SAW IT
(`merge-base(target, base)..target`, where `target` is the commit the existing reviewer made
the most inline comments on — i.e. pre-fix; reviewing the merged diff can't measure recall
because the fixes are already in it), run second-opinion's agentic reviewer on it, then judge
its findings against ground truth (the existing reviewer's inline comments + the follow-up
fix commits) in one strong-model call.

Per-PR scorecard: recall (substantive = high+medium ground truth), false positives, and
**validExtras** — findings our reviewer raised that the loop MISSED, which is the whole point
of a decorrelated second opinion. Ground truth = inline comments on the *pulls* API (the loop's
reviewers); second-opinion posts to the *issues* stream, so its own output never contaminates
the truth set.

Cost: ~1 agentic review pass + 1 judge call per PR (~$0.3-0.5 on glm-5.2). Use a small PR set;
`--dry-run` reconstructs + lists ground truth with NO model spend.

For trustworthy false-positive / validExtras calls, pass `--judge-model` a DIFFERENT (ideally
stronger) model than the reviewer — a model judging its own output is self-favoring. Recall
(matched vs the loop's fixed comments) is more robust to this; the extras assessment is not.

Runs from a local checkout of the repo (REPO_DIR / cwd) — needs git for reconstruction +
worktrees and `pi` for the agentic pass. Env: GITHUB_REPO, GITHUB_TOKEN, OPENROUTER_API_KEY,
plus the reviewer's usual env (PROVIDER, MODEL, ...).

Usage: second-opinion-eval [PR ...] [--auto N] [--judge-model ID] [--dry-run] [--save-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import tempfile
from pathlib import Path

from . import review as rv
from . import run
from .providers import write_models_json

JUDGE_PROMPT = """\
You are scoring an INDEPENDENT code reviewer (the CANDIDATE) against the ground truth of a
real pull-request review loop.

GROUND TRUTH = the findings the project's existing reviewer raised on the diff, plus the
follow-up commits the author then made to address them.
CANDIDATE = second-opinion's blind review of the SAME pre-fix diff (it never saw the
ground-truth comments — it's an independent second opinion).

For EACH ground-truth finding: assign a `severity` by TRUE impact — high (a correctness,
security, or data-integrity bug), medium (a real but non-blocking issue — perf, a missing
test, a real inconsistency), or low (a nit, style, naming, or docs point). Then MATCH it to a
candidate finding if the candidate raised substantially the same issue (same root cause /
location), even if worded differently. For each MISSED ground-truth finding, tag a `category`:
"in_diff_logic" (visible within the changed lines), "cross_file_reach" (needs reading code or
tests OUTSIDE the diff), or "domain_depth" (subtle domain/statistical reasoning). Assess the
candidate's EXTRA findings (no ground-truth match) as: "valid" (a real issue the loop missed),
"false_positive" (wrong/hallucinated), or "stylistic" (a defensible nit, not a bug).

Output ONLY a single fenced ```json block with this exact shape:
{
  "matched": [{"reference": "<short>", "candidate": "<short>", "severity": "high|medium|low", "matchQuality": "strong|partial"}],
  "missed":  [{"reference": "<short>", "severity": "high|medium|low", "category": "in_diff_logic|cross_file_reach|domain_depth"}],
  "extra":   [{"candidate": "<short>", "assessment": "valid|false_positive|stylistic"}],
  "verdict": "<2-3 sentences: the candidate's review quality vs the loop>"
}
"""


def log(msg: str) -> None:
    run.log(msg)


def _gh_json(path: str, *args: str):
    return json.loads(run._gh(["api", path, *args]))


def _git_out(args: list[str]) -> str:
    p = run._git(args, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {p.stderr.strip()[:140]}")
    return p.stdout


def target_commit(pr: int) -> tuple[str, list[dict]]:
    """The commit the existing reviewer made the MOST inline comments on — its main review
    pass. Reviewers comment incrementally across pushes; the findings cluster on one commit,
    and that commit's diff state is what we feed the reviewer and score against."""
    comments = _gh_json(f"repos/{run.REPO}/pulls/{pr}/comments", "--paginate")
    if not comments:
        raise RuntimeError(f"PR #{pr}: no inline review comments — not a usable reference")
    counts: dict[str, int] = {}
    for c in comments:
        counts[c["commit_id"]] = counts.get(c["commit_id"], 0) + 1
    return max(counts, key=counts.get), comments


def reconstruct(pr: int) -> dict:
    """The pre-fix diff the reviewer first saw, + ground-truth findings + follow-up commits."""
    meta = _gh_json(f"repos/{run.REPO}/pulls/{pr}")
    target, comments = target_commit(pr)
    base_ref = (meta.get("base") or {}).get("ref") or "main"
    run._git(["fetch", "-q", "origin", f"refs/pull/{pr}/head"], check=False)
    run._git(["fetch", "-q", "origin", base_ref], check=False)
    base = _git_out(["merge-base", target, f"origin/{base_ref}"]).strip()
    diff = _git_out(["diff", f"{base}..{target}"])

    gt = [{"path": c.get("path"), "line": c.get("line") or c.get("original_line"),
           "body": (c.get("body") or "").strip()}
          for c in comments if c.get("commit_id") == target and (c.get("body") or "").strip()]

    commits = _gh_json(f"repos/{run.REPO}/pulls/{pr}/commits", "--paginate")
    shas = [c["sha"] for c in commits]
    idx = shas.index(target) if target in shas else -1
    followups = [c["commit"]["message"].splitlines()[0] for c in commits[idx + 1:]] if idx >= 0 else []

    return {"pr": pr, "title": meta.get("title", ""), "target": target, "base": base,
            "diff": diff, "groundTruth": gt, "followups": followups}


def review_diff(rec: dict, model: str) -> str:
    """Run second-opinion's agentic reviewer on the reconstructed diff, in a worktree at the
    reviewed commit. Returns the review text (never posted)."""
    filtered, _files, _trunc = rv.filter_diff(rec["diff"], run._exclude_globs(), run.MAX_DIFF_CHARS)
    if not filtered.strip():
        return ""
    system = rv.system_prompt(run.PROJECT, run._guidance())
    user = (f"PR #{rec['pr']}: {rec['title']}\n\nThe full repository is checked out in your "
            f"working directory at the reviewed commit. Use your tools (read, grep via bash) to "
            f"inspect callers, tests, and definitions. The change to review is this diff:\n\n"
            f"{filtered}\n")
    wt = os.path.join(tempfile.gettempdir(), f"second-opinion-eval-pr{rec['pr']}")
    run._git(["worktree", "remove", "--force", wt], check=False)
    add = run._git(["worktree", "add", "--detach", "--force", wt, rec["target"]], check=False)
    if add.returncode != 0:
        raise RuntimeError(f"worktree add @ {rec['target'][:10]}: {add.stderr.strip()[:120]}")
    try:
        return run.run_pass(wt, model, system, user)
    finally:
        run._git(["worktree", "remove", "--force", wt], check=False)


def _hm(items: list[dict]) -> int:
    return sum(1 for x in items if (x.get("severity") or "").lower() in ("high", "medium"))


def _score(pr: int, sc: dict) -> dict:
    """Recompute metrics deterministically from the judge's own labels — don't trust any
    self-reported floats."""
    matched, missed, extra = sc.get("matched", []), sc.get("missed", []), sc.get("extra", [])
    m_hm, miss_hm = _hm(matched), _hm(missed)
    denom_hm, denom_all = m_hm + miss_hm, len(matched) + len(missed)
    cats: dict[str, int] = {}
    for x in missed:
        k = x.get("category") or "uncategorised"
        cats[k] = cats.get(k, 0) + 1
    return {
        "pr": pr,
        "recallSubstantive": round(m_hm / denom_hm, 3) if denom_hm else None,
        "recallAll": round(len(matched) / denom_all, 3) if denom_all else None,
        "matched": len(matched), "matchedSubstantive": m_hm,
        "missed": len(missed), "substantiveGroundTruth": denom_hm,
        "falsePositives": sum(1 for e in extra if e.get("assessment") == "false_positive"),
        "validExtras": sum(1 for e in extra if e.get("assessment") == "valid"),
        "missByCategory": cats,
        "verdict": sc.get("verdict", ""),
    }


def judge(rec: dict, review_text: str, judge_model: str) -> dict:
    prompt = (JUDGE_PROMPT
              + f"\n\n=== PR #{rec['pr']}: {rec['title']} ===\n"
              + "\n=== GROUND TRUTH (the loop's reviewer findings) ===\n"
              + json.dumps(rec["groundTruth"], indent=2, ensure_ascii=False)
              + "\n\n=== GROUND TRUTH (follow-up fix commits) ===\n"
              + ("\n".join(f"- {m}" for m in rec["followups"]) or "(none)")
              + "\n\n=== CANDIDATE (second-opinion's blind review) ===\n"
              + (review_text or "(empty — reviewer produced nothing)"))
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    # Judge models intermittently return an empty / non-JSON body (seen with
    # gemini-pro-preview on large prompts) — retry once before giving up, so a transient
    # blip doesn't silently drop the PR from the scorecard.
    text = ""
    for _attempt in range(2):
        text = run._chat(run.OPENROUTER_BASE, key, judge_model, prompt)
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
        if m:
            return _score(rec["pr"], json.loads(m.group(1)))
    raise RuntimeError(f"judge ({judge_model}) returned no JSON scorecard after retry: {text[:200]}")


def pick_auto(n: int, window: int = 40) -> list[int]:
    """The n most-reviewed recent merged PRs (by inline-comment count) — good eval references."""
    prs = json.loads(run._gh(["pr", "list", "-R", run.REPO, "--state", "merged",
                              "--limit", str(window), "--json", "number"]))
    scored = []
    for p in prs:
        try:
            cnt = len(_gh_json(f"repos/{run.REPO}/pulls/{p['number']}/comments", "--paginate"))
        except RuntimeError:
            cnt = 0
        if cnt:
            scored.append((cnt, p["number"]))
    scored.sort(reverse=True)
    return [num for _, num in scored[:n]]


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "-", s).strip("-")


def _aggregate(cards: list[dict], save_dir: Path | None, suffix: str = "") -> None:
    if not cards:
        raise SystemExit("No PRs scored.")
    recalls = [c["recallSubstantive"] for c in cards if c["recallSubstantive"] is not None]
    agg = {
        "prs": [c["pr"] for c in cards],
        "meanRecallSubstantive": round(statistics.mean(recalls), 3) if recalls else None,
        "totalFalsePositives": sum(c["falsePositives"] for c in cards),
        "totalValidExtras": sum(c["validExtras"] for c in cards),
    }
    log(f"AGGREGATE · {len(cards)} PR(s) · mean recall(subst)={agg['meanRecallSubstantive']} "
        f"· FP total={agg['totalFalsePositives']} · validExtras total={agg['totalValidExtras']}")
    if save_dir:
        name = f"aggregate.{suffix}.json" if suffix else "aggregate.json"
        (save_dir / name).write_text(json.dumps(agg, indent=2), encoding="utf-8")
    print(json.dumps(agg, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Eval second-opinion's recall against a real review loop")
    ap.add_argument("prs", nargs="*", type=int, help="PR number(s) to evaluate")
    ap.add_argument("--auto", type=int, metavar="N",
                    help="instead of explicit PRs, pick the N most-reviewed recent merged PRs")
    ap.add_argument("--judge-model", default="", help=f"judge model (default = MODEL: {run.MODEL})")
    ap.add_argument("--dry-run", action="store_true",
                    help="reconstruct + list ground truth only — NO agentic pass, NO judge, no spend")
    ap.add_argument("--judge-only", action="store_true",
                    help="re-judge the saved reviews in --save-dir with --judge-model (re-fetches "
                    "ground truth; NO agentic passes) — e.g. to re-grade with an independent judge")
    ap.add_argument("--save-dir", help="persist per-PR review text + scorecards + aggregate here")
    args = ap.parse_args()

    if not run.REPO:
        raise SystemExit("Missing GITHUB_REPO (or GITHUB_REPOSITORY)")
    if not run.TOKEN:
        raise SystemExit("Missing required environment variable: GITHUB_TOKEN")

    save_dir = Path(args.save_dir) if args.save_dir else None
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)

    prs = args.prs or (pick_auto(args.auto) if args.auto else [])
    if not prs:
        raise SystemExit("Pass PR number(s) or --auto N")

    if args.dry_run:
        log(f"dry run · {run.REPO} · {len(prs)} PR(s) — reconstruct + ground truth only")
        for pr in prs:
            try:
                rec = reconstruct(pr)
            except Exception as e:  # noqa: BLE001
                log(f"#{pr}: ERROR {str(e)[:160]}")
                continue
            log(f"#{pr}: {len(rec['groundTruth'])} ground-truth finding(s), "
                f"{len(rec['diff'])} diff chars @ {rec['target'][:10]} — '{rec['title'][:50]}'")
        return

    judge_model = args.judge_model.strip() or run.MODEL

    if args.judge_only:
        if not save_dir:
            raise SystemExit("--judge-only needs --save-dir (where the {pr}.review.md live)")
        if not os.environ.get("OPENROUTER_API_KEY", "").strip():
            raise SystemExit("Missing required environment variable: OPENROUTER_API_KEY")
        log(f"re-judge · {run.REPO} · judge={judge_model} · {len(prs)} PR(s) "
            f"(saved reviews, no agentic passes)")
        cards = []
        for pr in prs:
            rp = save_dir / f"{pr}.review.md"
            if not rp.exists():
                log(f"#{pr}: no saved review at {rp} — skipping")
                continue
            try:
                rec = reconstruct(pr)
                card = judge(rec, rp.read_text(encoding="utf-8"), judge_model)
            except Exception as e:  # noqa: BLE001
                log(f"#{pr}: ERROR {str(e)[:200]} — continuing")
                continue
            cards.append(card)
            log(f"#{pr}: recall(subst)={card['recallSubstantive']} "
                f"({card['matchedSubstantive']}/{card['substantiveGroundTruth']} high+med matched) "
                f"FP={card['falsePositives']} validExtras={card['validExtras']}")
            (save_dir / f"{pr}.scorecard.{_slug(judge_model)}.json").write_text(
                json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")
        _aggregate(cards, save_dir, suffix=_slug(judge_model))
        return

    # Real run: register the provider for the agentic pass, then review + judge each PR.
    model = run.resolve_model()
    if model is None:
        return
    write_models_json(model)
    for name in ("OPENROUTER_API_KEY",):  # judge always uses OpenRouter
        if not os.environ.get(name, "").strip():
            raise SystemExit(f"Missing required environment variable: {name}")

    log(f"eval · {run.REPO} · reviewer={model} · judge={judge_model} · {len(prs)} PR(s)")
    cards: list[dict] = []
    for pr in prs:
        try:
            rec = reconstruct(pr)
            if not rec["groundTruth"]:
                log(f"#{pr}: no ground-truth findings on the reviewed commit — skipping")
                continue
            review_text = review_diff(rec, model)
            card = judge(rec, review_text, judge_model)
        except Exception as e:  # noqa: BLE001 — one PR shouldn't sink the batch
            log(f"#{pr}: ERROR {str(e)[:200]} — continuing")
            continue
        cards.append(card)
        log(f"#{pr}: recall(subst)={card['recallSubstantive']} "
            f"({card['matchedSubstantive']}/{card['substantiveGroundTruth']} high+med matched) "
            f"FP={card['falsePositives']} validExtras={card['validExtras']}")
        if card["missByCategory"]:
            log(f"     misses by lever: {card['missByCategory']}")
        if save_dir:
            (save_dir / f"{pr}.review.md").write_text(review_text or "", encoding="utf-8")
            (save_dir / f"{pr}.scorecard.json").write_text(
                json.dumps(card, indent=2, ensure_ascii=False), encoding="utf-8")

    _aggregate(cards, save_dir)


if __name__ == "__main__":
    main()
