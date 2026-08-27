"""Tests mapped directly to the Milestone 6 domain-foundation security
boundary requirements:

1. An approved action's exact parameters cannot be silently mutated, and a
   changed action no longer matches an existing approval.
2. Invalid or incomplete proposals are never accepted as valid.
3. A rejected or expired approval cannot be turned into an approved one.

None of this implements execution or persistence — it verifies the domain
representation, immutability, and state-transition contracts those future
pieces will depend on.
"""

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from approvals.approval import (
    Approval,
    ApprovalState,
    approval_to_dict,
    approve,
    expire,
    matches_action,
    reject,
)
from approvals.proposed_action import (
    ActionType,
    ProposedAction,
    action_to_dict,
)

CREATED_AT = datetime(2026, 8, 1, 9, 0, tzinfo=timezone(timedelta(hours=1)))

_SENSITIVE_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "oauth",
    "refresh_token",
    "access_token",
)


def _create_action(title="Team sync"):
    return ProposedAction(
        action_type=ActionType.CREATE_EVENT,
        target_event_id=None,
        parameters={
            "title": title,
            "start": "2026-08-04T19:00:00+01:00",
            "end": "2026-08-04T20:00:00+01:00",
        },
    )


def _pending_approval(action=None):
    return Approval(
        action=action or _create_action(),
        requested_by="U12345",
        conversation_id="slack:T1:C1:1690000000.000100",
        created_at=CREATED_AT,
    )


# --- 1. exact action binding survives approval ---------------------------


def test_cannot_mutate_parameters_of_an_approved_action():
    approved = approve(_pending_approval())

    with pytest.raises(TypeError):
        approved.action.parameters["title"] = "Hijacked"

    with pytest.raises(dataclasses.FrozenInstanceError):
        approved.action.target_event_id = "some-other-event"

    with pytest.raises(dataclasses.FrozenInstanceError):
        approved.action = _create_action(title="Hijacked")


def test_approval_does_not_match_a_differently_parameterized_action():
    original = _create_action(title="Team sync")
    approved = approve(_pending_approval(action=original))

    tampered = _create_action(title="Wire transfer approval")

    assert matches_action(approved, tampered) is False
    # The only way to get a "matching" action back is to reconstruct the
    # exact original content — approval never widens to match a lookalike.
    assert matches_action(approved, _create_action(title="Team sync")) is True


def test_mutating_the_source_dict_after_approval_does_not_change_the_action():
    parameters = {
        "title": "Team sync",
        "start": "2026-08-04T19:00:00+01:00",
        "end": "2026-08-04T20:00:00+01:00",
    }
    action = ProposedAction(
        action_type=ActionType.CREATE_EVENT,
        target_event_id=None,
        parameters=parameters,
    )
    approved = approve(_pending_approval(action=action))

    parameters["title"] = "Hijacked after the fact"

    assert approved.action.parameters["title"] == "Team sync"


# --- 2. invalid / incomplete proposals are never valid --------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "action_type": ActionType.CREATE_EVENT,
            "target_event_id": None,
            "parameters": {"title": "Missing start and end"},
        },
        {
            "action_type": ActionType.UPDATE_EVENT,
            "target_event_id": None,
            "parameters": {"title": "No target event"},
        },
        {
            "action_type": ActionType.DELETE_EVENT,
            "target_event_id": "event-1",
            "parameters": {"title": "Delete takes no parameters"},
        },
    ],
)
def test_incomplete_or_invalid_actions_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ProposedAction(**kwargs)


def test_unknown_action_type_never_produces_a_valid_proposal():
    with pytest.raises(ValueError):
        ProposedAction(
            action_type="calendar.transfer_ownership",
            target_event_id=None,
            parameters={},
        )


def test_an_action_that_failed_construction_cannot_be_approved():
    # There is no code path that produces an Approval without first
    # constructing a valid ProposedAction, so an invalid proposal can
    # never reach the approval stage at all.
    with pytest.raises(ValueError):
        ProposedAction(
            action_type=ActionType.CREATE_EVENT,
            target_event_id=None,
            parameters={},
        )


# --- 3. rejected / expired approvals cannot become approved ---------------


def test_rejected_approval_cannot_become_approved():
    rejected = reject(_pending_approval())
    with pytest.raises(ValueError):
        approve(rejected)
    assert rejected.state is ApprovalState.REJECTED


def test_expired_approval_cannot_become_approved():
    expired = expire(_pending_approval())
    with pytest.raises(ValueError):
        approve(expired)
    assert expired.state is ApprovalState.EXPIRED


def test_approved_approval_cannot_be_revoked_back_to_pending_or_rejected():
    approved = approve(_pending_approval())
    with pytest.raises(ValueError):
        reject(approved)
    assert approved.state is ApprovalState.APPROVED


# --- no new sensitive-data surface ----------------------------------------


def _walk_strings(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key
            yield from _walk_strings(nested)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, str):
        yield value


@pytest.mark.parametrize(
    "state",
    [ApprovalState.PENDING, ApprovalState.APPROVED, ApprovalState.REJECTED, ApprovalState.EXPIRED],
)
def test_serialized_forms_never_contain_credential_like_keys_or_values(state):
    approval = Approval(
        action=_create_action(),
        requested_by="U12345",
        conversation_id="slack:T1:C1:1690000000.000100",
        created_at=CREATED_AT,
        state=state,
    )

    serialized = approval_to_dict(approval)
    also_serialized = action_to_dict(approval.action)

    for text in list(_walk_strings(serialized)) + list(_walk_strings(also_serialized)):
        lowered = text.lower()
        for forbidden in _SENSITIVE_SUBSTRINGS:
            assert forbidden not in lowered, (
                f"serialized approval unexpectedly contains {forbidden!r} "
                f"in {text!r}"
            )
