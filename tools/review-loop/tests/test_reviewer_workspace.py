"""The reviewer's working directory must be the commit under review.

These tests drive real ``git`` against repositories built in ``tmp_path``.
Nothing here reaches the network: the "remote" is a bare repository on disk,
and the pull request head ref is pushed into it the way GitHub would expose
it, as ``refs/pull/N/head``.

A fake git would prove nothing worth having. The whole failure this module
exists to prevent -- a reviewer reading a tree that is not the target -- is a
statement about what git actually did, so the assertions are about real
checkouts, real detached worktrees and a real dirty working tree.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from review_loop.reviewer_process import ReviewerRun
from review_loop.reviewer_workspace import (
    ExistingWorkspace,
    PreparedWorkspace,
    WorkspaceBoundReviewer,
    WorkspaceError,
    verify_checkout,
)


def git(cwd, *argv):
    completed = subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )
    return completed.stdout.strip()


@pytest.fixture
def origin(tmp_path):
    """A bare repository standing in for GitHub, plus its first commit."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--quiet", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet")
    (seed / "README.md").write_text("first\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "--quiet", "-m", "first")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")
    return bare, seed


@pytest.fixture
def clone(tmp_path, origin):
    """A working clone -- the repository the runner would be invoked from."""
    bare, _ = origin
    path = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(bare), str(path))
    return path


def head_of(path):
    return git(path, "rev-parse", "HEAD")


def publish_pull_request(origin_fixture, number, content="second\n"):
    """Add a commit and publish it as refs/pull/<number>/head, as GitHub does."""
    bare, seed = origin_fixture
    (seed / "feature.md").write_text(content)
    git(seed, "add", "feature.md")
    git(seed, "commit", "--quiet", "-m", f"pr {number}")
    sha = head_of(seed)
    git(seed, "push", "--quiet", "origin", f"HEAD:refs/pull/{number}/head")
    return sha


# --------------------------------------------------------------------------
# verify_checkout
# --------------------------------------------------------------------------


def test_a_clean_checkout_of_the_target_is_accepted(clone):
    verify_checkout(str(clone), head_of(clone))


def test_a_checkout_of_another_commit_is_refused(clone, origin):
    """The whole point: right directory, wrong commit."""
    other = publish_pull_request(origin, 7)

    with pytest.raises(WorkspaceError, match="is at .*, not the review target"):
        verify_checkout(str(clone), other)


def test_a_dirty_tracked_file_is_refused(clone):
    """The right commit plus uncommitted edits is not the right commit."""
    (clone / "README.md").write_text("locally edited\n")

    with pytest.raises(WorkspaceError, match="uncommitted or untracked"):
        verify_checkout(str(clone), head_of(clone))


def test_an_untracked_file_is_refused(clone):
    """An untracked file is still a file the reviewer can open and cite."""
    (clone / "scratch-notes.md").write_text("not in the pull request\n")

    with pytest.raises(WorkspaceError, match="uncommitted or untracked"):
        verify_checkout(str(clone), head_of(clone))


def test_a_directory_that_is_not_a_git_work_tree_is_refused(tmp_path, clone):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(WorkspaceError, match="not a git work tree"):
        verify_checkout(str(plain), head_of(clone))


def test_a_missing_directory_is_refused(tmp_path, clone):
    with pytest.raises(WorkspaceError, match="not a directory"):
        verify_checkout(str(tmp_path / "absent"), head_of(clone))


def test_an_abbreviated_target_is_refused_rather_than_resolved(clone):
    """The same refusal the verdict's SHA binding makes, one layer earlier."""
    with pytest.raises(WorkspaceError, match="not a 40-character SHA"):
        verify_checkout(str(clone), head_of(clone)[:7])


# --------------------------------------------------------------------------
# ExistingWorkspace
# --------------------------------------------------------------------------


def test_an_existing_workspace_yields_the_verified_path(clone):
    with ExistingWorkspace(str(clone)).open(head_of(clone)) as path:
        assert path == str(clone)


def test_an_existing_workspace_refuses_before_yielding(clone, origin):
    other = publish_pull_request(origin, 8)
    entered = False

    with pytest.raises(WorkspaceError):
        with ExistingWorkspace(str(clone)).open(other):
            entered = True

    assert entered is False, "the reviewer must not run in an unverified directory"


# --------------------------------------------------------------------------
# PreparedWorkspace
# --------------------------------------------------------------------------


def test_a_prepared_worktree_is_the_target_commit(clone, origin):
    sha = publish_pull_request(origin, 11, content="pull request eleven\n")

    with PreparedWorkspace(str(clone), 11).open(sha) as path:
        assert head_of(path) == sha
        with open(os.path.join(path, "feature.md")) as handle:
            assert handle.read() == "pull request eleven\n"


def test_a_prepared_worktree_does_not_disturb_the_invoking_checkout(clone, origin):
    """The operator's own checkout stays where it was, on whatever branch."""
    before = head_of(clone)
    sha = publish_pull_request(origin, 12)

    with PreparedWorkspace(str(clone), 12).open(sha) as path:
        assert head_of(path) == sha

    assert head_of(clone) == before


def test_a_prepared_worktree_is_removed_afterwards(clone, origin):
    sha = publish_pull_request(origin, 13)

    with PreparedWorkspace(str(clone), 13).open(sha) as path:
        created = path
        assert os.path.isdir(created)

    assert not os.path.isdir(created)
    assert created not in git(clone, "worktree", "list")


def test_a_prepared_worktree_is_removed_even_when_the_review_raises(clone, origin):
    """A reviewer that explodes must not leave a checkout behind."""
    sha = publish_pull_request(origin, 14)
    created = None

    with pytest.raises(RuntimeError):
        with PreparedWorkspace(str(clone), 14).open(sha) as path:
            created = path
            raise RuntimeError("the reviewer exploded")

    assert created is not None
    assert not os.path.isdir(created)


def test_a_head_ref_resolving_elsewhere_is_refused(clone, origin):
    """GitHub's reported head and refs/pull/N/head must be the same commit."""
    publish_pull_request(origin, 15)
    stale = git(clone, "rev-parse", "HEAD")

    with pytest.raises(WorkspaceError, match="resolves to .*, but the review target"):
        with PreparedWorkspace(str(clone), 15).open(stale):
            pytest.fail("a mismatched head ref must not yield a workspace")


def test_a_missing_head_ref_is_refused(clone):
    """No refs/pull/N/head at all is a failure, never an empty workspace."""
    with pytest.raises(WorkspaceError, match="couldn't find remote ref"):
        with PreparedWorkspace(str(clone), 999).open("0" * 40):
            pytest.fail("a missing head ref must not yield a workspace")


# --------------------------------------------------------------------------
# WorkspaceBoundReviewer
# --------------------------------------------------------------------------


class RecordingReviewer:
    def __init__(self):
        self.cwds = []

    def invoke(self, prompt, *, cwd=None):
        self.cwds.append(cwd)
        return ReviewerRun(stdout="ok")


def test_the_reviewer_runs_in_the_prepared_worktree(clone, origin):
    sha = publish_pull_request(origin, 16)
    inner = RecordingReviewer()

    run = WorkspaceBoundReviewer(inner, PreparedWorkspace(str(clone), 16)).invoke(
        "prompt", head_sha=sha
    )

    assert run.stdout == "ok"
    assert len(inner.cwds) == 1
    assert inner.cwds[0] != str(clone)


def test_no_reviewer_runs_when_the_workspace_cannot_be_bound(clone, origin):
    other = publish_pull_request(origin, 17)
    inner = RecordingReviewer()

    with pytest.raises(WorkspaceError):
        WorkspaceBoundReviewer(inner, ExistingWorkspace(str(clone))).invoke(
            "prompt", head_sha=other
        )

    assert inner.cwds == [], "the reviewer must not be started at all"
