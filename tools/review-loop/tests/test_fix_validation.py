"""A fix response is checked, never believed.

Two families of test. The first is the contract itself -- identity, the
closed outcome vocabulary, the fields each outcome requires. The second is
the cross-examination against the working tree, which is the half that
actually stops a bad fix: a response is only as good as the diff agrees with.
"""

import pytest

from review_loop.agent_workspace import WorkspaceInspection
from review_loop.fix_request import AllowedPath, FixRequest, RoutedFinding
from review_loop.fix_response import (
    FixFindingBindingError,
    FixOutcome,
    FixResponseValidationError,
    FixTargetBindingError,
)
from review_loop.fix_response_parser import parse
from review_loop.fix_validation import ScopeViolation, validate

from fix_fakes import FULL_SHA, OTHER_SHA, finding, response_text, target

SOURCE = "tools/review-loop/src/review_loop/verdict.py"
TEST = "tools/review-loop/tests/test_verdict.py"
ELSEWHERE = "services/orchestrator/src/orchestrator/registry.py"


def request(*findings, allowed=("tools/review-loop",)) -> FixRequest:
    findings = findings or (finding(),)
    entries = tuple(AllowedPath(path, is_directory=True) for path in allowed)
    return FixRequest(
        target=target(),
        round=1,
        findings=tuple(
            RoutedFinding(finding=f, cited_paths=(SOURCE,), allowed_paths=entries)
            for f in findings
        ),
    )


def inspection(*changed, head=FULL_SHA, ignored=(), unexpected=()) -> WorkspaceInspection:
    return WorkspaceInspection(
        head_sha=head,
        changed_paths=tuple(sorted(changed)),
        ignored_paths=tuple(ignored) + tuple(unexpected),
        residue_paths=tuple(ignored),
        unexpected_ignored=tuple(unexpected),
    )


def check(output, req=None, insp=None):
    return validate(
        parse(output),
        request=req or request(),
        inspection=insp if insp is not None else inspection(SOURCE),
    )


# --------------------------------------------------------------------------
# The three outcomes
# --------------------------------------------------------------------------


def test_a_valid_fixed_response_is_accepted():
    validated = check(response_text(files=(SOURCE,)))

    assert validated.responses[0].outcome is FixOutcome.FIXED
    assert validated.responses[0].files_changed == (SOURCE,)
    assert validated.responses[0].verification is not None


def test_a_valid_unable_to_fix_response_is_accepted():
    output = response_text(
        outcome="unable_to_fix",
        files=(),
        verification=None,
        reason="the behaviour the finding asks for contradicts the CI contract",
    )

    validated = check(output, insp=inspection())

    assert validated.responses[0].outcome is FixOutcome.UNABLE_TO_FIX
    assert validated.count(FixOutcome.FIXED) == 0


def test_a_valid_escalation_is_accepted():
    output = response_text(
        outcome="escalate",
        files=(),
        verification=None,
        reason="the cited line already does what the finding asks for",
    )

    validated = check(output, insp=inspection())

    assert validated.responses[0].outcome is FixOutcome.ESCALATE


def test_an_unknown_outcome_is_refused():
    output = response_text(outcome="partially fixed, I think")

    with pytest.raises(FixResponseValidationError, match="unknown outcome"):
        check(output)


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_the_exact_target_sha_is_accepted():
    assert check(response_text(head_sha=FULL_SHA)).responses[0].target_head_sha == FULL_SHA


def test_another_commits_sha_is_refused():
    with pytest.raises(FixTargetBindingError, match="not the exact"):
        check(response_text(head_sha=OTHER_SHA))


def test_an_abbreviated_sha_is_refused_rather_than_resolved():
    """A short SHA is a claim about a commit this runner will not resolve."""
    with pytest.raises(FixTargetBindingError, match="not the exact"):
        check(response_text(head_sha=FULL_SHA[:12]))


def test_a_finding_id_that_was_not_routed_is_refused():
    with pytest.raises(FixFindingBindingError, match="not routed"):
        check(response_text(finding_id="F9"))


def test_a_routed_finding_with_no_answer_is_refused():
    """A silently dropped finding looks exactly like a handled one."""
    req = request(finding("F1"), finding("F2"))

    with pytest.raises(FixFindingBindingError, match="did not answer F2"):
        check(response_text(finding_id="F1"), req=req)


def test_a_finding_answered_twice_is_refused():
    output = response_text(finding_id="F1") + response_text(finding_id="F1", preamble="")

    with pytest.raises(FixFindingBindingError, match="more than once"):
        check(output, insp=inspection(SOURCE))


def test_two_routed_findings_each_answered_once_are_accepted():
    req = request(finding("F1"), finding("F2"))
    output = response_text(finding_id="F1", files=(SOURCE,)) + response_text(
        finding_id="F2", files=(TEST,), preamble=""
    )

    validated = check(output, req=req, insp=inspection(SOURCE, TEST))

    assert validated.count(FixOutcome.FIXED) == 2


# --------------------------------------------------------------------------
# Required fields
# --------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["Finding ID", "Target head SHA", "Outcome", "Summary"])
def test_a_missing_required_field_is_refused(label):
    output = "\n".join(
        line for line in response_text().splitlines() if not line.startswith(label + ":")
    )

    with pytest.raises(FixResponseValidationError, match="missing the required field"):
        check(output + "\n")


def test_fixed_without_verification_is_refused():
    """A fix reported with no verification is an assertion."""
    with pytest.raises(FixResponseValidationError, match="without a 'Verification'"):
        check(response_text(verification=None))


def test_fixed_with_no_changed_file_is_refused():
    """There is no 'fixed, no code change'; that answer is 'escalate'."""
    with pytest.raises(FixResponseValidationError, match="lists no changed file"):
        check(response_text(files=()), insp=inspection())


@pytest.mark.parametrize("outcome", ["unable_to_fix", "escalate"])
def test_a_non_fix_without_a_reason_is_refused(outcome):
    output = response_text(outcome=outcome, files=(), verification=None)

    with pytest.raises(FixResponseValidationError, match="without a 'Reason'"):
        check(output, insp=inspection())


@pytest.mark.parametrize("outcome", ["unable_to_fix", "escalate"])
def test_a_non_fix_that_changed_files_is_refused(outcome):
    """Half-finished edits with no claim attached are the worst artifact."""
    output = response_text(
        outcome=outcome, files=(SOURCE,), verification=None, reason="gave up"
    )

    with pytest.raises(FixResponseValidationError, match="must leave the working tree"):
        check(output)


def test_an_oversized_field_is_refused():
    with pytest.raises(FixResponseValidationError, match="above the"):
        check(response_text(summary="x" * 5000))


def test_too_many_reported_files_are_refused():
    files = tuple(f"tools/review-loop/f{n}.py" for n in range(60))

    with pytest.raises(FixResponseValidationError, match="above the"):
        check(response_text(files=files), insp=inspection(*files))


def test_a_file_reported_twice_is_refused():
    output = response_text(files=(SOURCE, SOURCE))

    with pytest.raises(FixResponseValidationError, match="more than once"):
        check(output)


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../outside.py", "~/secrets", "a\\b.py"]
)
def test_a_reported_path_that_leaves_the_repository_is_refused(path):
    with pytest.raises(FixResponseValidationError):
        check(response_text(files=(path,)), insp=inspection(path))


# --------------------------------------------------------------------------
# The working tree
# --------------------------------------------------------------------------


def test_reported_files_equal_to_the_actual_change_are_accepted():
    output = response_text(files=(SOURCE, TEST))

    assert check(output, insp=inspection(SOURCE, TEST)).responses[0].files_changed


def test_a_hidden_extra_modified_file_is_refused():
    """The check that makes 'Files changed' a claim rather than a courtesy."""
    output = response_text(files=(SOURCE,))

    with pytest.raises(ScopeViolation, match="did not report"):
        check(output, insp=inspection(SOURCE, TEST))


def test_a_reported_file_that_did_not_change_is_refused():
    output = response_text(files=(SOURCE, TEST))

    with pytest.raises(ScopeViolation, match="shows no change there"):
        check(output, insp=inspection(SOURCE))


def test_claiming_fixed_while_the_tree_is_clean_is_refused():
    with pytest.raises(ScopeViolation, match="shows no change there"):
        check(response_text(files=(SOURCE,)), insp=inspection())


def test_an_edit_outside_the_allowed_scope_is_refused():
    output = response_text(files=(SOURCE, ELSEWHERE))

    with pytest.raises(ScopeViolation, match="outside the scope"):
        check(output, insp=inspection(SOURCE, ELSEWHERE))


def test_an_allowed_test_file_edit_is_accepted():
    """A fix whose test cannot be updated is not a fix."""
    output = response_text(files=(SOURCE, TEST))

    validated = check(output, insp=inspection(SOURCE, TEST))

    assert set(validated.responses[0].files_changed) == {SOURCE, TEST}


def test_a_commit_made_by_the_agent_is_refused():
    """A committed fix is a change `git status` no longer reports."""
    with pytest.raises(ScopeViolation, match="moved HEAD"):
        check(response_text(files=(SOURCE,)), insp=inspection(SOURCE, head=OTHER_SHA))


def test_build_and_test_residue_is_tolerated():
    insp = inspection(SOURCE, ignored=("tools/review-loop/tests/__pycache__/x.pyc",))

    assert check(response_text(files=(SOURCE,)), insp=insp).responses


def test_an_unexpected_ignored_file_is_refused():
    """The tree started with none, so the agent produced this one."""
    insp = inspection(SOURCE, unexpected=(".env",))

    with pytest.raises(ScopeViolation, match="not build or test residue"):
        check(response_text(files=(SOURCE,)), insp=insp)


def test_the_refusal_names_the_ignored_path_not_its_contents():
    insp = inspection(SOURCE, unexpected=("credentials.json",))

    with pytest.raises(ScopeViolation) as caught:
        check(response_text(files=(SOURCE,)), insp=insp)

    assert "credentials.json" in str(caught.value)


def test_an_operator_widened_scope_permits_the_edit():
    req = request(allowed=("tools/review-loop", "docs"))
    output = response_text(files=(SOURCE, "docs/roadmap.md"))

    validated = validate(
        parse(output),
        request=req,
        inspection=inspection(SOURCE, "docs/roadmap.md"),
    )

    assert len(validated.responses) == 1
