"""What a Coding Agent left behind, established by real git.

These tests drive real ``git`` against repositories built in ``tmp_path``,
following PR #32's approach and for the same reason: the property under test
is a claim about what git actually reports, and a faked git would let the
claim be true of a git that does not exist.

Nothing here reaches the network. The "remote" is a bare repository on disk,
and the pull request head ref is pushed into it as ``refs/pull/N/head``, the
way GitHub exposes it.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from review_loop.agent_workspace import inspect_workspace, is_residue
from review_loop.reviewer_workspace import (
    ExistingWorkspace,
    PreparedWorkspace,
    WorkspaceError,
)

from fix_fakes import ScriptedAgent


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
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--quiet", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet")
    (seed / "README.md").write_text("first\n")
    (seed / ".gitignore").write_text(".env\ncredentials.json\n__pycache__/\n*.pyc\n")
    (seed / "pkg").mkdir()
    (seed / "pkg" / "pyproject.toml").write_text("[project]\nname='pkg'\n")
    (seed / "pkg" / "code.py").write_text("value = 1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "first")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")
    return bare, seed


@pytest.fixture
def clone(tmp_path, origin):
    bare, _ = origin
    path = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(bare), str(path))
    return path


def publish_pull_request(origin_fixture, number):
    bare, seed = origin_fixture
    (seed / "pkg" / "feature.py").write_text("feature = True\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", f"pr {number}")
    sha = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "--quiet", "origin", f"HEAD:refs/pull/{number}/head")
    return sha


def head_of(path):
    return git(path, "rev-parse", "HEAD")


# --------------------------------------------------------------------------
# Residue classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "__pycache__/mod.cpython-312.pyc",
        "pkg/tests/__pycache__/x.pyc",
        ".pytest_cache/v/cache/lastfailed",
        ".hypothesis/examples/abc",
        "pkg/thing.pyc",
        "src/pkg.egg-info/PKG-INFO",
        ".venv/lib/python3.12/site-packages/x.py",
        "node_modules/left-pad/index.js",
        ".coverage",
    ],
)
def test_build_and_test_output_is_residue(path):
    assert is_residue(path)


@pytest.mark.parametrize(
    "path", [".env", "credentials.json", "token.json", "pkg/secret.pem", "notes.txt"]
)
def test_anything_else_is_not_residue(path):
    assert not is_residue(path)


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------


def test_an_untouched_worktree_reports_no_change(clone):
    inspection = inspect_workspace(str(clone), target_head_sha=head_of(clone))

    assert inspection.changed_paths == ()
    assert inspection.clean
    assert inspection.patch == ""
    assert inspection.head_sha == head_of(clone)


def test_a_modified_tracked_file_is_reported(clone):
    (clone / "pkg" / "code.py").write_text("value = 2\n")

    inspection = inspect_workspace(str(clone), target_head_sha=head_of(clone))

    assert inspection.changed_paths == ("pkg/code.py",)
    assert "value = 2" in inspection.patch


def test_a_new_untracked_file_is_reported_and_appears_in_the_patch(clone):
    """A fix that adds a test file must not vanish from the diff."""
    (clone / "pkg" / "test_new.py").write_text("def test_x(): pass\n")

    inspection = inspect_workspace(str(clone), target_head_sha=head_of(clone))

    assert inspection.changed_paths == ("pkg/test_new.py",)
    assert "pkg/test_new.py" in inspection.patch
    assert "def test_x" in inspection.patch


def test_a_deleted_file_is_reported(clone):
    (clone / "pkg" / "code.py").unlink()

    inspection = inspect_workspace(str(clone), target_head_sha=head_of(clone))

    assert inspection.changed_paths == ("pkg/code.py",)


def test_a_path_with_a_space_is_reported_unquoted(clone):
    """`-z` is why: without it git would quote this and the set would differ."""
    (clone / "pkg" / "a file.py").write_text("x = 1\n")

    inspection = inspect_workspace(str(clone), target_head_sha=head_of(clone))

    assert inspection.changed_paths == ("pkg/a file.py",)


def test_build_residue_is_separated_from_the_change(clone):
    (clone / "pkg" / "code.py").write_text("value = 2\n")
    (clone / "pkg" / "__pycache__").mkdir()
    (clone / "pkg" / "__pycache__" / "code.pyc").write_text("bytecode\n")

    inspection = inspect_workspace(str(clone), target_head_sha=head_of(clone))

    assert inspection.changed_paths == ("pkg/code.py",)
    assert inspection.residue_paths == ("pkg/__pycache__/code.pyc",)
    assert inspection.unexpected_ignored == ()


def test_an_unexpected_ignored_file_is_separated_from_residue(clone):
    (clone / ".env").write_text("SECRET=1\n")

    inspection = inspect_workspace(str(clone), target_head_sha=head_of(clone))

    assert inspection.unexpected_ignored == (".env",)
    assert inspection.residue_paths == ()
    assert inspection.changed_paths == (), "an ignored file is not part of the fix"


def test_a_commit_made_in_the_worktree_is_visible_as_a_moved_head(clone):
    before = head_of(clone)
    (clone / "pkg" / "code.py").write_text("value = 2\n")
    git(clone, "add", "-A")
    git(clone, "commit", "--quiet", "-m", "the agent committed")

    inspection = inspect_workspace(str(clone), target_head_sha=before)

    assert inspection.head_sha != before


def test_inspecting_a_directory_that_is_not_a_work_tree_fails(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(WorkspaceError):
        inspect_workspace(str(plain), target_head_sha="0" * 40)


# --------------------------------------------------------------------------
# Inspection inside a prepared, writable worktree
# --------------------------------------------------------------------------


def test_an_agent_edits_only_the_worktree_the_runner_prepared(clone, origin):
    """The operator's own checkout is untouched by the agent's edits."""
    sha = publish_pull_request(origin, 21)
    before = head_of(clone)
    before_content = (clone / "pkg" / "code.py").read_text()
    seen = {}

    def edit(path):
        with open(os.path.join(path, "pkg", "code.py"), "w") as handle:
            handle.write("value = 99\n")
        seen["inspection"] = inspect_workspace(path, target_head_sha=sha)
        seen["path"] = path

    agent = ScriptedAgent(stdout="ok", edit=edit)
    with PreparedWorkspace(str(clone), 21, role="coding agent").open(sha) as worktree:
        agent.invoke("prompt", cwd=worktree)

    assert seen["path"] != str(clone)
    assert seen["inspection"].changed_paths == ("pkg/code.py",)
    assert head_of(clone) == before
    assert (clone / "pkg" / "code.py").read_text() == before_content


def test_the_prepared_worktree_is_removed_after_a_successful_fix(clone, origin):
    sha = publish_pull_request(origin, 22)
    created = None

    with PreparedWorkspace(str(clone), 22, role="coding agent").open(sha) as worktree:
        created = worktree
        with open(os.path.join(worktree, "pkg", "code.py"), "w") as handle:
            handle.write("value = 3\n")

    assert not os.path.isdir(created)
    assert created not in git(clone, "worktree", "list")


def test_the_prepared_worktree_is_removed_when_the_agent_raises(clone, origin):
    """A coding agent that explodes must not leave a dirty checkout behind."""
    sha = publish_pull_request(origin, 23)
    created = None

    with pytest.raises(RuntimeError):
        with PreparedWorkspace(str(clone), 23, role="coding agent").open(sha) as worktree:
            created = worktree
            with open(os.path.join(worktree, "pkg", "code.py"), "w") as handle:
                handle.write("half a fix\n")
            raise RuntimeError("the coding agent exploded")

    assert created is not None
    assert not os.path.isdir(created)


def test_a_prepared_worktree_starts_at_exactly_the_target_and_clean(clone, origin):
    sha = publish_pull_request(origin, 24)

    with PreparedWorkspace(str(clone), 24, role="coding agent").open(sha) as worktree:
        inspection = inspect_workspace(worktree, target_head_sha=sha)

        assert inspection.head_sha == sha
        assert inspection.changed_paths == ()
        assert inspection.ignored_paths == ()


def test_an_agent_workspace_at_the_wrong_commit_is_refused(clone, origin):
    other = publish_pull_request(origin, 25)
    agent = ScriptedAgent(stdout="ok")

    with pytest.raises(WorkspaceError, match="not the review target"):
        with ExistingWorkspace(str(clone), role="coding agent").open(other) as worktree:
            agent.invoke("prompt", cwd=worktree)

    assert agent.cwds == [], "the coding agent must not be started at all"


def test_a_dirty_agent_workspace_override_is_refused(clone):
    """A writable workspace holding someone else's edits means nothing after."""
    (clone / "pkg" / "code.py").write_text("someone else was here\n")

    with pytest.raises(WorkspaceError, match="uncommitted or untracked"):
        with ExistingWorkspace(str(clone), role="coding agent").open(head_of(clone)):
            pytest.fail("a dirty workspace must not yield")


def test_an_agent_workspace_override_with_an_ignored_file_is_refused(clone):
    (clone / ".env").write_text("SECRET=1\n")

    with pytest.raises(WorkspaceError, match="git-ignored path"):
        with ExistingWorkspace(str(clone), role="coding agent").open(head_of(clone)):
            pytest.fail("a workspace holding credentials must not yield")


def test_the_refusal_names_the_coding_agent_not_the_reviewer(clone):
    (clone / "scratch.txt").write_text("x\n")

    with pytest.raises(WorkspaceError, match="coding agent working directory"):
        with ExistingWorkspace(str(clone), role="coding agent").open(head_of(clone)):
            pytest.fail("must not yield")
