"""Real repositories for the commit / push / CI-wait turn.

The fixtures here build an actual bare "remote" on disk with an actual pull
request branch and an actual ``refs/pull/N/head``, plus an actual candidate
patch captured the way the fix turn captures one -- through
:func:`review_loop.agent_workspace.inspect_workspace`, so the patch under test
is produced by the code that produces real ones rather than by a test helper
that resembles it.

Nothing here reaches the network, and no fixture writes outside ``tmp_path``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from push_fakes import DEFAULT_BRANCH_NAME, DEFAULT_NUMBER, git
from review_loop.agent_workspace import inspect_workspace


@dataclass(frozen=True)
class Scenario:
    """One pull request, its remote, a local clone, and a candidate patch."""

    origin: Path
    clone: Path
    seed: Path
    base_sha: str
    head_sha: str
    branch: str
    number: int
    patch_path: str
    patch_sha256: str
    patch_bytes: int
    changed_paths: tuple[str, ...]

    def remote_tip(self, branch: str | None = None) -> str:
        ref = f"refs/heads/{branch or self.branch}"
        line = git(self.clone, "ls-remote", "origin", ref)
        return line.split("\t")[0] if line else ""


def _default_edits(worktree: Path) -> None:
    (worktree / "pkg" / "code.py").write_text("value = 2\n")
    (worktree / "pkg" / "new.py").write_text("added = True\n")


@pytest.fixture
def scenario(tmp_path) -> Scenario:
    """A pull request branch one commit ahead of master, and a patch for it."""
    return build_scenario(tmp_path, _default_edits)


def build_scenario(tmp_path, edits, *, branch: str = DEFAULT_BRANCH_NAME,
                   number: int = DEFAULT_NUMBER) -> Scenario:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--quiet", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet")
    (seed / "README.md").write_text("first\n")
    (seed / ".gitignore").write_text(".env\ncredentials.json\n__pycache__/\n*.pyc\n")
    (seed / "pkg").mkdir()
    (seed / "pkg" / "code.py").write_text("value = 1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "first")
    base_sha = git(seed, "rev-parse", "HEAD")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")

    (seed / "pkg" / "feature.py").write_text("feature = True\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", f"pr {number}")
    head_sha = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
    git(seed, "push", "--quiet", "origin", f"HEAD:refs/pull/{number}/head")

    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(bare), str(clone))

    patch_path, digest, size, changed = capture_candidate_patch(
        clone, head_sha, edits, tmp_path / "candidate.patch"
    )

    return Scenario(
        origin=bare,
        clone=clone,
        seed=seed,
        base_sha=base_sha,
        head_sha=head_sha,
        branch=branch,
        number=number,
        patch_path=str(patch_path),
        patch_sha256=digest,
        patch_bytes=size,
        changed_paths=changed,
    )


def capture_candidate_patch(clone: Path, head_sha: str, edits, destination: Path):
    """Produce a candidate patch exactly as a fix turn would.

    A detached worktree at the reviewed head, the edits applied in it, and
    then :func:`inspect_workspace` -- the same function, with the same
    canonical diff, so the digest under test is the digest the fix turn would
    have recorded.
    """
    worktree = clone.parent / "fixturetree"
    git(clone, "worktree", "add", "--detach", "--quiet", str(worktree), head_sha)
    try:
        edits(worktree)
        inspection = inspect_workspace(str(worktree), target_head_sha=head_sha)
    finally:
        git(clone, "worktree", "remove", "--force", str(worktree), check=False)

    destination.write_text(inspection.patch, encoding="utf-8")
    return (
        destination,
        inspection.patch_sha256,
        inspection.patch_bytes,
        inspection.changed_paths,
    )
