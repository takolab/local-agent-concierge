"""One bounded fix turn, end to end.

The workspace is real: a real clone, a real ``refs/pull/N/head``, a real
detached worktree, and an agent that really edits files in it. Only the agent
itself is scripted, because what it *says* is the thing being validated.

Two properties are asserted over and over, in different failure modes,
because they are the ones the whole slice rests on:

* **the worktree is removed on every path**, so a failed turn leaves nothing
  behind and a successful one leaves only the captured patch;
* **the operator's own checkout is never touched**, whatever the agent does.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from review_loop.fix_response import FixOutcome, FixRunOutcome
from review_loop.fix_runner import run_fix
from review_loop.reviewer_workspace import PreparedWorkspace, WorkspaceError
from review_loop.verdict import Recommendation, Severity

from fix_fakes import OTHER_SHA, ScriptedAgent, finding, response_text, verdict
from fix_fakes import target as make_target

SOURCE = "pkg/code.py"
NEW_TEST = "pkg/test_new.py"
OUTSIDE = "other/thing.py"


def git(cwd, *argv):
    return subprocess.run(
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
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A clone, plus a published pull request head, plus its exact SHA."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--quiet", "--bare")

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "--quiet")
    (seed / ".gitignore").write_text(".env\n__pycache__/\n*.pyc\n")
    (seed / "pkg").mkdir()
    (seed / "pkg" / "pyproject.toml").write_text("[project]\nname='pkg'\n")
    (seed / "pkg" / "code.py").write_text("value = 1\n")
    (seed / "other").mkdir()
    (seed / "other" / "pyproject.toml").write_text("[project]\nname='other'\n")
    (seed / "other" / "thing.py").write_text("unrelated = True\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "first")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "--quiet", "origin", "HEAD:refs/heads/master")

    (seed / "pkg" / "code.py").write_text("value = 1\nlimit = None\n")
    git(seed, "add", "-A")
    git(seed, "commit", "--quiet", "-m", "pr 29")
    sha = git(seed, "rev-parse", "HEAD")
    git(seed, "push", "--quiet", "origin", "HEAD:refs/pull/29/head")

    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--quiet", str(bare), str(clone))
    return clone, sha


@pytest.fixture
def workspace(repo):
    clone, _ = repo
    return PreparedWorkspace(str(clone), 29, role="coding agent")


def routed_finding(finding_id="F1", **kwargs):
    kwargs.setdefault("location", "pkg/code.py:2")
    return finding(finding_id, **kwargs)


def edit(*paths, content="value = 2\n"):
    def apply(worktree):
        for path in paths:
            full = os.path.join(worktree, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(content)

    return apply


def run(workspace, agent, *findings, sha, **kwargs):
    return run_fix(
        agent=agent,
        workspace=workspace,
        target=make_target(sha),
        verdict=verdict(*findings, head_sha=sha),
        **kwargs,
    )


# --------------------------------------------------------------------------
# Gating: what never reaches an agent
# --------------------------------------------------------------------------


def test_an_approved_review_never_invokes_the_agent(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(stdout="unused")

    result = run(workspace, agent, sha=sha)

    assert result.outcome is FixRunOutcome.NO_ACTIONABLE_FINDINGS
    assert result.exit_code == 0
    assert not result.agent_invoked
    assert agent.prompts == []


def test_a_blocking_finding_never_invokes_the_agent(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(stdout="unused")

    result = run_fix(
        agent=agent,
        workspace=workspace,
        target=make_target(sha),
        verdict=verdict(
            routed_finding(severity=Severity.BLOCKING),
            head_sha=sha,
            recommendation=Recommendation.ESCALATE,
        ),
    )

    assert result.outcome is FixRunOutcome.REVIEW_REQUIRES_HUMAN
    assert result.exit_code == 40
    assert agent.prompts == []


def test_an_escalating_review_never_invokes_the_agent(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(stdout="unused")

    result = run_fix(
        agent=agent,
        workspace=workspace,
        target=make_target(sha),
        verdict=verdict(
            routed_finding(), head_sha=sha, recommendation=Recommendation.ESCALATE
        ),
    )

    assert result.outcome is FixRunOutcome.REVIEW_REQUIRES_HUMAN
    assert agent.prompts == []


def test_too_many_findings_never_invoke_the_agent(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(stdout="unused")
    many = [routed_finding(f"F{n}") for n in range(1, 5)]

    result = run(workspace, agent, *many, sha=sha, max_findings=2)

    assert result.outcome is FixRunOutcome.REVIEW_REQUIRES_HUMAN
    assert agent.prompts == []


def test_a_finding_reaching_outside_the_pull_requests_change_set_is_refused(
    workspace, repo
):
    """Reviewer prose may select inside what the PR changed, never beyond it.

    ``other/`` exists at the target but was introduced by the base commit, so
    this pull request never touched it. Routing a finding that cites it would
    let reviewer-written text hand the agent a component the change set gives
    it no claim to.
    """
    _, sha = repo
    agent = ScriptedAgent(stdout="unused")

    result = run(
        workspace, agent, routed_finding(location="other/thing.py:1"), sha=sha
    )

    assert result.outcome is FixRunOutcome.REVIEW_REQUIRES_HUMAN
    assert "lies outside what this pull request changed" in result.reasons[0]
    assert agent.prompts == []


def test_the_routed_scope_never_exceeds_the_change_set(workspace, repo):
    _, sha = repo
    seen = {}

    def record(worktree):
        edit(SOURCE)(worktree)

    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=record
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    boundary = {entry.path for entry in result.request.change_set_boundary}
    assert boundary == {"pkg"}, "only the component this pull request changed"
    assert not result.request.permits(OUTSIDE)


def test_a_finding_whose_scope_cannot_be_bounded_goes_to_a_human(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(stdout="unused")

    result = run(
        workspace, agent, routed_finding(location="somewhere in the CLI"), sha=sha
    )

    assert result.outcome is FixRunOutcome.REVIEW_REQUIRES_HUMAN
    assert "cannot be bounded" in result.reasons[0]
    assert agent.prompts == []


def test_a_dry_run_creates_no_workspace_and_invokes_nothing(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(stdout="unused")

    result = run(workspace, agent, routed_finding(), sha=sha, dry_run=True)

    assert result.outcome is FixRunOutcome.ROUTING_PREPARED
    assert result.exit_code == 0
    assert not result.workspace_created
    assert not result.agent_invoked
    assert agent.prompts == []


# --------------------------------------------------------------------------
# The successful turn
# --------------------------------------------------------------------------


def test_a_bounded_fix_is_applied_and_captured(workspace, repo):
    clone, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=edit(SOURCE)
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_APPLIED
    assert result.exit_code == 0
    assert result.validated.responses[0].outcome is FixOutcome.FIXED
    assert result.inspection.changed_paths == (SOURCE,)
    assert "value = 2" in result.patch


def test_the_agent_ran_in_a_worktree_at_the_exact_target(workspace, repo):
    clone, sha = repo
    seen = {}

    def record(worktree):
        seen["head"] = git(worktree, "rev-parse", "HEAD")
        seen["path"] = worktree
        edit(SOURCE)(worktree)

    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=record
    )

    run(workspace, agent, routed_finding(), sha=sha)

    assert seen["head"] == sha
    assert seen["path"] != str(clone)


def test_the_invoking_checkout_is_untouched(workspace, repo):
    """Whatever the agent does, the operator's own tree stays as it was."""
    clone, sha = repo
    before_head = git(clone, "rev-parse", "HEAD")
    before_status = git(clone, "status", "--porcelain")
    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=edit(SOURCE)
    )

    run(workspace, agent, routed_finding(), sha=sha)

    assert git(clone, "rev-parse", "HEAD") == before_head
    assert git(clone, "status", "--porcelain") == before_status


def test_the_worktree_is_removed_after_a_successful_fix(workspace, repo):
    clone, sha = repo
    seen = {}

    def record(worktree):
        seen["path"] = worktree
        edit(SOURCE)(worktree)

    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=record
    )

    run(workspace, agent, routed_finding(), sha=sha)

    assert not os.path.isdir(seen["path"])
    assert seen["path"] not in git(clone, "worktree", "list")


def test_the_patch_survives_the_worktrees_removal(workspace, repo):
    """A fix the runner could not hand back is a fix that did not happen."""
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(NEW_TEST,)), edit=edit(NEW_TEST)
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_APPLIED
    assert NEW_TEST in result.patch


def test_a_new_file_alongside_the_fix_is_in_scope(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE, NEW_TEST)),
        edit=edit(SOURCE, NEW_TEST),
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_APPLIED
    assert set(result.inspection.changed_paths) == {SOURCE, NEW_TEST}


def test_two_findings_are_carried_in_one_turn(workspace, repo):
    _, sha = repo
    output = response_text(finding_id="F1", head_sha=sha, files=(SOURCE,)) + response_text(
        finding_id="F2", head_sha=sha, files=(NEW_TEST,), preamble=""
    )
    agent = ScriptedAgent(stdout=output, edit=edit(SOURCE, NEW_TEST))

    result = run(
        workspace, agent, routed_finding("F1"), routed_finding("F2"), sha=sha
    )

    assert result.outcome is FixRunOutcome.FIX_APPLIED
    assert len(agent.prompts) == 1, "one bounded turn, not one turn per finding"
    assert result.validated.count(FixOutcome.FIXED) == 2


# --------------------------------------------------------------------------
# The agent's own non-fix outcomes
# --------------------------------------------------------------------------


def test_unable_to_fix_is_reported_distinctly(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(
            head_sha=sha,
            outcome="unable_to_fix",
            files=(),
            verification=None,
            reason="the required outcome contradicts the CI contract",
        )
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_NOT_APPLIED
    assert result.exit_code == 48


def test_an_escalation_is_reported_distinctly(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(
            head_sha=sha,
            outcome="escalate",
            files=(),
            verification=None,
            reason="the cited line already does what the finding asks",
        )
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_ESCALATED
    assert result.exit_code == 49


def test_an_escalation_outranks_a_fix_in_the_same_turn(workspace, repo):
    """A human has been asked a question; a green exit code would bury it."""
    _, sha = repo
    output = response_text(finding_id="F1", head_sha=sha, files=(SOURCE,)) + response_text(
        finding_id="F2",
        head_sha=sha,
        outcome="escalate",
        files=(),
        verification=None,
        reason="F2 is not a real problem",
        preamble="",
    )
    agent = ScriptedAgent(stdout=output, edit=edit(SOURCE))

    result = run(workspace, agent, routed_finding("F1"), routed_finding("F2"), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_ESCALATED


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------


def test_an_agent_that_fails_is_reported_and_cleans_up(workspace, repo):
    clone, sha = repo
    seen = {}

    def record(worktree):
        seen["path"] = worktree
        edit(SOURCE)(worktree)

    agent = ScriptedAgent(failure="the coding agent exited 1", edit=record)

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.CODING_AGENT_FAILED
    assert result.exit_code == 43
    assert not os.path.isdir(seen["path"]), "a failed agent leaves no checkout behind"


def test_an_agent_that_edits_and_then_fails_still_has_its_tree_inspected(
    workspace, repo
):
    """An agent that edited files and then timed out has still edited files.

    Skipping the inspection on the failure path would discard the evidence in
    the case an operator most needs it -- and with --agent-cwd the workspace is
    a directory the runner does not remove, so the result would be the only
    record that anything was left behind.
    """
    _, sha = repo
    agent = ScriptedAgent(
        failure="the coding agent did not finish within 1s", edit=edit(SOURCE, NEW_TEST)
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.CODING_AGENT_FAILED
    assert result.inspection is not None
    assert set(result.inspection.changed_paths) == {SOURCE, NEW_TEST}
    assert any("left 2 changed path(s)" in reason for reason in result.reasons)
    assert any("did not finish" in reason for reason in result.reasons)


def test_a_failed_agent_that_changed_nothing_says_so(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(failure="the coding agent exited 2")

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.CODING_AGENT_FAILED
    assert result.inspection is not None
    assert result.inspection.clean
    assert any("holds no change" in reason for reason in result.reasons)


def test_a_failed_agent_that_also_committed_has_that_reported(workspace, repo):
    _, sha = repo

    def commit(worktree):
        edit(SOURCE)(worktree)
        git(worktree, "add", "-A")
        git(worktree, "commit", "--quiet", "-m", "half a fix")

    agent = ScriptedAgent(failure="the coding agent exited 1", edit=commit)

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.CODING_AGENT_FAILED
    assert any("moved HEAD" in reason for reason in result.reasons)


def test_a_failed_agent_that_left_a_credential_file_has_that_reported(workspace, repo):
    _, sha = repo

    def leave(worktree):
        edit(SOURCE)(worktree)
        with open(os.path.join(worktree, ".env"), "w") as handle:
            handle.write("SECRET=1\n")

    agent = ScriptedAgent(failure="the coding agent exited 1", edit=leave)

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.CODING_AGENT_FAILED
    assert result.inspection.unexpected_ignored == (".env",)
    assert any("git-ignored path(s)" in reason for reason in result.reasons)


# --------------------------------------------------------------------------
# The patch has to survive, or the turn is not a success
# --------------------------------------------------------------------------


def test_a_diff_too_large_to_capture_is_not_a_success(workspace, repo, monkeypatch):
    """The worktree is removed when the run ends, so an uncaptured diff is a
    change nobody can retrieve. Reporting exit 0 for it would be a false
    success -- the fix would be announced and then discarded."""
    monkeypatch.setattr("review_loop.agent_workspace.MAX_PATCH_BYTES", 10)
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=edit(SOURCE)
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.PATCH_TOO_LARGE
    assert result.exit_code == 51
    assert result.exit_code != 0
    assert result.patch == ""
    assert result.validated is not None, "the response itself was valid"
    assert any("larger than" in reason for reason in result.reasons)


def test_an_escalation_still_outranks_an_uncapturable_patch(workspace, repo, monkeypatch):
    """A human has been asked a question; that stays the headline."""
    monkeypatch.setattr("review_loop.agent_workspace.MAX_PATCH_BYTES", 10)
    _, sha = repo
    output = response_text(finding_id="F1", head_sha=sha, files=(SOURCE,)) + response_text(
        finding_id="F2",
        head_sha=sha,
        outcome="escalate",
        files=(),
        verification=None,
        reason="F2 is not a real problem",
        preamble="",
    )
    agent = ScriptedAgent(stdout=output, edit=edit(SOURCE))

    result = run(workspace, agent, routed_finding("F1"), routed_finding("F2"), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_ESCALATED
    assert any("larger than" in reason for reason in result.reasons)


def test_output_that_is_not_a_response_is_malformed(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(stdout="I fixed it, all tests pass.", edit=edit(SOURCE))

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_RESPONSE_MALFORMED
    assert result.exit_code == 44


def test_a_response_naming_another_commit_is_a_target_mismatch(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(head_sha=OTHER_SHA, files=(SOURCE,)), edit=edit(SOURCE)
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_TARGET_MISMATCH
    assert result.exit_code == 45


def test_a_response_naming_another_finding_is_a_finding_mismatch(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(finding_id="F9", head_sha=sha, files=(SOURCE,)),
        edit=edit(SOURCE),
    )

    result = run(workspace, agent, routed_finding("F1"), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_FINDING_MISMATCH
    assert result.exit_code == 46


def test_a_hidden_extra_change_is_a_scope_violation(workspace, repo):
    """The agent edited two files and reported one."""
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE,)),
        edit=edit(SOURCE, NEW_TEST),
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_SCOPE_VIOLATION
    assert result.exit_code == 47
    assert "did not report" in result.reasons[0]


def test_an_edit_outside_the_routed_scope_is_a_scope_violation(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE, OUTSIDE)),
        edit=edit(SOURCE, OUTSIDE),
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_SCOPE_VIOLATION
    assert "outside the scope" in result.reasons[0]


def test_an_operator_allow_path_permits_the_wider_edit(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE, OUTSIDE)),
        edit=edit(SOURCE, OUTSIDE),
    )

    result = run(
        workspace, agent, routed_finding(), sha=sha, allow_paths=("other/",)
    )

    assert result.outcome is FixRunOutcome.FIX_APPLIED


def test_claiming_a_fix_with_a_clean_tree_is_a_scope_violation(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(stdout=response_text(head_sha=sha, files=(SOURCE,)))

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_SCOPE_VIOLATION
    assert "shows no change there" in result.reasons[0]


def test_a_commit_made_by_the_agent_is_a_scope_violation(workspace, repo):
    """Committing is the next slice's decision, with a human in it."""
    _, sha = repo

    def commit(worktree):
        edit(SOURCE)(worktree)
        git(worktree, "add", "-A")
        git(worktree, "commit", "--quiet", "-m", "the agent committed")

    agent = ScriptedAgent(stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=commit)

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_SCOPE_VIOLATION
    assert "moved HEAD" in result.reasons[0]


def test_an_unexpected_ignored_file_is_a_scope_violation(workspace, repo):
    _, sha = repo

    def leave_credentials(worktree):
        edit(SOURCE)(worktree)
        with open(os.path.join(worktree, ".env"), "w") as handle:
            handle.write("SECRET=1\n")

    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=leave_credentials
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_SCOPE_VIOLATION
    assert "not build or test residue" in result.reasons[0]


def test_build_residue_does_not_fail_the_turn(workspace, repo):
    """Telling the agent to run tests and then failing on their cache would
    make a writable agent turn impossible."""
    _, sha = repo

    def run_tests(worktree):
        edit(SOURCE)(worktree)
        cache = os.path.join(worktree, "pkg", "__pycache__")
        os.makedirs(cache, exist_ok=True)
        with open(os.path.join(cache, "code.pyc"), "w") as handle:
            handle.write("bytecode\n")

    agent = ScriptedAgent(stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=run_tests)

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert result.outcome is FixRunOutcome.FIX_APPLIED
    assert result.inspection.residue_paths == ("pkg/__pycache__/code.pyc",)


def test_a_workspace_that_cannot_be_bound_never_starts_the_agent(repo):
    clone, _ = repo
    agent = ScriptedAgent(stdout="unused")

    result = run_fix(
        agent=agent,
        workspace=PreparedWorkspace(str(clone), 29, role="coding agent"),
        target=make_target(OTHER_SHA),
        verdict=verdict(routed_finding(), head_sha=OTHER_SHA),
    )

    assert result.outcome is FixRunOutcome.CODING_AGENT_WORKSPACE_INVALID
    assert result.exit_code == 42
    assert agent.prompts == []


def test_a_missing_head_ref_is_a_workspace_failure(repo):
    clone, sha = repo
    agent = ScriptedAgent(stdout="unused")

    result = run_fix(
        agent=agent,
        workspace=PreparedWorkspace(str(clone), 999, role="coding agent"),
        target=make_target(sha),
        verdict=verdict(routed_finding(), head_sha=sha),
    )

    assert result.outcome is FixRunOutcome.CODING_AGENT_WORKSPACE_INVALID
    assert agent.prompts == []


# --------------------------------------------------------------------------
# What this turn is not allowed to do
# --------------------------------------------------------------------------


def test_the_result_never_claims_a_github_write(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=edit(SOURCE)
    )

    result = run(workspace, agent, routed_finding(), sha=sha)

    assert not hasattr(result, "comment_id")
    assert not hasattr(result, "github_write_performed")


def test_the_prompt_carries_the_exact_target_and_finding(workspace, repo):
    _, sha = repo
    agent = ScriptedAgent(
        stdout=response_text(head_sha=sha, files=(SOURCE,)), edit=edit(SOURCE)
    )

    run(workspace, agent, routed_finding("F1"), sha=sha)

    prompt = agent.prompts[0]
    assert sha in prompt
    assert "F1" in prompt
    assert "pkg/" in prompt
