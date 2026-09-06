"""The re-reviewer's working directory must be the pushed fix commit.

This drives real ``git`` against a repository in ``tmp_path``, for the reason
the review turn's workspace tests give: the failure being prevented is a
claim about what git actually checked out, so a faked git would prove
nothing. Here the specific failure is narrower and worse -- a re-reviewer
reading the *reviewed* commit rather than the fix would report every finding
unresolved, correctly, about the wrong tree.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from review_loop.rereview import ReReviewOutcome
from review_loop.rereview_input import load_request
from review_loop.rereview_runner import run_rereview
from review_loop.reviewer_process import ReviewerRun
from review_loop.reviewer_workspace import (
    ExistingWorkspace,
    PreparedWorkspace,
    WorkspaceBoundReviewer,
)

from fakes import (
    AUTOMATION_LOGIN,
    BASE_TIP,
    BASELINE_PATH,
    FILTERED_PATH,
    FakeCommentReader,
    FakeCommentWriter,
    FakeGitHubClient,
    pull_request_payload,
    run_payload,
)
from rereview_fakes import PR, push_document, rereview_text, resolution_block, review_document


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
def pushed_fix(tmp_path):
    """A pull request whose ``refs/pull/N/head`` is a fix on a reviewed head."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--quiet", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet")
    (seed / "README.md").write_text("first\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "first")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")

    (seed / "handler.py").write_text("def handle():\n    return 200\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "the reviewed head")
    reviewed = git(seed, "rev-parse", "HEAD")

    (seed / "handler.py").write_text("def handle():\n    raise RuntimeError('surfaced')\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "the fix")
    fix = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "--quiet", "origin", f"HEAD:refs/pull/{PR}/head")

    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(bare), str(clone))
    return clone, reviewed, fix


class RecordingReviewer:
    """Reports what its working directory actually is."""

    def __init__(self, fix_sha: str) -> None:
        self.fix_sha = fix_sha
        self.cwds: list[str] = []
        self.heads: list[str] = []
        self.contents: list[str] = []

    def invoke(self, prompt, *, cwd=None):
        self.cwds.append(cwd)
        self.heads.append(git(cwd, "rev-parse", "HEAD"))
        # Read inside the turn: the prepared worktree is gone afterwards.
        with open(os.path.join(cwd, "handler.py")) as handle:
            self.contents.append(handle.read())
        return ReviewerRun(
            stdout=rereview_text(
                head_sha=self.fix_sha,
                resolutions=(resolution_block("F1"), resolution_block("F2")),
            )
        )


def _client(fix_sha):
    return FakeGitHubClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=fix_sha)],
        runs=[
            run_payload(
                run_id=1, path=BASELINE_PATH, head_sha=fix_sha, pr_number=PR,
                merge_base=BASE_TIP,
            ),
            run_payload(
                run_id=2, workflow_id=347481064, path=FILTERED_PATH, head_sha=fix_sha,
                pr_number=PR, merge_base=BASE_TIP,
            ),
        ],
    )


def _request(reviewed, fix):
    return load_request(
        review_document(head_sha=reviewed),
        push_document(reviewed_sha=reviewed, pushed_sha=fix),
    )


def test_the_re_reviewer_reads_the_pushed_fix_not_the_reviewed_head(pushed_fix):
    clone, reviewed, fix = pushed_fix
    inner = RecordingReviewer(fix)
    writer = FakeCommentWriter()

    result = run_rereview(
        client=_client(fix),
        reader=FakeCommentReader(),
        writer=writer,
        reviewer=WorkspaceBoundReviewer(inner, PreparedWorkspace(str(clone), PR)),
        request=_request(reviewed, fix),
        expected_author=AUTOMATION_LOGIN,
    )

    assert result.outcome is ReReviewOutcome.RE_REVIEW_VALID
    # The invariant: the workspace HEAD is the pushed fix, and it is not the
    # commit the original findings were written against.
    assert inner.heads == [fix]
    assert reviewed not in inner.heads
    # And the tree really is the fixed one.
    assert "surfaced" in inner.contents[0]
    assert len(writer.posted) == 1


def test_the_prepared_worktree_is_removed_and_the_clone_untouched(pushed_fix):
    clone, reviewed, fix = pushed_fix
    inner = RecordingReviewer(fix)
    before = git(clone, "rev-parse", "HEAD")

    run_rereview(
        client=_client(fix),
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(),
        reviewer=WorkspaceBoundReviewer(inner, PreparedWorkspace(str(clone), PR)),
        request=_request(reviewed, fix),
        expected_author=AUTOMATION_LOGIN,
    )

    assert git(clone, "rev-parse", "HEAD") == before
    assert not os.path.exists(inner.cwds[0])


def test_an_explicit_workspace_at_the_reviewed_head_stops_the_turn(pushed_fix):
    """--reviewer-cwd is verified against the fix, not against the review."""
    clone, reviewed, fix = pushed_fix
    git(clone, "fetch", "--quiet", "origin", f"refs/pull/{PR}/head")
    git(clone, "checkout", "--quiet", "--detach", reviewed)
    inner = RecordingReviewer(fix)
    writer = FakeCommentWriter()

    result = run_rereview(
        client=_client(fix),
        reader=FakeCommentReader(),
        writer=writer,
        reviewer=WorkspaceBoundReviewer(inner, ExistingWorkspace(str(clone))),
        request=_request(reviewed, fix),
        expected_author=AUTOMATION_LOGIN,
    )

    assert result.outcome is ReReviewOutcome.REVIEWER_WORKSPACE_INVALID
    assert inner.cwds == [], "the re-reviewer must not be started at all"
    assert writer.posted == []


def test_an_explicit_workspace_at_the_fix_is_accepted(pushed_fix):
    clone, reviewed, fix = pushed_fix
    git(clone, "fetch", "--quiet", "origin", f"refs/pull/{PR}/head")
    git(clone, "checkout", "--quiet", "--detach", fix)
    inner = RecordingReviewer(fix)

    result = run_rereview(
        client=_client(fix),
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(),
        reviewer=WorkspaceBoundReviewer(inner, ExistingWorkspace(str(clone))),
        request=_request(reviewed, fix),
        expected_author=AUTOMATION_LOGIN,
    )

    assert result.outcome is ReReviewOutcome.RE_REVIEW_VALID
    assert inner.heads == [fix]
