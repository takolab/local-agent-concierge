"""``review-loop push``: what an operator sees, and what it may reach.

Two kinds of test here. The first kind is about the report: after a run that
may have changed the repository, the single most important thing the output
has to get right is *whether it did*, so that is asserted on every path.

The second kind is about the boundary, asserted at the source level the way PR
#29's single-write boundary and PR #34's no-GitHub boundary are: this command
gained one repository write, and the tests pin that it gained exactly one.
"""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pytest

from conftest import build_scenario
from push_fakes import PushGitHubClient, Timeline, fix_json
from review_loop import cli, push_cli
from review_loop.push_response import PUSH_EXIT_CODES, PushOutcome
from review_loop.reviewer_workspace import PreparedWorkspace

PUSH_ROLE = "fix commit"


def _edits(worktree):
    (worktree / "pkg" / "code.py").write_text("value = 2\n")
    (worktree / "pkg" / "new.py").write_text("added = True\n")


@pytest.fixture
def live(tmp_path):
    return build_scenario(tmp_path, _edits)


def write_fix_json(tmp_path, scenario, **overrides) -> str:
    kwargs = {
        "head_sha": scenario.head_sha,
        "changed_paths": scenario.changed_paths,
        "patch_sha256": scenario.patch_sha256,
        "patch_bytes": scenario.patch_bytes,
        "patch_path": scenario.patch_path,
        "number": scenario.number,
    }
    kwargs.update(overrides)
    path = tmp_path / "fix.json"
    path.write_text(fix_json(**kwargs))
    return str(path)


def run(argv, *, client=None, workspace=None, timeline=None):
    stream = io.StringIO()
    timeline = timeline or Timeline()
    code = push_cli.push_main(
        argv,
        client=client,
        workspace=workspace,
        stream=stream,
        clock=timeline.clock,
        sleep=timeline.sleep,
    )
    return code, stream.getvalue()


def green_client(scenario, timeline):
    client = PushGitHubClient(
        number=scenario.number, head_sha=scenario.head_sha, branch=scenario.branch
    )

    def step():
        pushed = scenario.remote_tip()
        client.head_sha = pushed
        client.ci[pushed] = "success"

    timeline.steps[1] = step
    return client


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def test_a_successful_run_reports_the_exact_pushed_commit(tmp_path, live):
    timeline = Timeline()
    client = green_client(live, timeline)

    code, out = run(
        [
            "--fix-json", write_fix_json(tmp_path, live),
            "--repo-root", str(live.clone),
            "--git-remote", "origin",
        ],
        client=client,
        workspace=PreparedWorkspace(str(live.clone), live.number, remote="origin", role=PUSH_ROLE),
        timeline=timeline,
    )

    pushed = live.remote_tip()
    assert code == 0
    assert "Outcome:              PUSH_READY" in out
    assert f"Pushed commit SHA:    {pushed}" in out
    assert f"Yes -- {pushed} was pushed by this run" in out
    assert "refs/heads/feat/example" in out
    assert "(== the pushed commit)" in out
    assert "GitHub write performed: No" in out


def test_the_json_result_states_what_changed(tmp_path, live):
    timeline = Timeline()
    client = green_client(live, timeline)

    code, out = run(
        [
            "--fix-json", write_fix_json(tmp_path, live),
            "--repo-root", str(live.clone),
            "--json",
        ],
        client=client,
        workspace=PreparedWorkspace(str(live.clone), live.number, remote="origin", role=PUSH_ROLE),
        timeline=timeline,
    )

    payload = json.loads(out)
    assert code == 0
    assert payload["outcome"] == "PUSH_READY"
    assert payload["repository_mutated"] is True
    assert payload["push_performed"] is True
    assert payload["already_pushed"] is False
    assert payload["pushed_sha"] == live.remote_tip()
    assert payload["github_write_performed"] is False
    assert payload["commit"]["parent_sha"] == live.head_sha
    assert payload["commit"]["patch_sha256"] == live.patch_sha256
    assert payload["push_target"]["ref"] == "refs/heads/feat/example"
    assert payload["ci"]["bound_to_pushed_commit"] is True
    assert payload["verified_target"]["head_sha"] == payload["pushed_sha"]


def test_a_refusal_says_plainly_that_nothing_was_written(tmp_path, live):
    client = PushGitHubClient(
        number=live.number,
        head_sha=live.head_sha,
        branch=live.branch,
        head_repo="someone/fork",
    )

    code, out = run(
        ["--fix-json", write_fix_json(tmp_path, live), "--repo-root", str(live.clone)],
        client=client,
        workspace=PreparedWorkspace(str(live.clone), live.number, remote="origin", role=PUSH_ROLE),
    )

    assert code == PUSH_EXIT_CODES[PushOutcome.PUSH_BRANCH_REFUSED]
    assert "No -- the pull request branch is verified unchanged" in out
    assert "Fix commit:           (none created)" in out
    assert live.remote_tip() == live.head_sha


def test_an_already_pushed_fix_is_reported_as_an_earlier_runs_write(tmp_path, live):
    timeline = Timeline()
    client = green_client(live, timeline)
    argv = [
        "--fix-json", write_fix_json(tmp_path, live),
        "--repo-root", str(live.clone),
    ]
    first, _ = run(
        argv,
        client=client,
        workspace=PreparedWorkspace(str(live.clone), live.number, remote="origin", role=PUSH_ROLE),
        timeline=timeline,
    )
    assert first == 0

    code, out = run(
        argv,
        client=client,
        workspace=PreparedWorkspace(str(live.clone), live.number, remote="origin", role=PUSH_ROLE),
    )

    assert code == 0
    assert "was already on the branch" in out
    assert "Fix commit:           (none created)" in out


def test_a_dry_run_reports_that_nothing_was_committed(tmp_path, live):
    code, out = run(
        [
            "--fix-json", write_fix_json(tmp_path, live),
            "--repo-root", str(live.clone),
            "--dry-run",
        ],
        client=PushGitHubClient(
            number=live.number, head_sha=live.head_sha, branch=live.branch
        ),
        workspace=PreparedWorkspace(str(live.clone), live.number, remote="origin", role=PUSH_ROLE),
    )

    assert code == 0
    assert "Outcome:              PUSH_PREPARED" in out
    assert "No -- the pull request branch is verified unchanged" in out
    assert live.remote_tip() == live.head_sha


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def test_the_patch_defaults_to_the_path_the_fix_turn_recorded(tmp_path, live):
    timeline = Timeline()
    client = green_client(live, timeline)

    code, _ = run(
        ["--fix-json", write_fix_json(tmp_path, live), "--repo-root", str(live.clone)],
        client=client,
        workspace=PreparedWorkspace(str(live.clone), live.number, remote="origin", role=PUSH_ROLE),
        timeline=timeline,
    )

    assert code == 0


def test_a_fix_document_with_no_patch_path_requires_the_flag(tmp_path, live):
    code, out = run(
        [
            "--fix-json", write_fix_json(tmp_path, live, patch_path=None),
            "--repo-root", str(live.clone),
        ]
    )

    assert code == PUSH_EXIT_CODES[PushOutcome.PUSH_INPUT_INVALID]
    assert "--patch is required" in out


def test_an_unreadable_fix_document_is_an_input_error(tmp_path):
    code, out = run(["--fix-json", str(tmp_path / "absent.json")])

    assert code == PUSH_EXIT_CODES[PushOutcome.PUSH_INPUT_INVALID]
    assert "could not be read" in out


def test_a_fix_document_for_another_repository_is_refused(tmp_path, live):
    code, out = run(
        [
            "--fix-json", write_fix_json(tmp_path, live),
            "--repo", "someone/else",
            "--repo-root", str(live.clone),
        ]
    )

    assert code == PUSH_EXIT_CODES[PushOutcome.PUSH_INPUT_INVALID]
    assert "someone/else" in out


@pytest.mark.parametrize(
    "argv,message",
    [
        (["--ci-timeout", "-1"], "--ci-timeout"),
        (["--ci-poll", "0"], "--ci-poll"),
    ],
)
def test_usage_errors_are_distinct_from_every_outcome(tmp_path, live, argv, message):
    code, out = run(
        ["--fix-json", write_fix_json(tmp_path, live), *argv]
    )

    assert code == 2
    assert message in out


def test_the_parser_offers_no_way_to_choose_a_branch():
    """The one flag that would undo this design does not exist."""
    parser = push_cli.build_push_parser()
    options = {
        option for action in parser._actions for option in action.option_strings
    }

    assert "--branch" not in options
    assert "--ref" not in options
    assert "--refspec" not in options
    assert "--force" not in options
    assert not any("force" in option for option in options)


# --------------------------------------------------------------------------
# The boundary, asserted at the source level
# --------------------------------------------------------------------------

PUSH_MODULES = (
    "push_cli",
    "push_runner",
    "push_branch",
    "push_response",
    "fix_commit",
    "fix_handoff",
    "patch_identity",
)


def _module_path(name: str) -> Path:
    return Path(push_cli.__file__).parent / f"{name}.py"


@pytest.mark.parametrize("name", PUSH_MODULES)
def test_no_push_module_can_write_to_github(name):
    """The read-only client is reachable; the comment writer is not."""
    tree = ast.parse(_module_path(name).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not {"github_comments", ".github_comments"} & imported


@pytest.mark.parametrize("name", PUSH_MODULES)
def test_no_push_module_names_a_write_http_method(name):
    literals = {
        node.value
        for node in ast.walk(ast.parse(_module_path(name).read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert not {"POST", "PATCH", "PUT", "DELETE"} & literals
    assert not any("api.github.com" in literal for literal in literals)


@pytest.mark.parametrize("name", PUSH_MODULES)
def test_no_push_module_can_force_a_push_or_write_a_tag(name):
    """The blast radius, pinned: no force flag and no tag ref, anywhere."""
    literals = {
        node.value
        for node in ast.walk(ast.parse(_module_path(name).read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    argv_like = {
        literal
        for literal in literals
        if literal.startswith("-") or literal.startswith("refs/") or ":" in literal
    }

    assert not any(literal.startswith("--force") for literal in argv_like)
    assert not any("refs/tags" in literal for literal in argv_like)
    assert not any(literal.startswith("+") for literal in argv_like)


def test_the_git_subcommands_the_push_path_runs_are_exactly_these():
    """Enumerated rather than described, so a new one cannot arrive quietly.

    ``apply``, ``commit`` and ``push`` are the three that change anything, and
    only ``push`` changes anything outside a workspace this run created. The
    rest read. Nothing here can rewrite history, move a ref by hand, or check
    another commit out from under the patch.
    """
    subcommands = set()
    for name in ("fix_commit", "patch_identity"):
        tree = ast.parse(_module_path(name).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "run_git"):
                continue
            first = node.args[0] if node.args else None
            if isinstance(first, ast.List) and first.elts:
                head = first.elts[0]
                if isinstance(head, ast.Constant):
                    subcommands.add(head.value)
            elif isinstance(first, ast.Call):
                # run_git(diff_argv(...)) -- a canonical diff, which reads.
                subcommands.add("diff")

    assert subcommands == {
        "apply",
        "commit",
        "push",
        "fetch",
        "ls-remote",
        "rev-parse",
        "rev-list",
        "diff",
    }
    for forbidden in (
        "reset",
        "rebase",
        "merge",
        "checkout",
        "switch",
        "tag",
        "update-ref",
        "filter-branch",
        "cherry-pick",
        "clean",
    ):
        assert forbidden not in subcommands


def test_the_push_command_is_dispatched_with_a_read_only_client():
    """`main` threads the GET-only client in, and no comment reader or writer.

    Read from the call's own keywords rather than from the surrounding text,
    so that a comment mentioning a writer cannot fail the test and, more to
    the point, so that removing the comment cannot pass it.
    """
    tree = ast.parse(Path(cli.__file__).read_text())
    keywords: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "push_main"
        ):
            keywords = {keyword.arg for keyword in node.keywords}

    assert keywords == {"client", "workspace", "stream"}


def test_the_push_runner_never_receives_a_comment_writer():
    import inspect

    from review_loop.push_runner import run_push

    parameters = set(inspect.signature(run_push).parameters)

    assert "writer" not in parameters
    assert "reader" not in parameters


def test_push_exit_codes_do_not_collide_with_the_earlier_commands():
    from review_loop.fix_response import FIX_EXIT_CODES
    from review_loop.model import EXIT_CODES
    from review_loop.verdict import REVIEW_EXIT_CODES

    earlier = (
        {code for code in EXIT_CODES.values() if code}
        | {code for code in REVIEW_EXIT_CODES.values() if code}
        | {code for code in FIX_EXIT_CODES.values() if code}
    )
    push = {code for code in PUSH_EXIT_CODES.values() if code}

    assert not earlier & push
    assert len(push) == len([c for c in PUSH_EXIT_CODES.values() if c])


def test_every_outcome_has_an_exit_code():
    assert set(PUSH_EXIT_CODES) == set(PushOutcome)


def test_only_outcomes_after_a_verified_push_report_a_mutation():
    from review_loop.push_response import PUSHED_OUTCOMES
    from review_loop.push_runner import PushResult

    for outcome in PushOutcome:
        mutated = PushResult(outcome=outcome).repository_mutated
        if outcome is PushOutcome.PUSH_NOT_VERIFIED:
            assert mutated is None
        else:
            assert mutated is (outcome in PUSHED_OUTCOMES)
