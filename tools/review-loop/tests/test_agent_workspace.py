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

from review_loop.agent_workspace import (
    inspect_workspace,
    is_residue,
    resolve_change_set,
)
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


# --------------------------------------------------------------------------
# The pull request's own change set
#
# This is the outer authority the coding agent's scope is bounded by, so the
# tests here are about what git actually reports -- including in the one shape
# where a plausible shortcut gets it wrong.
# --------------------------------------------------------------------------


@pytest.fixture
def diverged(tmp_path):
    """A clone whose ``origin/master`` is older than the pull request's base.

    The shape independent re-review of PR #34 named:

        B0  <- clone created here; local origin/master stays at B0
         |
        B1  <- base branch advances, changing comp_b
         |
         H  <- the pull request branches from B1, changing comp_a only

    The clone then fetches only ``refs/pull/5/head``, exactly as
    ``PreparedWorkspace`` does -- which does not advance ``origin/master``.
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--quiet", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet")
    for name in ("comp_a", "comp_b"):
        (seed / name).mkdir()
        (seed / name / "pyproject.toml").write_text(f"[project]\nname='{name}'\n")
        (seed / name / "mod.py").write_text("value = 0\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "B0")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")

    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(bare), str(clone))
    stale = git(clone, "rev-parse", "refs/remotes/origin/master")

    # The base branch advances, changing a component the pull request will not.
    (seed / "comp_b" / "mod.py").write_text("value = 1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "B1")
    git(seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")

    # The pull request branches from B1 and changes only comp_a.
    (seed / "comp_a" / "mod.py").write_text("value = 2\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "H")
    head = head_of(seed)
    git(seed, "push", "--quiet", "origin", "HEAD:refs/pull/5/head")

    # What a fix turn actually does: fetch the head ref, and nothing else.
    git(clone, "fetch", "--quiet", "origin", "refs/pull/5/head")
    return clone, head, stale


def test_the_change_set_is_what_the_pull_request_itself_changed(clone, origin):
    sha = publish_pull_request(origin, 31)
    git(clone, "fetch", "--quiet", "origin", f"refs/pull/31/head")

    changed = resolve_change_set(
        str(clone), head_sha=sha, base_ref="master", remote="origin"
    )

    assert changed == ("pkg/feature.py",)


def test_a_stale_local_base_ref_does_not_widen_the_change_set(diverged):
    """The finding: a cached origin/master can smuggle base-only changes in.

    With the stale ref, merge-base(B0, H) is B0 and the diff reports comp_b --
    a component this pull request never touched, which would then enter the
    scope boundary and be selectable by reviewer text.
    """
    clone, head, stale = diverged
    assert git(clone, "rev-parse", "refs/remotes/origin/master") == stale, (
        "the fixture must reproduce a stale remote-tracking ref"
    )

    changed = resolve_change_set(
        str(clone), head_sha=head, base_ref="master", remote="origin"
    )

    assert changed == ("comp_a/mod.py",)
    assert "comp_b/mod.py" not in changed, (
        "a base-only change must never be reported as this pull request's"
    )


def test_the_stale_ref_really_would_have_been_wrong(diverged):
    """Pins the bug itself, so the fixture cannot silently stop reproducing it.

    If this ever fails, the fixture no longer builds a diverged history and
    the test above has stopped proving anything.
    """
    clone, head, stale = diverged

    merge_base = git(clone, "merge-base", stale, head)
    naive = git(clone, "diff", "--name-only", merge_base, head).split()

    assert "comp_b/mod.py" in naive


def test_a_base_branch_that_cannot_be_fetched_fails_closed(clone, origin):
    sha = publish_pull_request(origin, 32)
    git(clone, "fetch", "--quiet", "origin", f"refs/pull/32/head")

    with pytest.raises(WorkspaceError, match="could not be fetched"):
        resolve_change_set(
            str(clone), head_sha=sha, base_ref="no-such-branch", remote="origin"
        )


def test_an_unreachable_remote_fails_closed_rather_than_using_a_local_ref(clone, origin):
    """Offline is a refusal, not a reason to trust the cache."""
    sha = publish_pull_request(origin, 33)
    git(clone, "fetch", "--quiet", "origin", f"refs/pull/33/head")

    with pytest.raises(WorkspaceError, match="could not be fetched"):
        resolve_change_set(
            str(clone), head_sha=sha, base_ref="master", remote="no-such-remote"
        )


def test_a_head_identical_to_its_base_has_no_change_set(clone, origin):
    """"I could not work out what changed" and "nothing changed" are different
    facts, and only one of them is a reason to route a fix."""
    base = head_of(clone)
    git(clone, "push", "--quiet", "origin", f"HEAD:refs/pull/34/head")

    with pytest.raises(WorkspaceError, match="changes no path"):
        resolve_change_set(
            str(clone), head_sha=base, base_ref="master", remote="origin"
        )


@pytest.fixture
def base_advanced(tmp_path):
    """A pull request whose base branch moved on *after* it was branched.

        B1 ----- B2      <- base advances after the review, changing comp_b
         \
          H            <- the pull request, changing comp_a only
    """
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--quiet", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet")
    for name in ("comp_a", "comp_b"):
        (seed / name).mkdir()
        (seed / name / "pyproject.toml").write_text(f"[project]\nname='{name}'\n")
        (seed / name / "mod.py").write_text("value = 0\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "B1")
    git(seed, "branch", "-M", "master")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "--quiet", "origin", "master:refs/heads/master")

    git(seed, "checkout", "--quiet", "-b", "pr")
    (seed / "comp_a" / "mod.py").write_text("value = 1\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "H")
    head = head_of(seed)
    git(seed, "push", "--quiet", "origin", "pr:refs/pull/9/head")

    git(seed, "checkout", "--quiet", "master")
    (seed / "comp_b" / "mod.py").write_text("value = 2\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "B2")
    git(seed, "push", "--quiet", "origin", "master:refs/heads/master")

    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(bare), str(clone))
    git(clone, "fetch", "--quiet", "origin", "refs/pull/9/head")
    return clone, head


def test_a_base_that_advanced_after_the_review_does_not_change_the_change_set(
    base_advanced,
):
    """Fetching the *current* base tip does not make the boundary drift.

    ``merge-base`` backs up to where this branch diverged, so a base that has
    moved on since contributes nothing. This is what lets the fix turn gate on
    head currency alone: the boundary describes the pull request, not the
    freshness of its merge context.
    """
    clone, head = base_advanced

    changed = resolve_change_set(
        str(clone), head_sha=head, base_ref="master", remote="origin"
    )

    assert changed == ("comp_a/mod.py",)
    assert "comp_b/mod.py" not in changed, "B2 is the base's change, not this PR's"


def test_the_change_set_is_the_same_before_and_after_the_base_moves(base_advanced):
    """The same pull request yields the same boundary whenever it is asked."""
    clone, head = base_advanced
    first = resolve_change_set(
        str(clone), head_sha=head, base_ref="master", remote="origin"
    )

    # Nothing about the pull request changed; only the base moved, which the
    # previous fetch already picked up.
    second = resolve_change_set(
        str(clone), head_sha=head, base_ref="master", remote="origin"
    )

    assert first == second == ("comp_a/mod.py",)
