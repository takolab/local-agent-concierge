"""``review-loop fix``: its arguments, its output, and what it cannot reach.

The last section is the important one. This command's central claim is that
it makes no GitHub request at all -- and that claim is asserted at the source
level, the way PR #29 asserted its single-write boundary, rather than by
observing that no request happened to be made in a test.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from review_loop import fix_cli
from review_loop.cli import main
from review_loop.fix_response import FixRunOutcome
from review_loop.model import EXIT_USAGE
from review_loop.reviewer_workspace import WorkspaceError

from fix_fakes import (
    FULL_SHA,
    OTHER_SHA,
    FakeWorkspace,
    ScriptedAgent,
    finding,
    response_text,
    review_json,
)

SOURCE = "pkg/code.py"


@pytest.fixture
def tree(tmp_path):
    """A real git worktree the fake workspace hands to the agent.

    Real git, because the runner inspects the tree with it *and* asks it what
    this pull request changed: a plain directory, or a repository with no base
    branch to have diverged from, would make every assertion here a statement
    about a workspace the runner would refuse in production.

    So the shape is the real one -- a reachable remote holding ``master`` at
    the base commit, and a detached head one commit ahead of it, which is the
    pull request.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }

    def git(cwd, *argv):
        subprocess.run(
            ["git", *argv], cwd=str(cwd), check=True, env=env, capture_output=True
        )

    # A bare "remote", because the change-set boundary fetches the base tip
    # rather than trusting a local ref -- so a repository with no reachable
    # remote is one the runner now correctly refuses.
    bare = tmp_path / "origin.git"
    bare.mkdir()
    git(bare, "init", "--quiet", "--bare")

    work = tmp_path / "work"
    package = work / "pkg"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text("[project]\nname='pkg'\n")
    (package / "code.py").write_text("value = 0\n")

    git(work, "-c", "init.defaultBranch=master", "init", "--quiet")
    git(work, "add", "-A")
    git(work, "commit", "--quiet", "-m", "base")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "--quiet", "origin", "HEAD:refs/heads/master")

    git(work, "checkout", "--quiet", "--detach")
    (package / "code.py").write_text("value = 1\n")
    git(work, "add", "-A")
    git(work, "commit", "--quiet", "-m", "the pull request")
    return work


@pytest.fixture
def head(tree):
    """The tree's real HEAD, which the routing input must describe."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tree),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def write(tmp_path, document, name="review.json"):
    path = tmp_path / name
    path.write_text(document)
    return str(path)


def invoke(argv, **kwargs):
    stream = io.StringIO()
    code = main(["fix", *argv], stream=stream, **kwargs)
    return code, stream.getvalue()


def routed(**kwargs):
    kwargs.setdefault("location", "pkg/code.py:1")
    return finding(**kwargs)


# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------


def test_a_review_json_is_required():
    with pytest.raises(SystemExit):
        invoke(["--agent-command", "agent"])


def test_an_agent_command_is_required(tmp_path):
    path = write(tmp_path, review_json(routed()))

    code, output = invoke(["--review-json", path])

    assert code == EXIT_USAGE
    assert "--agent-command is required" in output


def test_an_unparseable_agent_command_is_a_usage_error(tmp_path):
    path = write(tmp_path, review_json(routed()))

    code, output = invoke(["--review-json", path, "--agent-command", "a 'unterminated"])

    assert code == EXIT_USAGE
    assert "could not parse" in output


def test_a_non_positive_max_findings_is_a_usage_error(tmp_path):
    path = write(tmp_path, review_json(routed()))

    code, output = invoke(
        ["--review-json", path, "--agent-command", "agent", "--max-findings", "0"]
    )

    assert code == EXIT_USAGE
    assert "positive integer" in output


def test_a_missing_review_json_file_is_a_routing_input_error(tmp_path):
    code, output = invoke(
        ["--review-json", str(tmp_path / "absent.json"), "--agent-command", "agent"]
    )

    assert code == 41
    assert "could not be read" in output


def test_a_review_json_that_is_not_a_validated_review_is_refused(tmp_path):
    path = write(tmp_path, review_json(routed(), outcome="REVIEW_MALFORMED"))

    code, output = invoke(["--review-json", path, "--agent-command", "agent"])

    assert code == 41
    assert "ROUTING_INPUT_INVALID" in output


def test_the_review_json_can_be_read_from_stdin(monkeypatch, tree):
    monkeypatch.setattr("sys.stdin", io.StringIO(review_json()))

    code, output = invoke(
        ["--review-json", "-"],
        agent=ScriptedAgent(stdout="unused"),
        workspace=FakeWorkspace(str(tree)),
    )

    assert code == 0
    assert "NO_ACTIONABLE_FINDINGS" in output


# --------------------------------------------------------------------------
# The turn, through the CLI
# --------------------------------------------------------------------------


def test_an_approved_review_exits_zero_without_an_agent(tmp_path, tree):
    path = write(tmp_path, review_json())
    agent = ScriptedAgent(stdout="unused")

    code, output = invoke(
        ["--review-json", path], agent=agent, workspace=FakeWorkspace(str(tree))
    )

    assert code == 0
    assert "NO_ACTIONABLE_FINDINGS" in output
    assert "Agent invoked:        No" in output
    assert agent.prompts == []


def test_a_dry_run_reports_what_would_be_routed_and_invokes_nothing(tmp_path, tree):
    path = write(tmp_path, review_json(routed()))
    agent = ScriptedAgent(stdout="unused")
    workspace = FakeWorkspace(str(tree))

    code, output = invoke(
        ["--review-json", path, "--dry-run"], agent=agent, workspace=workspace
    )

    assert code == 0
    assert "ROUTING_PREPARED" in output
    assert agent.prompts == []
    assert workspace.opened == [], "a dry run must not open a writable workspace"


def test_a_successful_fix_is_reported(tmp_path, tree, head):
    path = write(tmp_path, review_json(routed(), head_sha=head))

    def edit(worktree):
        with open(os.path.join(worktree, SOURCE), "w") as handle:
            handle.write("value = 2\n")

    agent = ScriptedAgent(stdout=response_text(files=(SOURCE,), head_sha=head), edit=edit)

    code, output = invoke(
        ["--review-json", path], agent=agent, workspace=FakeWorkspace(str(tree))
    )

    assert code == 0
    assert "FIX_APPLIED" in output
    assert "F1: fixed" in output
    assert "Routed findings:      F1 (Major)" in output


def test_the_text_output_states_both_write_boundaries(tmp_path, tree):
    path = write(tmp_path, review_json())

    code, output = invoke(
        ["--review-json", path],
        agent=ScriptedAgent(stdout="unused"),
        workspace=FakeWorkspace(str(tree)),
    )

    assert "GitHub write performed: No" in output
    assert "Commit or push performed: No" in output


def test_a_workspace_failure_is_reported_with_its_own_code(tmp_path, tree):
    path = write(tmp_path, review_json(routed()))
    workspace = FakeWorkspace(str(tree), error=WorkspaceError("wrong commit"))

    code, output = invoke(
        ["--review-json", path], agent=ScriptedAgent(stdout="x"), workspace=workspace
    )

    assert code == 42
    assert "CODING_AGENT_WORKSPACE_INVALID" in output


def test_a_repository_mismatch_is_refused_before_anything_runs(tmp_path, tree):
    path = write(tmp_path, review_json(routed()))
    agent = ScriptedAgent(stdout="unused")

    code, output = invoke(
        ["--review-json", path, "--repo", "someone/else"],
        agent=agent,
        workspace=FakeWorkspace(str(tree)),
    )

    assert code == 41
    assert agent.prompts == []


# --------------------------------------------------------------------------
# The patch
# --------------------------------------------------------------------------


def test_the_patch_is_written_where_it_is_asked_for(tmp_path, tree, head):
    path = write(tmp_path, review_json(routed(), head_sha=head))
    patch_path = tmp_path / "fix.patch"

    def edit(worktree):
        with open(os.path.join(worktree, SOURCE), "w") as handle:
            handle.write("value = 2\n")

    agent = ScriptedAgent(stdout=response_text(files=(SOURCE,), head_sha=head), edit=edit)

    code, output = invoke(
        ["--review-json", path, "--write-patch", str(patch_path)],
        agent=agent,
        workspace=FakeWorkspace(str(tree)),
    )

    assert code == 0
    assert "value = 2" in patch_path.read_text()
    assert str(patch_path) in output


def test_without_write_patch_the_diff_is_reported_as_unkept(tmp_path, tree, head):
    path = write(tmp_path, review_json(routed(), head_sha=head))

    def edit(worktree):
        with open(os.path.join(worktree, SOURCE), "w") as handle:
            handle.write("value = 2\n")

    agent = ScriptedAgent(stdout=response_text(files=(SOURCE,), head_sha=head), edit=edit)

    code, output = invoke(
        ["--review-json", path], agent=agent, workspace=FakeWorkspace(str(tree))
    )

    assert "not written (use --write-patch)" in output


def test_a_patch_that_cannot_be_written_has_its_own_outcome(tmp_path, tree, head):
    path = write(tmp_path, review_json(routed(), head_sha=head))

    def edit(worktree):
        with open(os.path.join(worktree, SOURCE), "w") as handle:
            handle.write("value = 2\n")

    agent = ScriptedAgent(stdout=response_text(files=(SOURCE,), head_sha=head), edit=edit)

    code, output = invoke(
        ["--review-json", path, "--write-patch", str(tmp_path / "absent" / "f.patch")],
        agent=agent,
        workspace=FakeWorkspace(str(tree)),
    )

    assert code == 50
    assert "PATCH_WRITE_FAILED" in output


# --------------------------------------------------------------------------
# JSON output
# --------------------------------------------------------------------------


def test_the_json_output_carries_the_whole_result(tmp_path, tree, head):
    path = write(tmp_path, review_json(routed(), head_sha=head))

    def edit(worktree):
        with open(os.path.join(worktree, SOURCE), "w") as handle:
            handle.write("value = 2\n")

    agent = ScriptedAgent(stdout=response_text(files=(SOURCE,), head_sha=head), edit=edit)

    code, output = invoke(
        ["--review-json", path, "--json"],
        agent=agent,
        workspace=FakeWorkspace(str(tree)),
    )

    payload = json.loads(output)
    assert payload["outcome"] == "FIX_APPLIED"
    assert payload["exit_code"] == 0
    assert payload["target"]["head_sha"] == head
    assert payload["responses"][0]["finding_id"] == "F1"
    assert payload["responses"][0]["target_head_sha"] == head
    assert payload["workspace"]["changed_paths"] == [SOURCE]
    assert payload["request"]["allowed_paths"] == ["pkg/"]


def test_the_json_output_pins_the_write_boundaries(tmp_path, tree):
    path = write(tmp_path, review_json())

    _, output = invoke(
        ["--review-json", path, "--json"],
        agent=ScriptedAgent(stdout="unused"),
        workspace=FakeWorkspace(str(tree)),
    )

    payload = json.loads(output)
    assert payload["github_write_performed"] is False
    assert payload["github_requests_performed"] == 0
    assert payload["commit_or_push_performed"] is False


def test_the_boundary_is_reported_alongside_the_allowed_scope(tmp_path, tree, head):
    path = write(tmp_path, review_json(routed(), head_sha=head))

    def edit(worktree):
        with open(os.path.join(worktree, SOURCE), "w") as handle:
            handle.write("value = 2\n")

    agent = ScriptedAgent(stdout=response_text(files=(SOURCE,), head_sha=head), edit=edit)

    code, output = invoke(
        ["--review-json", path], agent=agent, workspace=FakeWorkspace(str(tree))
    )

    assert code == 0
    assert "Change-set boundary:  pkg/" in output
    assert "Allowed scope:        pkg/" in output


def test_a_citation_outside_the_change_set_is_shown_as_not_granted(
    tmp_path, tree, head
):
    """An operator deciding whether to pass --allow-path has to see it."""
    reaching = routed(
        location="pkg/code.py:1 which contradicts pkg/pyproject.toml and README.md"
    )
    (tree / "README.md").write_text("unchanged by this pull request\n")
    path = write(tmp_path, review_json(reaching, head_sha=head))
    agent = ScriptedAgent(stdout="unused")

    code, output = invoke(
        ["--review-json", path], agent=agent, workspace=FakeWorkspace(str(tree))
    )

    assert "Cited but out of PR:  README.md" in output
    assert "--allow-path" in output


def test_the_json_output_carries_the_boundary(tmp_path, tree, head):
    path = write(tmp_path, review_json(routed(), head_sha=head))

    def edit(worktree):
        with open(os.path.join(worktree, SOURCE), "w") as handle:
            handle.write("value = 2\n")

    agent = ScriptedAgent(stdout=response_text(files=(SOURCE,), head_sha=head), edit=edit)

    _, output = invoke(
        ["--review-json", path, "--json"],
        agent=agent,
        workspace=FakeWorkspace(str(tree)),
    )

    payload = json.loads(output)
    assert payload["request"]["change_set_boundary"] == ["pkg/"]
    assert payload["request"]["findings"][0]["out_of_boundary_paths"] == []


def test_a_repository_without_the_base_branch_cannot_bound_a_fix(
    tmp_path, tree, head
):
    """Failing closed: with no base to have diverged from, the scope would
    have no authority behind it but the reviewer's own text."""
    path = write(
        tmp_path, review_json(routed(), head_sha=head).replace('"master"', '"absent"')
    )
    agent = ScriptedAgent(stdout="unused")

    code, output = invoke(
        ["--review-json", path], agent=agent, workspace=FakeWorkspace(str(tree))
    )

    assert code == 42
    assert "CODING_AGENT_WORKSPACE_INVALID" in output
    assert agent.prompts == [], "no agent runs without an established boundary"


def test_every_outcome_has_a_distinct_exit_code():
    """A later slice branches on these; collapsing two would hide a case."""
    from review_loop.fix_response import FIX_EXIT_CODES

    assert set(FIX_EXIT_CODES) == set(FixRunOutcome)
    non_zero = [code for code in FIX_EXIT_CODES.values() if code != 0]
    assert len(non_zero) == len(set(non_zero))


def test_fix_exit_codes_do_not_collide_with_the_earlier_commands():
    from review_loop.fix_response import FIX_EXIT_CODES
    from review_loop.model import EXIT_CODES
    from review_loop.verdict import REVIEW_EXIT_CODES

    earlier = {code for code in EXIT_CODES.values() if code} | {
        code for code in REVIEW_EXIT_CODES.values() if code
    }
    fix = {code for code in FIX_EXIT_CODES.values() if code}

    assert not earlier & fix


# --------------------------------------------------------------------------
# The write boundary, asserted at the source level
# --------------------------------------------------------------------------

FIX_MODULES = (
    "fix_cli",
    "fix_runner",
    "fix_request",
    "fix_response",
    "fix_response_parser",
    "fix_validation",
    "agent_prompt",
    "agent_process",
    "agent_workspace",
    "routing",
)


def _module_path(name: str) -> Path:
    return Path(fix_cli.__file__).parent / f"{name}.py"


@pytest.mark.parametrize("name", FIX_MODULES)
def test_no_fix_module_imports_a_github_client(name):
    """The claim is structural: these modules cannot reach GitHub at all."""
    tree = ast.parse(_module_path(name).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not {"github_client", "github_comments", ".github_client", ".github_comments"} & imported
    assert not any("github" in name for name in imported)


@pytest.mark.parametrize("name", FIX_MODULES)
def test_no_fix_module_names_the_gh_cli_or_an_http_method(name):
    source = _module_path(name).read_text()
    literals = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert not {"gh", "POST", "PATCH", "PUT", "DELETE"} & literals
    assert not any("api.github.com" in literal for literal in literals)


def test_the_fix_command_is_dispatched_without_a_github_client():
    """`main` threads no client, reader or writer into the fix subcommand."""
    source = Path(fix_cli.__file__).parent.joinpath("cli.py").read_text()
    dispatch = source.split("if arguments and arguments[0] == FIX_COMMAND:")[1].split(
        "if arguments and arguments[0] == REVIEW_COMMAND:"
    )[0]

    assert "fix_main(arguments[1:], agent=agent, workspace=workspace, stream=stream)" in dispatch
    assert "client" not in dispatch.replace("# No client, reader or writer", "")


def test_the_fix_runner_never_receives_a_writer():
    from review_loop.fix_runner import run_fix
    import inspect

    parameters = set(inspect.signature(run_fix).parameters)

    assert "writer" not in parameters
    assert "client" not in parameters
    assert "reader" not in parameters
