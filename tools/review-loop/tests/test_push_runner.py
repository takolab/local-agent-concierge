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
        git_remote="origin",
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


def test_a_push_whose_readback_disagrees_is_reported_as_unknown(scenario, monkeypatch):
    """`git push` exits zero and the ref is not what we created."""
    from review_loop import push_runner

    def silent_push(worktree, *, remote, refspec, timeout=300.0):
        return None  # exits zero, moves nothing

    monkeypatch.setattr(push_runner, "push_fix_commit", silent_push)

    result = push(scenario)

    assert result.outcome is PushOutcome.PUSH_NOT_VERIFIED
    assert result.exit_code == 66
    assert result.repository_mutated is None
    assert result.commit_created is True
    assert "not known" in " ".join(result.reasons)


def test_a_push_that_lands_despite_a_reported_failure_is_believed(scenario, monkeypatch):
    """A lost response is not a failed push, and the remote settles it."""
    from review_loop import fix_commit, push_runner

    real = fix_commit.push_fix_commit

    def push_then_claim_failure(worktree, *, remote, refspec, timeout=300.0):
        real(worktree, remote=remote, refspec=refspec, timeout=timeout)
        raise fix_commit.PushRefused("the connection dropped before the answer arrived")

    monkeypatch.setattr(push_runner, "push_fix_commit", push_then_claim_failure)

    client = client_for(scenario)
    timeline = Timeline({1: green(scenario, client, sha_getter=scenario.remote_tip)})
    result = push(scenario, client=client, timeline=timeline)

    assert result.outcome is PushOutcome.PUSH_READY
    assert result.repository_mutated is True
    assert "the push landed" in " ".join(result.reasons)


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
