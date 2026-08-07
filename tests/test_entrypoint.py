"""The reviewed checkout must never shadow the reviewer.

Both deliveries run `python3 -m second_opinion.run` with cwd set to the repo under review.
`-m` prepends cwd to sys.path *ahead of* PYTHONPATH, so a target repo that ships its own
top-level `second_opinion/` package silently replaces the released reviewer with the one
on the branch being reviewed. This repo is exactly such a target, so its own dogfood check
pinned `@v1`, logged that it had downloaded v1.7.0, and then ran the branch's code — which
is how per-pass Loki events appeared for PRs reviewed by a release that cannot emit them.

Two tests, because a grep alone would not have caught this: one asserts the mechanism (that
`-P` is what actually stops the shadowing, so the fix is not cargo-culted), the other that
both entrypoints use it.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _fake_tree(tmp_path):
    """A checkout and an image dir that both provide `second_opinion`, as in production."""
    workspace = tmp_path / "workspace" / "second_opinion"
    image = tmp_path / "opt" / "reviewer" / "second_opinion"
    for d, who in ((workspace, "workspace"), (image, "image")):
        d.mkdir(parents=True)
        (d / "__init__.py").write_text("")
        (d / "run.py").write_text(f"print({who!r})\n")
    return workspace.parent, image.parent


def _run(cwd, pythonpath, *flags):
    proc = subprocess.run(
        [sys.executable, *flags, "-m", "second_opinion.run"],
        cwd=cwd, capture_output=True, text=True,
        env={"PYTHONPATH": str(pythonpath), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_cwd_shadows_the_image_without_dash_p(tmp_path):
    """The bug itself. If this ever fails, -P is no longer load-bearing and the
    entrypoint comments (and the assertion below) need revisiting — not deleting."""
    workspace, image = _fake_tree(tmp_path)
    assert _run(workspace, image) == "workspace"


def test_dash_p_restores_the_image_copy(tmp_path):
    workspace, image = _fake_tree(tmp_path)
    assert _run(workspace, image, "-P") == "image"


def _exec_lines(path):
    """Only the lines that actually launch the reviewer. Matching every mention of
    `second_opinion.run` would also catch the entrypoint's own help text, which is
    prose about the CLI and has no business carrying -P."""
    return [ln.strip() for ln in (ROOT / path).read_text().splitlines()
            if ln.strip().startswith("exec ") and "second_opinion.run" in ln]


def test_action_entrypoint_uses_dash_p():
    lines = _exec_lines("entrypoint.sh")
    assert lines, "entrypoint.sh no longer execs second_opinion.run"
    assert all(" -P " in ln for ln in lines), lines


def test_daemon_entrypoint_uses_dash_p():
    lines = _exec_lines("deploy/docker-compose.yml")
    assert lines, "docker-compose.yml no longer execs second_opinion.run"
    assert all(" -P " in ln for ln in lines), lines
