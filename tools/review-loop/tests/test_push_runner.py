"""One push turn end to end: real git, a scripted GitHub, a scripted clock.

The runner is where the slice's promises are actually kept, so these tests are
organised around them rather than around the code:

* what is on the branch afterwards, and whether the runner says so correctly;
* that nothing is on the branch after every pre-write refusal;
* that CI evidence belongs to the exact pushed commit and to nothing else;
* that a retry re-derives its state from git and GitHub rather than repeating
  a push it has no record of.
"""

from __future__ import annotations

import os

import pytest

from conftest import build_scenario
from fakes import ADVANCED_BASE_TIP, BASE_TIP, OTHER_SHA
from push_fakes import PushGitHubClient, Timeline, fix_json, git
from review_loop.fix_handoff import load_handoff
from review_loop.push_response import PushOutcome
from review_loop.push_runner import run_push
from review_loop.reviewer_workspace import PreparedWorkspace, WorkspaceError

PUSH_ROLE = "fix commit"


def handoff_for(scenario, **overrides):
    kwargs = {
        "head_sha": scenario.head_sha,
        "changed_paths": scenario.changed_paths,
        "patch_sha256": scenario.patch_sha256,
        "patch_bytes": scenario.patch_bytes,
        "patch_path": scenario.patch_path,
        "number": scenario.number,
    }
    kwargs.update(overrides)
    return load_handoff(fix_json(**kwargs))


def workspace_for(scenario):
    return PreparedWorkspace(
        str(scenario.clone), scenario.number, remote="origin", role=PUSH_ROLE
    )


def client_for(scenario, **overrides):
    kwargs = {
        "number": scenario.number,
        "head_sha": scenario.head_sha,
        "branch": scenario.branch,
        "ci": {},
    }
    kwargs.update(overrides)
    return PushGitHubClient(**kwargs)


def push(scenario, *, client=None, timeline=None, workspace=None, **kwargs):
    timeline = timeline or Timeline()
    return run_push(
        client=client if client is not None else client_for(scenario),
        workspace=workspace if workspace is not None else workspace_for(scenario),
        repo_root=str(scenario.clone),
        handoff=kwargs.pop("handoff", None) or handoff_for(scenario),
        patch_path=kwargs.pop("patch_path", None) or scenario.patch_path,
        git_remote=kwargs.pop("git_remote", "origin"),
        clock=timeline.clock,
        sleep=timeline.sleep,
        **kwargs,
    )


def green(scenario, client, *, sha_getter):
    """A sleep that makes GitHub catch up with the push, then go green."""

    def step():
        sha = sha_getter()
        client.head_sha = sha
        client.ci[sha] = "success"

    return step


# --------------------------------------------------------------------------
# The success path
# --------------------------------------------------------------------------


def test_the_candidate_patch_becomes_a_pushed_commit_with_green_ci(scenario):
    client = client_for(scenario)
    timeline = Timeline({1: lambda: None})

    # GitHub catches up on the first sleep: the head becomes whatever is on
    # the branch, and CI for it succeeds.
    timeline.steps[1] = green(scenario, client, sha_getter=scenario.remote_tip)

    result = push(scenario, client=client, timeline=timeline)

    assert result.outcome is PushOutcome.PUSH_READY
    assert result.exit_code == 0
    assert result.repository_mutated is True
    assert result.push_performed is True
    assert result.already_pushed is False
    assert result.pushed_sha == scenario.remote_tip()
    assert result.commit is not None
    assert result.commit.parent_sha == scenario.head_sha
    assert result.commit.patch_sha256 == scenario.patch_sha256
    assert result.commit.changed_paths == scenario.changed_paths
    assert result.verified_target is not None
    assert result.verified_target.head_sha == result.pushed_sha
    assert result.verified_target.ci_merge_base_sha == BASE_TIP


def test_the_pushed_commit_contains_only_the_candidate_patch(scenario):
    client = client_for(scenario)
    timeline = Timeline({1: green(scenario, client, sha_getter=scenario.remote_tip)})

    result = push(scenario, client=client, timeline=timeline)

    pushed = result.pushed_sha
    names = git(scenario.clone, "diff", "--name-only", f"{pushed}^", pushed).split()
    assert sorted(names) == sorted(scenario.changed_paths)
    assert git(scenario.clone, "rev-list", "--count", f"{scenario.head_sha}..{pushed}") == "1"


def test_the_commit_message_carries_no_agent_text(scenario):
    client = client_for(scenario)
    timeline = Timeline({1: green(scenario, client, sha_getter=scenario.remote_tip)})

    result = push(scenario, client=client, timeline=timeline)

    body = git(scenario.clone, "log", "-1", "--format=%B", result.pushed_sha)
    assert "F1" in body
    assert scenario.patch_sha256 in body
    # The agent's own prose from the fix response never reaches history.
    assert "made the change" not in body
    assert "693 passed" not in body


def test_a_dry_run_verifies_the_patch_and_writes_nothing(scenario):
    result = push(scenario, dry_run=True)

    assert result.outcome is PushOutcome.PUSH_PREPARED
    assert result.exit_code == 0
    assert result.repository_mutated is False
    assert result.commit is None
    assert result.pushed_sha is None
    assert scenario.remote_tip() == scenario.head_sha


# --------------------------------------------------------------------------
# Refusals before any write
# --------------------------------------------------------------------------


def assert_branch_untouched(scenario, result):
    assert result.repository_mutated is False
    assert result.pushed_sha is None
    assert scenario.remote_tip() == scenario.head_sha


def test_a_patch_that_is_not_the_candidate_patch_never_reaches_git(tmp_path, scenario):
    tampered = tmp_path / "tampered.patch"
    tampered.write_text(open(scenario.patch_path).read().replace("value = 2", "value = 3"))

    result = push(scenario, patch_path=str(tampered))

    assert result.outcome is PushOutcome.PATCH_IDENTITY_MISMATCH
    assert result.exit_code == 63
    assert_branch_untouched(scenario, result)


def test_a_fork_head_is_refused_before_anything_is_prepared(scenario):
    result = push(scenario, client=client_for(scenario, head_repo="someone/fork"))

    assert result.outcome is PushOutcome.PUSH_BRANCH_REFUSED
    assert result.exit_code == 61
    assert_branch_untouched(scenario, result)


def test_a_head_branch_that_is_the_default_branch_is_refused(tmp_path):
    scenario = build_scenario(tmp_path, _touch, branch="release")
    client = client_for(scenario, branch="release", default_branch="release")
    client.base_ref = "master"

    result = push(scenario, client=client)

    assert result.outcome is PushOutcome.PUSH_BRANCH_REFUSED
    assert "default branch" in " ".join(result.reasons)
    assert_branch_untouched(scenario, result)


def test_a_closed_pull_request_is_refused(scenario):
    result = push(scenario, client=client_for(scenario, state="closed"))

    assert result.outcome is PushOutcome.PUSH_BRANCH_REFUSED
    assert_branch_untouched(scenario, result)


def test_a_pull_request_whose_head_moved_stops_before_the_commit(scenario):
    git(scenario.seed, "commit", "--quiet", "--allow-empty", "-m", "someone else")
    moved = git(scenario.seed, "rev-parse", "HEAD")
    git(scenario.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{scenario.branch}")

    result = push(scenario, client=client_for(scenario, head_sha=moved))

    assert result.outcome is PushOutcome.PUSH_TARGET_STALE
    assert result.exit_code == 62
    assert result.repository_mutated is False
    assert result.pushed_sha is None
    assert scenario.remote_tip() == moved


def test_a_base_branch_change_since_the_fix_stops_the_run(scenario):
    result = push(scenario, client=client_for(scenario, base_ref="release"))

    assert result.outcome is PushOutcome.PUSH_TARGET_STALE
    assert "release" in " ".join(result.reasons)
    assert_branch_untouched(scenario, result)


def test_a_github_failure_before_the_push_writes_nothing(scenario):
    from review_loop.github_client import GitHubApiError

    result = push(scenario, client=client_for(scenario, error=GitHubApiError("HTTP 502")))

    assert result.outcome is PushOutcome.PUSH_API_ERROR
    assert result.exit_code == 72
    assert_branch_untouched(scenario, result)


def test_an_unrelated_workspace_change_prevents_the_commit(scenario, tmp_path):
    """`--commit-cwd`'s failure mode: a directory that is not only the target."""
    from review_loop.reviewer_workspace import ExistingWorkspace

    checkout = tmp_path / "operator-checkout"
    git(scenario.clone, "worktree", "add", "--detach", "--quiet", str(checkout), scenario.head_sha)
    open(checkout / "README.md", "a").write("my own edit\n")

    result = push(
        scenario, workspace=ExistingWorkspace(str(checkout), role=PUSH_ROLE)
    )

    # The workspace verification refuses a dirty tree before the runner's own
    # check is reached; both are refusals with nothing written.
    assert result.outcome in (
        PushOutcome.PUSH_WORKSPACE_INVALID,
        PushOutcome.COMMIT_REFUSED,
    )
    assert_branch_untouched(scenario, result)


def test_a_git_failure_inside_the_prepared_workspace_refuses_the_commit(
    scenario, monkeypatch
):
    """Already prepared and verified, so a git failure here is not the workspace.

    Both are no-write outcomes; the difference is which one an operator goes
    and looks at, and "the workspace could not be prepared" would send them to
    the wrong place.
    """
    from review_loop import push_runner

    def broken(worktree, *, reviewed_head_sha, timeout=300.0):
        raise WorkspaceError("git status could not be run")

    monkeypatch.setattr(push_runner, "require_clean_target", broken)

    result = push(scenario)

    assert result.outcome is PushOutcome.COMMIT_REFUSED
    assert result.exit_code == 64
    assert_branch_untouched(scenario, result)


def test_a_git_failure_while_applying_is_not_a_patch_identity_problem(
    scenario, monkeypatch
):
    from review_loop import push_runner

    def broken(worktree, **kwargs):
        raise WorkspaceError("git apply could not be run")

    monkeypatch.setattr(push_runner, "apply_candidate_patch", broken)

    result = push(scenario)

    assert result.outcome is PushOutcome.COMMIT_REFUSED
    assert "could not be applied" in " ".join(result.reasons)
    assert_branch_untouched(scenario, result)


def test_a_workspace_that_cannot_be_prepared_writes_nothing(scenario):
    class Broken:
        def open(self, head_sha):
            raise WorkspaceError("the worktree could not be created")

        def describe(self):
            return "(broken)"

    result = push(scenario, workspace=Broken())

    assert result.outcome is PushOutcome.PUSH_WORKSPACE_INVALID
    assert result.exit_code == 71
    assert_branch_untouched(scenario, result)


# --------------------------------------------------------------------------
# Push verification
# --------------------------------------------------------------------------


def test_a_non_fast_forward_push_is_refused_and_reported_as_no_write(scenario):
    """The branch moves under the run, after the pre-flight read."""

    class MovingWorkspace:
        """Advances the remote branch just before the commit is pushed."""

        def __init__(self, inner):
            self._inner = inner

        def open(self, head_sha):
            from contextlib import contextmanager

            @contextmanager
            def _open():
                with self._inner.open(head_sha) as path:
                    git(scenario.seed, "commit", "--quiet", "--allow-empty", "-m", "theirs")
                    git(
                        scenario.seed,
                        "push",
                        "--quiet",
                        "origin",
                        f"HEAD:refs/heads/{scenario.branch}",
                    )
                    yield path

            return _open()

        def describe(self):
            return "(moving)"

    theirs_before = scenario.remote_tip()
    result = push(scenario, workspace=MovingWorkspace(workspace_for(scenario)))

    assert result.outcome is PushOutcome.PUSH_FAILED
    assert result.exit_code == 65
    assert result.repository_mutated is False
    assert result.commit_created is True
    assert result.pushed_sha is None
    # The branch moved -- but to their commit, not to ours, and the runner
    # says so rather than reporting an unknown state.
    assert scenario.remote_tip() != theirs_before
    assert result.commit.sha != scenario.remote_tip()
    # And the no-write claim rests on the remote's own answer: a real
    # `git push --porcelain` against a real remote produced a per-ref
    # rejection line, which is the only evidence that establishes it.
    assert "rejected" in " ".join(result.reasons)


def test_a_push_whose_readback_disagrees_is_reported_as_unknown(scenario, monkeypatch):
    """`git push` exits zero and the ref is not what we created."""
    from review_loop import push_runner

    def silent_push(worktree, *, remote, refspec, lease, timeout=300.0):
        return None  # exits zero, moves nothing

    monkeypatch.setattr(push_runner, "push_fix_commit", silent_push)

    result = push(scenario)

    assert result.outcome is PushOutcome.PUSH_NOT_VERIFIED
    assert result.exit_code == 66
    assert result.repository_mutated is None
    assert result.commit_created is True
    assert "not known" in " ".join(result.reasons)


def test_a_push_that_lands_despite_a_reported_failure_is_believed(scenario, monkeypatch):
    """A lost response is not a failed push, and the read-back settles it.

    What the read-back settles is *what is on the branch*, not *who put it
    there*. With no per-ref answer from the remote, "this run pushed it" is
    not established -- so the fix is reported as present and the attribution
    is not claimed.
    """
    from review_loop import fix_commit, push_runner

    real = fix_commit.push_fix_commit

    def push_then_claim_failure(worktree, *, remote, refspec, lease, timeout=300.0):
        real(worktree, remote=remote, refspec=refspec, lease=lease, timeout=timeout)
        raise fix_commit.PushRefused("the connection dropped before the answer arrived")

    monkeypatch.setattr(push_runner, "push_fix_commit", push_then_claim_failure)

    client = client_for(scenario)
    timeline = Timeline({1: green(scenario, client, sha_getter=scenario.remote_tip)})
    result = push(scenario, client=client, timeline=timeline)

    assert result.outcome is PushOutcome.PUSH_READY
    assert result.repository_mutated is True
    assert result.pushed_sha == scenario.remote_tip()
    # The branch holds the fix; the runner does not claim to know it moved it.
    assert result.push_performed is False
    reasons = " ".join(result.reasons)
    assert "gave no answer establishing that this run's push is what put it there" in reasons
    assert "git push reported a failure" in reasons


# --------------------------------------------------------------------------
# Idempotency and retry
# --------------------------------------------------------------------------


def test_a_retry_after_a_successful_push_creates_no_second_commit(scenario):
    client = client_for(scenario)
    first = push(
        scenario,
        client=client,
        timeline=Timeline({1: green(scenario, client, sha_getter=scenario.remote_tip)}),
    )
    assert first.outcome is PushOutcome.PUSH_READY
    pushed = first.pushed_sha

    second = push(scenario, client=client, timeline=Timeline())

    assert second.outcome is PushOutcome.PUSH_READY
    assert second.pushed_sha == pushed
    assert second.already_pushed is True
    assert second.push_performed is False
    assert second.commit is None
    assert second.repository_mutated is True
    assert scenario.remote_tip() == pushed
    assert git(scenario.clone, "rev-list", "--count", f"{scenario.head_sha}..{pushed}") == "1"


def test_a_retry_after_a_push_whose_ci_was_never_observed_resumes_at_ci(scenario):
    """The first run pushes and times out waiting; the second finds the state."""
    client = client_for(scenario)
    first = push(scenario, client=client, ci_timeout=0.0)

    assert first.outcome in (PushOutcome.CI_PENDING, PushOutcome.CI_AMBIGUOUS)
    assert first.repository_mutated is True
    pushed = first.pushed_sha

    client.head_sha = pushed
    client.ci[pushed] = "success"
    second = push(scenario, client=client, timeline=Timeline())

    assert second.outcome is PushOutcome.PUSH_READY
    assert second.already_pushed is True
    assert second.push_performed is False
    assert second.pushed_sha == pushed


def test_a_branch_holding_something_else_is_not_mistaken_for_this_fix(scenario):
    git(scenario.seed, "commit", "--quiet", "--allow-empty", "-m", "unrelated")
    theirs = git(scenario.seed, "rev-parse", "HEAD")
    git(scenario.seed, "push", "--quiet", "origin", f"HEAD:refs/heads/{scenario.branch}")

    result = push(scenario, client=client_for(scenario, head_sha=theirs))

    assert result.outcome is PushOutcome.PUSH_TARGET_STALE
    assert result.repository_mutated is False
    assert scenario.remote_tip() == theirs


def test_an_already_pushed_fix_with_a_disagreeing_pull_request_head_stops(scenario):
    client = client_for(scenario)
    first = push(
        scenario,
        client=client,
        timeline=Timeline({1: green(scenario, client, sha_getter=scenario.remote_tip)}),
    )
    assert first.outcome is PushOutcome.PUSH_READY

    client.head_sha = OTHER_SHA
    result = push(scenario, client=client, timeline=Timeline())

    assert result.outcome is PushOutcome.PUSH_TARGET_STALE
    assert "disagree" in " ".join(result.reasons)


# --------------------------------------------------------------------------
# Authoritative CI, bound to the exact pushed commit
# --------------------------------------------------------------------------


def pushed_then(scenario, *, ci_for_pushed, head_after_push=None, ci_timeout=600.0,
                extra_steps=None, **client_overrides):
    """Push, then let GitHub report the given state on the first sleep."""
    client = client_for(scenario, **client_overrides)

    def step():
        pushed = scenario.remote_tip()
        client.head_sha = head_after_push or pushed
        if ci_for_pushed is not None:
            client.ci[pushed] = ci_for_pushed
        if extra_steps is not None:
            extra_steps(client)

    return client, push(
        scenario,
        client=client,
        timeline=Timeline({1: step}),
        ci_timeout=ci_timeout,
    )


def test_ci_from_the_reviewed_head_is_never_accepted_for_the_pushed_commit(scenario):
    """The reviewed head is green; the pushed commit has no runs at all.

    A green reviewed head is exactly the stale evidence this stage must not
    accept, and the outcome is not READY: the pushed commit's own baseline
    workflow has no run, which is unexplained absence and therefore
    ambiguous rather than pending.
    """
    client, result = pushed_then(
        scenario,
        ci_for_pushed=None,
        ci={scenario.head_sha: "success"},
        ci_timeout=40.0,
    )

    assert result.outcome is PushOutcome.CI_AMBIGUOUS
    assert result.repository_mutated is True
    assert result.ci_evaluation is not None
    # Whatever it concluded, it concluded it about the pushed commit.
    assert result.ci_evaluation.target.head_sha == result.pushed_sha
    assert result.ci_evaluation.target.head_sha != scenario.head_sha


def test_a_failing_ci_for_the_pushed_commit_is_reported_as_such(scenario):
    _, result = pushed_then(scenario, ci_for_pushed="failure")

    assert result.outcome is PushOutcome.CI_FAILED
    assert result.exit_code == 67
    assert result.repository_mutated is True
    assert result.pushed_sha is not None


def test_ci_that_never_finishes_ends_the_bounded_wait_as_pending(scenario):
    _, result = pushed_then(scenario, ci_for_pushed="pending", ci_timeout=45.0)

    assert result.outcome is PushOutcome.CI_PENDING
    assert result.exit_code == 68
    assert result.repository_mutated is True


def test_another_commit_pushed_on_top_makes_the_ci_evidence_stale(scenario):
    _, result = pushed_then(
        scenario, ci_for_pushed="success", head_after_push=OTHER_SHA
    )

    assert result.outcome is PushOutcome.CI_STALE_TARGET
    assert result.exit_code == 69
    assert result.repository_mutated is True


def test_a_base_branch_that_advanced_after_ci_is_a_stale_merge_context(scenario):
    """Green CI against a merge that no longer exists is not ready for re-review."""
    _, result = pushed_then(
        scenario,
        ci_for_pushed="success",
        base_tip=ADVANCED_BASE_TIP,
        merge_base=BASE_TIP,
    )

    assert result.outcome is PushOutcome.CI_STALE_TARGET
    assert result.verified_target is None


def test_an_undecidable_ci_state_fails_closed(scenario):
    """A successful run that belongs to another pull request decides nothing."""
    _, result = pushed_then(
        scenario, ci_for_pushed="success", run_pr_number=scenario.number + 1
    )

    assert result.outcome is PushOutcome.CI_AMBIGUOUS
    assert result.exit_code == 70
    assert result.repository_mutated is True
    assert result.verified_target is None
    assert any("not #" in reason for reason in result.reasons)


def test_a_pull_request_head_that_never_becomes_the_pushed_commit_is_ambiguous(scenario):
    _, result = pushed_then(
        scenario, ci_for_pushed="success", head_after_push=None, ci_timeout=0.0
    )

    # With a zero timeout the very first observation still happens, and it sees
    # the pull request at the reviewed head rather than at the pushed commit.
    assert result.outcome is PushOutcome.CI_AMBIGUOUS
    assert result.exit_code == 70
    assert result.repository_mutated is True


def test_a_persistent_github_failure_after_the_push_keeps_the_push_reported(scenario):
    from review_loop.github_client import GitHubApiError

    client = client_for(scenario)

    def fail_from_now_on():
        client.error = GitHubApiError("HTTP 502")

    result = push(
        scenario,
        client=client,
        timeline=Timeline({1: lambda: fail_from_now_on()}),
        ci_timeout=600.0,
    )

    assert result.outcome is PushOutcome.CI_API_ERROR
    assert result.exit_code == 73
    assert result.repository_mutated is True
    assert result.pushed_sha == scenario.remote_tip()


def _touch(worktree):
    (worktree / "pkg" / "code.py").write_text("value = 2\n")
    (worktree / "pkg" / "new.py").write_text("added = True\n")


# --------------------------------------------------------------------------
# Regressions from PR #35's independent review
# --------------------------------------------------------------------------


def test_a_relative_patch_path_works_with_the_prepared_worktree(scenario, monkeypatch):
    """The documented flow, exactly as an operator types it.

    `read_patch` runs in the operator's directory and `git apply` runs in a
    temporary worktree, so a relative `--patch fix.patch` passed the identity
    check and then failed to open -- and was reported as though the patch did
    not apply to the reviewed commit.
    """
    monkeypatch.chdir(scenario.clone)
    os.replace(scenario.patch_path, str(scenario.clone / "fix.patch"))

    client = client_for(scenario)
    timeline = Timeline({1: green(scenario, client, sha_getter=scenario.remote_tip)})
    result = push(
        scenario,
        client=client,
        timeline=timeline,
        handoff=handoff_for(scenario, patch_path="fix.patch"),
        patch_path="fix.patch",
    )

    assert result.outcome is PushOutcome.PUSH_READY
    assert result.pushed_sha == scenario.remote_tip()
    # And the report names the file that was actually read, absolutely.
    assert result.patch_path == str(scenario.clone / "fix.patch")
    assert os.path.isabs(result.patch_path)


def test_the_resolved_patch_path_is_reported_on_a_failure_too(scenario, monkeypatch):
    monkeypatch.chdir(scenario.clone)
    (scenario.clone / "wrong.patch").write_text("not the candidate patch\n")

    result = push(scenario, patch_path="wrong.patch")

    assert result.outcome is PushOutcome.PATCH_IDENTITY_MISMATCH
    assert result.patch_path == str(scenario.clone / "wrong.patch")


def test_a_remote_naming_another_repository_is_refused_before_any_push(
    tmp_path, scenario
):
    """A second remote with the same branch at the same commit is not this one.

    The branch name came from GitHub, but the *repository* came from
    `--git-remote`. Without binding the two, a mirror holding the same history
    would be pushed to while every message said "this repository".
    """
    mirror = tmp_path / "someone" / "mirror.git"
    mirror.mkdir(parents=True)
    git(mirror, "init", "--quiet", "--bare")
    git(scenario.seed, "remote", "add", "mirror", str(mirror))
    git(scenario.seed, "push", "--quiet", "mirror", f"HEAD:refs/heads/{scenario.branch}")
    git(scenario.clone, "remote", "add", "mirror", str(mirror))

    # The mirror really does hold the same branch at the same commit.
    assert scenario.remote_tip() == scenario.head_sha
    tip = git(scenario.clone, "ls-remote", "mirror", f"refs/heads/{scenario.branch}")
    assert tip.split("\t")[0] == scenario.head_sha

    result = push(scenario, git_remote="mirror")

    assert result.outcome is PushOutcome.PUSH_BRANCH_REFUSED
    assert result.exit_code == 61
    assert result.repository_mutated is False
    assert "someone/mirror" in " ".join(result.reasons)
    # Nothing reached either repository.
    assert scenario.remote_tip() == scenario.head_sha
    assert (
        git(scenario.clone, "ls-remote", "mirror", f"refs/heads/{scenario.branch}")
        .split("\t")[0]
        == scenario.head_sha
    )


def test_a_remote_on_another_forge_is_refused(scenario):
    git(
        scenario.clone,
        "remote",
        "add",
        "elsewhere",
        "https://gitlab.com/takolab/local-agent-concierge.git",
    )

    result = push(scenario, git_remote="elsewhere")

    assert result.outcome is PushOutcome.PUSH_BRANCH_REFUSED
    assert "host is not GitHub" in " ".join(result.reasons)
    assert scenario.remote_tip() == scenario.head_sha


def test_a_remote_that_does_not_exist_is_refused(scenario):
    result = push(scenario, git_remote="no-such-remote")

    assert result.outcome is PushOutcome.PUSH_BRANCH_REFUSED
    assert result.repository_mutated is False
    assert scenario.remote_tip() == scenario.head_sha


def test_a_landed_push_followed_by_a_concurrent_child_is_not_a_no_write(
    scenario, monkeypatch
):
    """The lost-response case, with one extra concurrent event.

    push accepted -> response lost -> someone pushes a child on top ->
    read-back sees the child. Reporting that as a verified no-write would send
    an operator to re-run a push whose commit is already in the branch.
    """
    from review_loop import fix_commit, push_runner

    real = fix_commit.push_fix_commit
    landed = {}

    def push_then_lose_the_answer(worktree, *, remote, refspec, lease, timeout=300.0):
        real(worktree, remote=remote, refspec=refspec, lease=lease, timeout=timeout)
        landed["sha"] = refspec.split(":")[0]
        git(scenario.seed, "fetch", "--quiet", "origin", f"refs/heads/{scenario.branch}")
        git(scenario.seed, "checkout", "--quiet", "FETCH_HEAD")
        git(scenario.seed, "commit", "--quiet", "--allow-empty", "-m", "theirs on top")
        git(
            scenario.seed,
            "push",
            "--quiet",
            "origin",
            f"HEAD:refs/heads/{scenario.branch}",
        )
        raise fix_commit.PushRefused("the connection dropped before the answer arrived")

    monkeypatch.setattr(push_runner, "push_fix_commit", push_then_lose_the_answer)

    result = push(scenario)

    assert result.outcome is PushOutcome.CI_STALE_TARGET
    assert result.repository_mutated is True
    assert result.push_performed is True
    assert result.pushed_sha == landed["sha"]
    assert "DID land" in " ".join(result.reasons)
    # And it really is in the branch's history, which is what was asserted.
    assert (
        git(
            scenario.clone,
            "rev-list",
            "--max-count=1",
            landed["sha"],
            f"^{scenario.remote_tip()}",
        )
        == ""
    )


def test_an_unanswerable_ancestry_question_stays_unknown(scenario, monkeypatch):
    """`landed is None` must not be rounded to either certainty."""
    from review_loop import fix_commit, push_runner

    # Exits zero but the remote gave no per-ref answer, and git cannot say
    # whether the commit reached it. Both unknowns, and the result stays one.
    monkeypatch.setattr(
        push_runner, "push_fix_commit", lambda *a, **k: fix_commit.REMOTE_SILENT
    )
    monkeypatch.setattr(push_runner, "contains_commit", lambda *a, **k: None)

    result = push(scenario)

    assert result.outcome is PushOutcome.PUSH_NOT_VERIFIED
    assert result.repository_mutated is None
    assert "could not be determined" in " ".join(result.reasons)


# --------------------------------------------------------------------------
# Regressions from PR #35's second review round
# --------------------------------------------------------------------------


def test_a_second_push_url_naming_another_repository_is_refused(tmp_path, scenario):
    """`git push` writes to EVERY push URL; the check must read every one.

    `git remote get-url --push` without `--all` reports only the first, so a
    remote configured with two push URLs passed the earlier check while the
    push itself reached both repositories.
    """
    other = tmp_path / "someone" / "other-repo.git"
    other.mkdir(parents=True)
    git(other, "init", "--quiet", "--bare")
    git(scenario.clone, "remote", "set-url", "--add", "--push", "origin", str(scenario.origin))
    git(scenario.clone, "remote", "set-url", "--add", "--push", "origin", str(other))

    # The fixture really does configure two push URLs, only one of which the
    # un-`--all` form would have reported.
    assert len(git(scenario.clone, "remote", "get-url", "--push", "--all", "origin").splitlines()) == 2

    result = push(scenario)

    assert result.outcome is PushOutcome.PUSH_BRANCH_REFUSED
    assert result.repository_mutated is False
    assert "someone/other-repo" in " ".join(result.reasons)
    # Neither repository was written to.
    assert scenario.remote_tip() == scenario.head_sha
    assert git(other, "for-each-ref", "--format=%(refname)") == ""


def test_a_pull_request_in_another_repository_is_refused(scenario):
    """A cross-repository pull request whose head happens to live here.

    `head.repo` alone does not establish that this is a pull request *in* the
    target repository, and the branch named in one that is not is not a branch
    this fix may be pushed to.
    """
    client = client_for(scenario)
    payload = client.get_pull_request(scenario.number)
    payload["base"]["repo"]["full_name"] = "someone/other-repo"
    client.get_pull_request = lambda number: payload

    result = push(scenario, client=client)

    assert result.outcome is PushOutcome.PUSH_BRANCH_REFUSED
    assert "someone/other-repo" in " ".join(result.reasons)
    assert scenario.remote_tip() == scenario.head_sha


def test_a_silent_push_failure_is_unknown_not_a_verified_no_write(scenario, monkeypatch):
    """A local hook refusal and a lost response look identical afterwards.

    Neither produces a per-ref answer from the remote, and an absent commit
    does not prove no-write -- a commit can land and then be erased. So the
    honest report is "unknown", not `repository_mutated: false`.
    """
    from review_loop import fix_commit, push_runner

    def silent_failure(worktree, *, remote, refspec, lease, timeout=300.0):
        raise fix_commit.PushRefused(
            "git push failed: hook says no", fix_commit.REMOTE_SILENT
        )

    monkeypatch.setattr(push_runner, "push_fix_commit", silent_failure)

    result = push(scenario)

    assert result.outcome is PushOutcome.PUSH_NOT_VERIFIED
    assert result.exit_code == 66
    assert result.repository_mutated is None
    assert "no per-ref answer" in " ".join(result.reasons)
    # Nothing actually reached the remote in this fixture, but the runner does
    # not claim to know that -- which is the point.
    assert scenario.remote_tip() == scenario.head_sha


def test_a_remote_rejection_is_reported_as_a_verified_no_write(scenario, monkeypatch):
    from review_loop import fix_commit, push_runner

    def rejected(worktree, *, remote, refspec, lease, timeout=300.0):
        raise fix_commit.PushRefused(
            "git push failed: non-fast-forward", fix_commit.REMOTE_REJECTED
        )

    monkeypatch.setattr(push_runner, "push_fix_commit", rejected)

    result = push(scenario)

    assert result.outcome is PushOutcome.PUSH_FAILED
    assert result.exit_code == 65
    assert result.repository_mutated is False
    assert "rejected" in " ".join(result.reasons)


def test_a_remote_that_accepted_a_since_rewritten_ref_is_reported_as_pushed(
    scenario, monkeypatch
):
    """Accepted by the remote, then erased from the branch: mutated, absent."""
    from review_loop import fix_commit, push_runner

    def accepted_then_gone(worktree, *, remote, refspec, lease, timeout=300.0):
        return fix_commit.REMOTE_ACCEPTED

    monkeypatch.setattr(push_runner, "push_fix_commit", accepted_then_gone)

    result = push(scenario)

    assert result.outcome is PushOutcome.CI_STALE_TARGET
    assert result.repository_mutated is True
    assert result.push_performed is True
    assert "rewritten" in " ".join(result.reasons)


# --------------------------------------------------------------------------
# Regressions from PR #35's third review round
# --------------------------------------------------------------------------


class MovesTheBranchDuringTheRun:
    """Moves the remote branch after the pre-flight read, before the push.

    This is the TOCTOU window itself, reproduced: the runner has already read
    the branch and decided it is at the reviewed head when the move happens.
    """

    def __init__(self, inner, act):
        self._inner = inner
        self._act = act

    def open(self, head_sha):
        from contextlib import contextmanager

        @contextmanager
        def _open():
            with self._inner.open(head_sha) as path:
                self._act()
                yield path

        return _open()

    def describe(self):
        return "(moves the branch mid-run)"


def test_a_branch_deleted_after_the_preflight_is_not_recreated(scenario):
    """A plain push recreates a deleted branch; the lease refuses to.

    "Never creates a branch that does not exist" is one of this command's
    stated guarantees, and without a compare-and-swap on the exact expected
    old value it was not true between the read and the write.
    """

    def delete_the_branch():
        git(scenario.seed, "push", "--quiet", "origin", "--delete", scenario.branch)

    result = push(
        scenario,
        workspace=MovesTheBranchDuringTheRun(
            workspace_for(scenario), delete_the_branch
        ),
    )

    assert result.outcome is PushOutcome.PUSH_FAILED
    assert result.repository_mutated is False
    # The branch is still gone: it was not recreated.
    assert scenario.remote_tip() == ""


def test_a_branch_rewound_after_the_preflight_is_not_written_over(scenario):
    """A rewind to an ancestor still fast-forwards, so only a lease stops it.

    `A -> C` is a fast-forward from git's point of view even though the ref is
    no longer at the `H` the fix was authorised against.
    """
    ancestor = git(scenario.clone, "rev-parse", f"{scenario.head_sha}^")

    def rewind_the_branch():
        git(
            scenario.seed,
            "push",
            "--quiet",
            "--force",
            "origin",
            f"{ancestor}:refs/heads/{scenario.branch}",
        )

    result = push(
        scenario,
        workspace=MovesTheBranchDuringTheRun(
            workspace_for(scenario), rewind_the_branch
        ),
    )

    assert result.outcome is PushOutcome.PUSH_FAILED
    assert result.repository_mutated is False
    # The branch is still where the other actor put it; our commit is not there.
    assert scenario.remote_tip() == ancestor


def test_a_transient_remote_failure_is_not_a_verified_no_write(scenario, monkeypatch):
    """`!` means "rejected OR failed to push", and only the first is evidence.

    `[remote failure]` is a server-side error whose outcome is not
    established, so it must never become `repository_mutated: false`.
    """
    from review_loop import fix_commit, push_runner

    def remote_failure(worktree, *, remote, refspec, lease, timeout=300.0):
        raise fix_commit.PushRefused(
            "git push failed: remote end hung up",
            fix_commit.read_push_report(
                f"To x\n!\t{refspec}\t[remote failure]\nDone\n", refspec=refspec
            ),
        )

    monkeypatch.setattr(push_runner, "push_fix_commit", remote_failure)

    result = push(scenario)

    assert result.outcome is PushOutcome.PUSH_NOT_VERIFIED
    assert result.repository_mutated is None


def test_a_push_that_lands_then_times_out_is_reported_as_pushed(scenario, monkeypatch):
    """A timeout after the process started cannot be a pre-write failure.

    The remote may already have applied the update, and the answer may be the
    only thing that went missing. Reporting this as a workspace problem lost
    both the mutation and the commit record.
    """
    from review_loop import fix_commit, push_runner
    from review_loop.reviewer_workspace import GitTimeoutError

    real = fix_commit.push_fix_commit
    landed = {}

    def push_then_time_out(worktree, *, remote, refspec, lease, timeout=300.0):
        real(worktree, remote=remote, refspec=refspec, lease=lease, timeout=timeout)
        landed["sha"] = refspec.split(":")[0]
        raise GitTimeoutError("git push timed out after 300s")

    monkeypatch.setattr(push_runner, "push_fix_commit", push_then_time_out)

    client = client_for(scenario)
    timeline = Timeline({1: green(scenario, client, sha_getter=scenario.remote_tip)})
    result = push(scenario, client=client, timeline=timeline)

    assert result.outcome is PushOutcome.PUSH_READY
    assert result.repository_mutated is True
    assert result.pushed_sha == landed["sha"] == scenario.remote_tip()
    assert result.commit_created is True


def test_a_timeout_reaches_the_push_path_as_an_unknown_answer(scenario, monkeypatch):
    """The translation itself: a timeout is a push attempt, not a bad workspace."""
    from review_loop import fix_commit
    from review_loop.reviewer_workspace import GitTimeoutError

    def time_out(argv, *, cwd, timeout):
        raise GitTimeoutError("git push timed out after 300s")

    monkeypatch.setattr(fix_commit, "run_git_capture", time_out)

    with pytest.raises(fix_commit.PushRefused) as error:
        fix_commit.push_fix_commit(
            str(scenario.clone),
            remote="origin",
            refspec=f"{scenario.head_sha}:refs/heads/{scenario.branch}",
            lease=f"--force-with-lease=refs/heads/{scenario.branch}:{scenario.head_sha}",
        )

    assert error.value.report == fix_commit.REMOTE_SILENT
    assert "timed out" in str(error.value)


def test_an_up_to_date_ref_is_not_attributed_to_this_run(scenario, monkeypatch):
    """`= [up to date]` means the push moved nothing, whoever put it there."""
    from review_loop import fix_commit, push_runner

    real = fix_commit.push_fix_commit

    def push_then_report_up_to_date(worktree, *, remote, refspec, lease, timeout=300.0):
        real(worktree, remote=remote, refspec=refspec, lease=lease, timeout=timeout)
        return fix_commit.REMOTE_UP_TO_DATE

    monkeypatch.setattr(push_runner, "push_fix_commit", push_then_report_up_to_date)

    client = client_for(scenario)
    timeline = Timeline({1: green(scenario, client, sha_getter=scenario.remote_tip)})
    result = push(scenario, client=client, timeline=timeline)

    assert result.outcome is PushOutcome.PUSH_READY
    assert result.repository_mutated is True
    assert result.push_performed is False
    assert result.already_pushed is True
    assert "already held this exact fix" in " ".join(result.reasons)
