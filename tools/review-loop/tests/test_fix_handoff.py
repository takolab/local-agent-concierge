"""What a candidate patch has to look like before anything is committed.

The handoff is the last purely-textual gate before this pipeline gains write
authority, so the tests here are about refusals: every shape that is not "a
fix turn that ended FIX_APPLIED, against one exact commit, with a patch that
has an identity" has to stop here rather than reach git.
"""

from __future__ import annotations

import json

import pytest

from fakes import BASE_TIP, FULL_SHA, OTHER_SHA, REPO
from push_fakes import fix_json
from review_loop.fix_handoff import FixHandoffError, load_handoff

DIGEST = "a" * 64
PATHS = ("pkg/code.py", "pkg/new.py")


def document(**overrides) -> str:
    kwargs = {
        "head_sha": FULL_SHA,
        "changed_paths": PATHS,
        "patch_sha256": DIGEST,
        "patch_bytes": 420,
        "patch_path": "/tmp/candidate.patch",
    }
    kwargs.update(overrides)
    return fix_json(**kwargs)


def mutate(path: list[str], value) -> str:
    """Edit one field of a realistic document, leaving the rest valid."""
    payload = json.loads(document())
    cursor = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    return json.dumps(payload)


def drop(path: list[str]) -> str:
    payload = json.loads(document())
    cursor = payload
    for key in path[:-1]:
        cursor = cursor[key]
    del cursor[path[-1]]
    return json.dumps(payload)


# --------------------------------------------------------------------------
# The shape that is accepted
# --------------------------------------------------------------------------


def test_a_fix_applied_document_carries_the_candidate_patch():
    handoff = load_handoff(document())

    assert handoff.target.repo == REPO
    assert handoff.target.head_sha == FULL_SHA
    assert handoff.target.ci_merge_base_sha == BASE_TIP
    assert handoff.changed_paths == PATHS
    assert handoff.patch_sha256 == DIGEST
    assert handoff.patch_bytes == 420
    assert handoff.patch_path == "/tmp/candidate.patch"
    assert handoff.finding_ids == ("F1",)


def test_changed_paths_are_normalised_and_sorted():
    handoff = load_handoff(
        document(changed_paths=("pkg/new.py", "./pkg/code.py"))
    )

    assert handoff.changed_paths == ("pkg/code.py", "pkg/new.py")


def test_a_missing_patch_path_is_allowed_and_reported_as_absent():
    handoff = load_handoff(document(patch_path=None))

    assert handoff.patch_path is None


# --------------------------------------------------------------------------
# Only one fix outcome describes a patch worth committing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        "FIX_NOT_APPLIED",
        "FIX_ESCALATED",
        "PATCH_TOO_LARGE",
        "NO_ACTIONABLE_FINDINGS",
        "ROUTING_PREPARED",
        "FIX_SCOPE_VIOLATION",
        "REVIEW_REQUIRES_HUMAN",
    ],
)
def test_no_other_fix_outcome_is_pushable(outcome):
    with pytest.raises(FixHandoffError) as error:
        load_handoff(document(outcome=outcome))

    assert outcome in str(error.value)
    assert "FIX_APPLIED" in str(error.value)


def test_a_dry_run_document_is_not_a_candidate_patch():
    with pytest.raises(FixHandoffError, match="dry_run"):
        load_handoff(document(dry_run=True))


def test_a_document_that_already_reports_a_push_is_refused():
    with pytest.raises(FixHandoffError, match="commit_or_push_performed"):
        load_handoff(document(commit_or_push_performed=True))


def test_a_refused_patch_capture_leaves_nothing_to_push():
    with pytest.raises(FixHandoffError, match="refused to capture its own patch"):
        load_handoff(document(patch_refused="the diff was too large"))


def test_a_fix_that_left_unexpected_ignored_files_is_not_pushable():
    with pytest.raises(FixHandoffError, match="git-ignored"):
        load_handoff(document(unexpected_ignored=(".env",)))


# --------------------------------------------------------------------------
# The digest is the load-bearing field
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "digest",
    ["", "not-a-digest", "A" * 64, "a" * 63, "a" * 65, DIGEST[:40], 12345, None],
)
def test_a_patch_without_a_usable_digest_is_refused(digest):
    with pytest.raises(FixHandoffError, match="patch_sha256"):
        load_handoff(mutate(["workspace", "patch_sha256"], digest))


def test_a_document_with_no_digest_field_at_all_is_refused():
    with pytest.raises(FixHandoffError, match="patch_sha256"):
        load_handoff(drop(["workspace", "patch_sha256"]))


@pytest.mark.parametrize("size", [0, -1, "420", None, 2_000_001])
def test_an_unusable_patch_size_is_refused(size):
    with pytest.raises(FixHandoffError, match="patch"):
        load_handoff(mutate(["workspace", "patch_bytes"], size))


# --------------------------------------------------------------------------
# Identity: one commit, one repository, one round
# --------------------------------------------------------------------------


def test_a_workspace_at_another_commit_is_refused():
    with pytest.raises(FixHandoffError, match="same commit"):
        load_handoff(document(workspace_head_sha=OTHER_SHA))


def test_a_response_about_another_commit_is_refused():
    with pytest.raises(FixHandoffError, match="other than the target"):
        load_handoff(mutate(["responses", 0, "target_head_sha"], OTHER_SHA))


@pytest.mark.parametrize("sha", ["abc123", FULL_SHA.upper(), "", None])
def test_an_inexact_head_sha_is_refused(sha):
    with pytest.raises(FixHandoffError, match="head_sha"):
        load_handoff(mutate(["target", "head_sha"], sha))


def test_a_different_repository_than_expected_is_refused():
    with pytest.raises(FixHandoffError, match="but --repo says"):
        load_handoff(document(), expected_repo="someone/else")


def test_a_later_round_is_not_pushed_by_this_runner():
    with pytest.raises(FixHandoffError, match="round"):
        load_handoff(document(round=2))


# --------------------------------------------------------------------------
# The responses must reconstruct the observed change
# --------------------------------------------------------------------------


def test_a_response_that_is_not_fixed_is_refused():
    with pytest.raises(FixHandoffError, match="only a turn in which every routed"):
        load_handoff(mutate(["responses", 0, "outcome"], "unable_to_fix"))


def test_responses_that_omit_an_observed_path_are_refused():
    with pytest.raises(FixHandoffError, match="unreported: pkg/new.py"):
        load_handoff(mutate(["responses", 0, "files_changed"], ["pkg/code.py"]))


def test_responses_naming_a_path_the_tree_did_not_show_are_refused():
    with pytest.raises(FixHandoffError, match="reported but unobserved"):
        load_handoff(
            mutate(["responses", 0, "files_changed"], [*PATHS, "pkg/ghost.py"])
        )


def test_a_document_with_no_responses_is_refused():
    with pytest.raises(FixHandoffError, match="no fix response"):
        load_handoff(mutate(["responses"], []))


def test_a_repeated_finding_id_is_refused():
    payload = json.loads(document())
    payload["responses"].append(dict(payload["responses"][0]))
    with pytest.raises(FixHandoffError, match="answered more than once"):
        load_handoff(json.dumps(payload))


# --------------------------------------------------------------------------
# Paths are repository paths, or they are not paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "~/.ssh/id_rsa", "../outside.py", "pkg/../../out.py", "a\\b.py", "pkg/\x00x"],
)
def test_a_changed_path_that_leaves_the_repository_is_refused(path):
    with pytest.raises(FixHandoffError):
        load_handoff(document(changed_paths=(path,)))


def test_an_empty_changed_path_list_describes_no_fix():
    with pytest.raises(FixHandoffError, match="no changed path"):
        load_handoff(document(changed_paths=()))


def test_a_repeated_changed_path_is_refused():
    with pytest.raises(FixHandoffError, match="more than once"):
        load_handoff(mutate(["workspace", "changed_paths"], ["pkg/a.py", "pkg/a.py"]))


# --------------------------------------------------------------------------
# Not a document at all
# --------------------------------------------------------------------------


@pytest.mark.parametrize("document_text", ["", "not json", "[]", "null", '"text"'])
def test_a_non_document_is_refused(document_text):
    with pytest.raises(FixHandoffError):
        load_handoff(document_text)
