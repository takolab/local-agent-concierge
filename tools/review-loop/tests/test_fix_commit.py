"""What actually gets committed and pushed, established by real git.

Every assertion here is about what ``git`` reports, against repositories built
in ``tmp_path`` with a bare repository standing in for the remote. That is the
point: the claims this slice makes -- "the commit is exactly the candidate
patch", "the branch reads back as the commit we created" -- are claims about
git's behaviour, and a faked git would let them be true of a git that does not
exist.
"""

from __future__ import annotations

import os
import re

import pytest

from push_fakes import git
from review_loop.fix_commit import (
    CandidatePatchError,
    CommitRefused,
    PushRefused,
    apply_candidate_patch,
    create_fix_commit,
    describe_remote_commit,
    push_fix_commit,
    read_patch,
    read_remote_tip,
    require_clean_target,
)
from review_loop.patch_identity import capture_patch, patch_digest
from review_loop.reviewer_workspace import WorkspaceError, run_git

MESSAGE = "fix: address independent review finding(s) F1\n"


@pytest.fixture
def worktree(tmp_path, scenario):
    """A detached worktree at the reviewed head, as the runner prepares one."""
    path = tmp_path / "commit-tree"
    git(scenario.clone, "worktree", "add", "--detach", "--quiet", str(path), scenario.head_sha)
    yield str(path)
    git(scenario.clone, "worktree", "remove", "--force", str(path), check=False)


def lease_for(scenario):
    """The compare-and-swap condition every push in these tests carries."""
    return f"--force-with-lease=refs/heads/{scenario.branch}:{scenario.head_sha}"


def commit_the_patch(worktree, scenario):
    apply_candidate_patch(
        worktree,
        patch_path=scenario.patch_path,
        expected_digest=scenario.patch_sha256,
        expected_paths=scenario.changed_paths,
    )
    return create_fix_commit(
        worktree,
        message=MESSAGE,
        reviewed_head_sha=scenario.head_sha,
        expected_digest=scenario.patch_sha256,
        expected_paths=scenario.changed_paths,
    )


# --------------------------------------------------------------------------
# The candidate patch's identity, before anything is applied
# --------------------------------------------------------------------------


def test_the_candidate_patch_is_identified_by_its_bytes(scenario):
    data = read_patch(
        scenario.patch_path,
        expected_digest=scenario.patch_sha256,
        expected_bytes=scenario.patch_bytes,
    )

    assert data.decode("utf-8").startswith("diff --git")


def test_a_patch_that_hashes_to_something_else_is_refused(tmp_path, scenario):
    tampered = tmp_path / "tampered.patch"
    tampered.write_text(
        open(scenario.patch_path).read().replace("value = 2", "value = 999")
    )

    with pytest.raises(CandidatePatchError, match="not the validated candidate patch"):
        read_patch(
            str(tampered),
            expected_digest=scenario.patch_sha256,
            expected_bytes=scenario.patch_bytes,
        )


def test_a_patch_whose_size_disagrees_with_the_record_is_refused(scenario):
    with pytest.raises(CandidatePatchError, match="bytes but the fix turn recorded"):
        read_patch(
            scenario.patch_path,
            expected_digest=scenario.patch_sha256,
            expected_bytes=scenario.patch_bytes + 1,
        )


def test_a_missing_patch_file_is_refused(tmp_path, scenario):
    with pytest.raises(CandidatePatchError, match="could not be read"):
        read_patch(
            str(tmp_path / "absent.patch"),
            expected_digest=scenario.patch_sha256,
            expected_bytes=scenario.patch_bytes,
        )


def test_an_empty_patch_file_is_refused(tmp_path, scenario):
    empty = tmp_path / "empty.patch"
    empty.write_text("")

    with pytest.raises(CandidatePatchError, match="is empty"):
        read_patch(str(empty), expected_digest=scenario.patch_sha256, expected_bytes=0)


# --------------------------------------------------------------------------
# The workspace the commit is built in
# --------------------------------------------------------------------------


def test_a_workspace_at_another_commit_may_not_be_committed_in(worktree, scenario):
    git(worktree, "checkout", "--quiet", scenario.base_sha)

    with pytest.raises(CommitRefused, match="not the reviewed head"):
        require_clean_target(worktree, reviewed_head_sha=scenario.head_sha)


def test_an_unrelated_workspace_change_prevents_the_commit(worktree, scenario):
    (open(f"{worktree}/README.md", "a")).write("someone else was here\n")

    with pytest.raises(CommitRefused, match="unrelated change in the workspace"):
        require_clean_target(worktree, reviewed_head_sha=scenario.head_sha)


def test_an_unrelated_untracked_file_prevents_the_commit(worktree, scenario):
    open(f"{worktree}/scratch.txt", "w").write("notes\n")

    with pytest.raises(CommitRefused, match="uncommitted or untracked"):
        require_clean_target(worktree, reviewed_head_sha=scenario.head_sha)


def test_a_clean_reviewed_head_passes(worktree, scenario):
    require_clean_target(worktree, reviewed_head_sha=scenario.head_sha)


# --------------------------------------------------------------------------
# Applying the candidate patch
# --------------------------------------------------------------------------


def test_the_exact_candidate_patch_becomes_the_exact_working_tree(worktree, scenario):
    apply_candidate_patch(
        worktree,
        patch_path=scenario.patch_path,
        expected_digest=scenario.patch_sha256,
        expected_paths=scenario.changed_paths,
    )

    applied = capture_patch(worktree, "HEAD", timeout=60)
    assert patch_digest(applied) == scenario.patch_sha256
    assert open(f"{worktree}/pkg/code.py").read() == "value = 2\n"
    assert open(f"{worktree}/pkg/new.py").read() == "added = True\n"


def test_a_patch_that_does_not_apply_stops_before_any_commit(tmp_path, scenario, worktree):
    # A patch generated against a different content: it is byte-identical to
    # its own digest, so it passes identity and fails on application.
    (tmp_path / "other").mkdir()
    foreign = tmp_path / "foreign.patch"
    foreign.write_text(
        "diff --git a/pkg/code.py b/pkg/code.py\n"
        "index 0000000000000000000000000000000000000000..1111111111111111111111111111111111111111 100644\n"
        "--- a/pkg/code.py\n"
        "+++ b/pkg/code.py\n"
        "@@ -1 +1 @@\n"
        "-value = 77\n"
        "+value = 78\n"
    )

    with pytest.raises(CandidatePatchError, match="does not apply"):
        apply_candidate_patch(
            worktree,
            patch_path=str(foreign),
            expected_digest=patch_digest(foreign.read_text()),
            expected_paths=("pkg/code.py",),
        )

    assert run_git(["rev-parse", "HEAD"], cwd=worktree, timeout=60) == scenario.head_sha


def test_an_extra_edit_alongside_the_patch_fails_the_identity_check(worktree, scenario):
    run_git(
        ["apply", "--index", "--whitespace=nowarn", "--", scenario.patch_path],
        cwd=worktree,
        timeout=60,
    )
    open(f"{worktree}/README.md", "a").write("and one more thing\n")

    applied = capture_patch(worktree, "HEAD", timeout=60)
    assert patch_digest(applied) != scenario.patch_sha256


def test_a_changed_path_set_that_differs_is_refused(worktree, scenario):
    with pytest.raises(CandidatePatchError, match="does not match the change"):
        apply_candidate_patch(
            worktree,
            patch_path=scenario.patch_path,
            expected_digest=scenario.patch_sha256,
            expected_paths=("pkg/code.py",),
        )


def test_a_git_ignored_credential_left_by_the_patch_is_refused(worktree, scenario):
    apply_first = run_git(
        ["apply", "--index", "--whitespace=nowarn", "--", scenario.patch_path],
        cwd=worktree,
        timeout=60,
    )
    run_git(["checkout", "--", "."], cwd=worktree, timeout=60)
    run_git(["reset", "--quiet", "--hard", scenario.head_sha], cwd=worktree, timeout=60)
    open(f"{worktree}/.env", "w").write("TOKEN=secret\n")

    with pytest.raises(CandidatePatchError, match="git-ignored"):
        apply_candidate_patch(
            worktree,
            patch_path=scenario.patch_path,
            expected_digest=scenario.patch_sha256,
            expected_paths=scenario.changed_paths,
        )


# --------------------------------------------------------------------------
# The commit
# --------------------------------------------------------------------------


def test_the_commit_is_the_candidate_patch_on_the_reviewed_head(worktree, scenario):
    commit = commit_the_patch(worktree, scenario)

    assert commit.parent_sha == scenario.head_sha
    assert commit.patch_sha256 == scenario.patch_sha256
    assert commit.changed_paths == scenario.changed_paths
    assert run_git(["rev-parse", "HEAD"], cwd=worktree, timeout=60) == commit.sha
    # And the commit's own diff, read back from git, is the candidate patch.
    committed = capture_patch(worktree, commit.parent_sha, commit.sha, timeout=60)
    assert patch_digest(committed) == scenario.patch_sha256


def test_the_commit_leaves_nothing_behind_in_the_working_tree(worktree, scenario):
    commit_the_patch(worktree, scenario)

    assert run_git(
        ["status", "--porcelain", "--untracked-files=all"], cwd=worktree, timeout=60
    ) == ""


def test_a_commit_on_the_wrong_parent_is_refused(worktree, scenario):
    git(worktree, "checkout", "--quiet", scenario.base_sha)
    run_git(
        ["apply", "--index", "--whitespace=nowarn", "--", scenario.patch_path],
        cwd=worktree,
        timeout=60,
    )

    with pytest.raises(CommitRefused, match="not of the reviewed head"):
        create_fix_commit(
            worktree,
            message=MESSAGE,
            reviewed_head_sha=scenario.head_sha,
            expected_digest=scenario.patch_sha256,
            expected_paths=scenario.changed_paths,
        )


def test_a_commit_containing_more_than_the_candidate_patch_is_refused(worktree, scenario):
    run_git(
        ["apply", "--index", "--whitespace=nowarn", "--", scenario.patch_path],
        cwd=worktree,
        timeout=60,
    )
    open(f"{worktree}/README.md", "a").write("smuggled\n")
    run_git(["add", "-A"], cwd=worktree, timeout=60)

    with pytest.raises(CommitRefused, match="not the change that was validated"):
        create_fix_commit(
            worktree,
            message=MESSAGE,
            reviewed_head_sha=scenario.head_sha,
            expected_digest=scenario.patch_sha256,
            expected_paths=scenario.changed_paths,
        )


def test_a_commit_that_leaves_part_of_the_change_unstaged_is_refused(worktree, scenario):
    run_git(
        ["apply", "--index", "--whitespace=nowarn", "--", scenario.patch_path],
        cwd=worktree,
        timeout=60,
    )
    # Unstage one half, so `git commit` would record only the other.
    run_git(["reset", "--quiet", "HEAD", "--", "pkg/new.py"], cwd=worktree, timeout=60)

    with pytest.raises(CommitRefused):
        create_fix_commit(
            worktree,
            message=MESSAGE,
            reviewed_head_sha=scenario.head_sha,
            expected_digest=scenario.patch_sha256,
            expected_paths=scenario.changed_paths,
        )


# --------------------------------------------------------------------------
# The push, and reading the remote back
# --------------------------------------------------------------------------


def test_the_pushed_ref_reads_back_as_the_created_commit(worktree, scenario):
    commit = commit_the_patch(worktree, scenario)

    push_fix_commit(
        worktree,
        remote="origin",
        refspec=f"{commit.sha}:refs/heads/{scenario.branch}",
        lease=lease_for(scenario),
    )

    assert (
        read_remote_tip(str(scenario.clone), remote="origin", branch=scenario.branch)
        == commit.sha
    )
    assert scenario.remote_tip() == commit.sha


def test_a_push_the_remote_rejects_leaves_the_branch_where_it_was(worktree, scenario):
    commit = commit_the_patch(worktree, scenario)
    # Someone else moves the branch first, so the fast-forward no longer holds.
    git(scenario.seed, "commit", "--quiet", "--allow-empty", "-m", "theirs")
    theirs = git(scenario.seed, "rev-parse", "HEAD")
    git(scenario.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{scenario.branch}")

    with pytest.raises(PushRefused):
        push_fix_commit(
            worktree,
            remote="origin",
            refspec=f"{commit.sha}:refs/heads/{scenario.branch}",
            lease=lease_for(scenario),
        )

    assert scenario.remote_tip() == theirs


def test_the_push_argument_vector_carries_no_force_and_no_tag(monkeypatch, scenario):
    """The refusal above is the property; this is the argv that produces it."""
    from review_loop import fix_commit

    from review_loop.reviewer_workspace import GitResult

    seen = {}
    refspec = f"{scenario.head_sha}:refs/heads/{scenario.branch}"

    def record(argv, *, cwd, timeout):
        seen["argv"] = list(argv)
        return GitResult(returncode=0, stdout=f"To x\n\t{refspec}\told..new\nDone\n", stderr="")

    monkeypatch.setattr(fix_commit, "run_git_capture", record)
    push_fix_commit(
        str(scenario.clone),
        remote="origin",
        refspec=refspec,
        lease=lease_for(scenario),
    )

    argv = seen["argv"]
    assert argv == [
        "push",
        "--porcelain",
        # The two expansions git configuration can apply to a one-refspec
        # push, refused explicitly rather than hoped against.
        "--no-follow-tags",
        "--recurse-submodules=no",
        lease_for(scenario),
        "--",
        "origin",
        refspec,
    ]
    # The lease is a compare-and-swap on an exact old value, never a bare
    # force: it names the derived ref and the exact reviewed head.
    assert re.fullmatch(
        r"--force-with-lease=refs/heads/[^:]+:[0-9a-f]{40}", lease_for(scenario)
    )
    assert "--force" not in argv
    assert "--force-with-lease" not in argv
    assert not any(word.startswith("+") for word in argv)
    assert not any("refs/tags" in word for word in argv)


def test_a_branch_the_remote_does_not_have_reads_back_as_absent(scenario):
    assert (
        read_remote_tip(str(scenario.clone), remote="origin", branch="no/such/branch")
        is None
    )


def test_an_unreachable_remote_is_an_error_not_an_absent_branch(tmp_path, scenario):
    with pytest.raises(WorkspaceError):
        read_remote_tip(
            str(scenario.clone),
            remote=str(tmp_path / "not-a-remote"),
            branch=scenario.branch,
        )


# --------------------------------------------------------------------------
# Identifying a commit someone already pushed
# --------------------------------------------------------------------------


def test_a_pushed_fix_is_identified_by_its_parent_and_its_diff(worktree, scenario):
    commit = commit_the_patch(worktree, scenario)
    push_fix_commit(
        worktree,
        remote="origin",
        refspec=f"{commit.sha}:refs/heads/{scenario.branch}",
        lease=lease_for(scenario),
    )

    parent, digest = describe_remote_commit(
        str(scenario.clone), remote="origin", branch=scenario.branch, tip=commit.sha
    )

    assert parent == scenario.head_sha
    assert digest == scenario.patch_sha256


def test_someone_elses_commit_is_not_identified_as_this_fix(scenario):
    git(scenario.seed, "commit", "--quiet", "--allow-empty", "-m", "unrelated")
    theirs = git(scenario.seed, "rev-parse", "HEAD")
    git(scenario.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{scenario.branch}")

    parent, digest = describe_remote_commit(
        str(scenario.clone), remote="origin", branch=scenario.branch, tip=theirs
    )

    assert parent == scenario.head_sha
    assert digest != scenario.patch_sha256


# --------------------------------------------------------------------------
# The digest is a property of the change, not of the repository
# --------------------------------------------------------------------------


def test_local_diff_configuration_cannot_change_the_patch_identity(worktree, scenario):
    """A digest that moved with `git config` would compare two different things.

    Every setting below changes the bytes of an ordinary `git diff` without
    changing what the diff says. The canonical argument vector pins each one,
    so the identity survives an operator's own configuration -- and survives
    the fix turn and the push turn running on different machines.
    """
    before = capture_patch(worktree, "HEAD", timeout=60)
    run_git(
        ["apply", "--index", "--whitespace=nowarn", "--", scenario.patch_path],
        cwd=worktree,
        timeout=60,
    )
    plain = capture_patch(worktree, "HEAD", timeout=60)

    for setting, value in (
        ("core.abbrev", "4"),
        ("diff.noprefix", "true"),
        ("diff.mnemonicPrefix", "true"),
        ("diff.algorithm", "patience"),
        ("diff.context", "9"),
    ):
        run_git(["config", setting, value], cwd=worktree, timeout=60)

    assert before == ""
    assert capture_patch(worktree, "HEAD", timeout=60) == plain
    assert patch_digest(plain) == scenario.patch_sha256


def test_the_same_change_hashes_the_same_in_a_second_repository(tmp_path, scenario, worktree):
    """The push turn's repository is not the fix turn's, and must agree with it."""
    commit = commit_the_patch(worktree, scenario)
    push_fix_commit(
        worktree,
        remote="origin",
        refspec=f"{commit.sha}:refs/heads/{scenario.branch}",
        lease=lease_for(scenario),
    )

    second = tmp_path / "second-clone"
    git(tmp_path, "clone", "--quiet", str(scenario.origin), str(second))
    # A repository with a different object count, which is what would change
    # an unpinned abbreviation length.
    for index in range(20):
        git(second, "commit", "--quiet", "--allow-empty", "-m", f"noise {index}")

    elsewhere = capture_patch(str(second), commit.parent_sha, commit.sha, timeout=60)

    assert patch_digest(elsewhere) == scenario.patch_sha256


def test_a_captured_patch_ends_with_a_newline_and_applies(worktree, scenario):
    """A stripped trailing newline makes `git apply` reject the patch outright."""
    text = open(scenario.patch_path).read()

    assert text.endswith("\n")
    apply_candidate_patch(
        worktree,
        patch_path=scenario.patch_path,
        expected_digest=scenario.patch_sha256,
        expected_paths=scenario.changed_paths,
    )


def test_a_non_ascii_path_hashes_the_same_whatever_quotepath_says(worktree):
    """`core.quotePath` renders a non-ASCII path literally or octal-escaped.

    Two machines that merely disagree about that setting would compute two
    digests for one change, and the second would refuse a patch the first
    validated -- a fail-closed availability bug rather than a write-safety
    one, but a real one for anyone whose fix touches such a file.
    """
    path = os.path.join(worktree, "pkg", "ünïcode-ファイル.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("value = 1\n")
    run_git(["add", "--intent-to-add", "--", "pkg/ünïcode-ファイル.py"], cwd=worktree, timeout=60)

    run_git(["config", "core.quotePath", "true"], cwd=worktree, timeout=60)
    quoted = capture_patch(worktree, "HEAD", timeout=60)
    run_git(["config", "core.quotePath", "false"], cwd=worktree, timeout=60)
    literal = capture_patch(worktree, "HEAD", timeout=60)

    assert quoted == literal
    assert "ünïcode-ファイル.py" in literal
    assert patch_digest(quoted) == patch_digest(literal)


def test_a_rename_hashes_the_same_whatever_rename_detection_says(worktree):
    """The same tree transition renders two ways depending on `diff.renames`.

    Rename detection also depends on `diff.renameLimit`, and therefore on how
    many files the change happened to touch -- so leaving it unpinned makes
    the digest a function of the size of the change as well as its content.
    """
    run_git(["mv", "pkg/code.py", "pkg/moved.py"], cwd=worktree, timeout=60)

    run_git(["config", "diff.renames", "true"], cwd=worktree, timeout=60)
    detected = capture_patch(worktree, "HEAD", timeout=60)
    run_git(["config", "diff.renames", "false"], cwd=worktree, timeout=60)
    plain = capture_patch(worktree, "HEAD", timeout=60)

    assert detected == plain
    assert "rename from" not in detected
    assert patch_digest(detected) == patch_digest(plain)


def test_the_pinned_settings_are_the_ones_the_module_claims(worktree, scenario):
    """Every setting named in the docstring is actually neutralised."""
    run_git(
        ["apply", "--index", "--whitespace=nowarn", "--", scenario.patch_path],
        cwd=worktree,
        timeout=60,
    )
    baseline = capture_patch(worktree, "HEAD", timeout=60)

    for setting, value in (
        ("core.abbrev", "4"),
        ("core.quotePath", "true"),
        ("diff.noprefix", "true"),
        ("diff.mnemonicPrefix", "true"),
        ("diff.algorithm", "patience"),
        ("diff.context", "9"),
        ("diff.indentHeuristic", "false"),
        ("diff.renames", "copies"),
        ("diff.suppressBlankEmpty", "true"),
        ("diff.interHunkContext", "7"),
        ("diff.relative", "true"),
    ):
        run_git(["config", setting, value], cwd=worktree, timeout=60)
        assert capture_patch(worktree, "HEAD", timeout=60) == baseline, setting

    assert patch_digest(baseline) == scenario.patch_sha256


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("To /x\n!\tSPEC\t[rejected] (non-fast-forward)\nDone\n", "rejected"),
        ("To /x\n!\tSPEC\t[remote rejected] (pre-receive hook)\nDone\n", "rejected"),
        ("To /x\n\tSPEC\tabc..def\nDone\n", "accepted"),
        ("To /x\n*\tSPEC\t[new branch]\nDone\n", "accepted"),
        ("To /x\n=\tSPEC\t[up to date]\nDone\n", "up_to_date"),
        # No line for our ref at all: a local hook refusal and a lost response
        # look exactly like this, and they are not the same fact.
        ("", "silent"),
        ("To /x\n!\tother:refs/heads/x\t[rejected]\nDone\n", "silent"),
        ("garbage\n", "silent"),
    ],
)
def test_the_remotes_own_answer_is_read_from_the_porcelain_report(stdout, expected):
    from review_loop.fix_commit import read_push_report

    spec = "abc123:refs/heads/feat"
    assert read_push_report(stdout.replace("SPEC", spec), refspec=spec) == expected


def test_a_real_rejected_push_carries_the_remotes_answer(worktree, scenario):
    """End to end, against a real remote that really refuses the update."""
    from review_loop.fix_commit import REMOTE_REJECTED

    commit = commit_the_patch(worktree, scenario)
    git(scenario.seed, "commit", "--quiet", "--allow-empty", "-m", "theirs")
    git(scenario.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{scenario.branch}")

    with pytest.raises(PushRefused) as error:
        push_fix_commit(
            worktree,
            remote="origin",
            refspec=f"{commit.sha}:refs/heads/{scenario.branch}",
            lease=lease_for(scenario),
        )

    assert error.value.report == REMOTE_REJECTED


def test_a_real_accepted_push_carries_the_remotes_answer(worktree, scenario):
    from review_loop.fix_commit import REMOTE_ACCEPTED

    commit = commit_the_patch(worktree, scenario)

    attempt = push_fix_commit(
        worktree,
        remote="origin",
        refspec=f"{commit.sha}:refs/heads/{scenario.branch}",
        lease=lease_for(scenario),
    )

    assert attempt.report == REMOTE_ACCEPTED
    assert attempt.unexpected_refs == ()
    assert scenario.remote_tip() == commit.sha


def test_every_push_url_is_read_not_only_the_first(tmp_path, scenario):
    """`--all`, because `git push` writes to every configured push URL."""
    from review_loop.fix_commit import read_remote_urls

    other = tmp_path / "someone" / "other-repo.git"
    other.mkdir(parents=True)
    git(other, "init", "--quiet", "--bare")
    git(scenario.clone, "remote", "set-url", "--add", "--push", "origin", str(scenario.origin))
    git(scenario.clone, "remote", "set-url", "--add", "--push", "origin", str(other))

    urls = read_remote_urls(str(scenario.clone), remote="origin")

    assert str(other) in urls
    assert str(scenario.origin) in urls
