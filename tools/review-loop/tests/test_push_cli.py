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

    # The provenance the re-review stage pairs against. Both halves: which
    # review caused this fix, and what git said the fix is.
    provenance = payload["fix_provenance"]
    assert provenance["source_round"] == 1
    assert provenance["source_reviewed_head_sha"] == live.head_sha
    assert provenance["source_finding_ids"] == ["F1"]
    assert provenance["source_patch_sha256"] == live.patch_sha256
    assert provenance["fix_sha"] == payload["pushed_sha"]
    assert provenance["fix_parent_sha"] == live.head_sha
    assert provenance["fix_patch_sha256"] == live.patch_sha256


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
    # Narrower than "the branch is unchanged", which this runner cannot know.
    assert "No -- this run performed no repository write" in out
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
    # Stated as a fact about the branch, not as an attribution: this runner
    # cannot tell its own earlier run from another actor.
    assert "the branch already held this exact fix" in out
    assert "this run did not move the ref" in out
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
    # Narrower than "the branch is unchanged", which this runner cannot know.
    assert "No -- this run performed no repository write" in out
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


#: The one force-shaped flag this slice is allowed to build, and only as the
#: prefix of a value naming the derived ref and an exact 40-character commit.
#: It is a compare-and-swap condition, not an authorisation to rewrite: the
#: commit's parent is already proven to be the leased value.
LEASE_PREFIX = "--force-with-lease="


@pytest.mark.parametrize("name", PUSH_MODULES)
def test_no_push_module_can_force_a_push_or_write_a_tag(name):
    """The blast radius, pinned: one exact CAS form, and nothing else."""
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
    forceish = {literal for literal in argv_like if literal.startswith("--force")}

    # `--force`, and a bare `--force-with-lease` -- which leases against the
    # local remote-tracking ref, i.e. against whatever this clone last
    # fetched -- are both absent. Only the assignment form may appear.
    assert forceish <= {LEASE_PREFIX}
    assert "--force" not in argv_like
    assert "--force-with-lease" not in argv_like
    assert not any("refs/tags" in literal for literal in argv_like)
    assert not any(literal.startswith("+") for literal in argv_like)


def test_only_push_branch_may_build_the_lease():
    """One module *constructs* it; others may only describe it.

    The distinction is the point, so it is read from the syntax rather than
    from the text: a lease is built by interpolating into an f-string, which
    is what `push_branch` does and what nothing else may do. Help text and
    documentation that merely name the flag are not construction.
    """
    builders = set()
    for name in PUSH_MODULES:
        tree = ast.parse(_module_path(name).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in node.values:
                if (
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and part.value.startswith(LEASE_PREFIX)
                ):
                    builders.add(name)

    assert builders == {"push_branch"}


def test_the_lease_names_the_derived_ref_and_an_exact_commit():
    """The value, not just the flag: a CAS on one ref at one exact old value."""
    import re

    from fakes import FULL_SHA, REPO, pull_request_payload
    from review_loop.push_branch import BranchAuthorityError, resolve

    target = resolve(
        pull_request_payload(number=27, head_ref="feat/example"), repo=REPO, number=27
    )

    lease = target.lease(FULL_SHA)

    assert lease == f"--force-with-lease=refs/heads/feat/example:{FULL_SHA}"
    assert re.fullmatch(r"--force-with-lease=refs/heads/[^:]+:[0-9a-f]{40}", lease)
    # It cannot be built for anything but an exact commit, so it can never
    # degrade into the unqualified form.
    for bad in ("", "HEAD", "abc1234", FULL_SHA.upper(), FULL_SHA + "a"):
        with pytest.raises(BranchAuthorityError):
            target.lease(bad)


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
            if not (
                isinstance(node.func, ast.Name)
                and node.func.id in {"run_git", "run_git_capture"}
            ):
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
        elif outcome is PushOutcome.PUSH_WROTE_UNEXPECTED_REFS:
            # Certainly written, and more than was authorised: known, not
            # unknown, and the one outcome that is mutated without the branch
            # necessarily holding the fix.
            assert mutated is True
        elif outcome is PushOutcome.PUSH_BOUNDARY_NOT_VERIFIED:
            # The *boundary* is what is unknown. Whether a write happened is
            # answered from the branch read-back, so it depends on the result
            # rather than on the outcome alone.
            assert mutated is None
            assert (
                PushResult(
                    outcome=outcome, pushed_sha="a" * 40
                ).repository_mutated
                is True
            )
        else:
            assert mutated is (outcome in PUSHED_OUTCOMES)


def test_the_github_client_is_scoped_from_the_handoff_not_the_directory(
    tmp_path, live, monkeypatch
):
    """The git side and the GitHub side must have one authority, not two.

    `detect_repository()` reads the repository out of whatever clone the
    operator is standing in. Using it would let this command read a pull
    request and its CI from one repository while pushing to another.
    """
    constructed = []

    class RecordingClient:
        def __init__(self, repo, **kwargs):
            constructed.append(repo)
            raise ValueError("stop here; the constructor is what is under test")

    def forbidden():  # pragma: no cover - the assertion is that it is not called
        raise AssertionError("detect_repository() must not decide the repository")

    monkeypatch.setattr(push_cli, "GitHubClient", RecordingClient)
    monkeypatch.setattr(push_cli, "detect_repository", forbidden, raising=False)
    monkeypatch.chdir(tmp_path)

    code, out = run(
        ["--fix-json", write_fix_json(tmp_path, live), "--repo-root", str(live.clone)]
    )

    assert constructed == ["takolab/local-agent-concierge"]
    assert code == PUSH_EXIT_CODES[PushOutcome.PUSH_API_ERROR]


def test_the_push_command_cannot_detect_a_repository_from_the_directory():
    """Structural: the module does not import the cwd-based detector at all."""
    tree = ast.parse(_module_path("push_cli").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update(alias.name for alias in node.names)

    assert "detect_repository" not in imported


def test_the_unexpected_ref_outcome_does_not_render_a_missing_sha(tmp_path, live):
    """Exit 74 has no pushed SHA, and must not claim one.

    The generic pushed-SHA branch rendered `Yes -- None was pushed by this
    run`, which is both false and unreadable for the one outcome that means
    the authorised branch state was never established.
    """
    from review_loop.push_runner import PushResult

    result = PushResult(outcome=PushOutcome.PUSH_WROTE_UNEXPECTED_REFS)
    stream = io.StringIO()
    push_cli.render_text(result, stream)
    out = stream.getvalue()

    assert result.repository_mutated is True
    assert "None" not in out.split("Repository mutated:")[1].splitlines()[0]
    assert "unexpected ref update" in out
    assert "Inspect the remote" in out
    assert "Outcome:              PUSH_WROTE_UNEXPECTED_REFS" in out


def test_no_write_is_stated_as_this_runs_inaction_not_the_branchs_stillness():
    """Concurrency makes "the branch is unchanged" a claim this cannot prove."""
    from review_loop.push_runner import PushResult

    for outcome in (
        PushOutcome.PUSH_FAILED,
        PushOutcome.PUSH_BRANCH_REFUSED,
        PushOutcome.PUSH_TARGET_STALE,
        PushOutcome.COMMIT_REFUSED,
    ):
        stream = io.StringIO()
        push_cli.render_text(PushResult(outcome=outcome), stream)
        out = stream.getvalue()

        assert "No -- this run performed no repository write" in out
        assert "verified unchanged" not in out


def test_the_boundary_is_reported_as_its_own_line(tmp_path, live):
    """Branch state and boundary state are two facts, printed as two lines."""
    from review_loop.push_runner import PushResult

    for outcome, fragment in (
        (PushOutcome.PUSH_READY, "clean -- only the authorised ref"),
        (PushOutcome.PUSH_WROTE_UNEXPECTED_REFS, "EXCEEDED"),
        (PushOutcome.PUSH_BOUNDARY_NOT_VERIFIED, "UNKNOWN"),
    ):
        status = {
            PushOutcome.PUSH_READY: "clean",
            PushOutcome.PUSH_WROTE_UNEXPECTED_REFS: "exceeded",
            PushOutcome.PUSH_BOUNDARY_NOT_VERIFIED: "unknown",
        }[outcome]
        stream = io.StringIO()
        push_cli.render_text(
            PushResult(outcome=outcome, boundary_status=status), stream
        )
        out = stream.getvalue()

        assert "Write boundary:" in out
        assert fragment in out.split("Write boundary:")[1].splitlines()[0]


def test_an_exceeded_boundary_reports_the_branch_it_did_establish():
    from review_loop.push_runner import PushResult

    stream = io.StringIO()
    push_cli.render_text(
        PushResult(
            outcome=PushOutcome.PUSH_WROTE_UNEXPECTED_REFS,
            boundary_status="exceeded",
            pushed_sha="c" * 40,
        ),
        stream,
    )
    out = stream.getvalue()

    assert f"{'c' * 40} is on the branch" in out
    assert "None" not in out.split("Repository mutated:")[1].splitlines()[0]
