import dataclasses
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from approvals.approval import (
    Approval,
    ApprovalState,
    TERMINAL_STATES,
    _ALLOWED_TRANSITIONS,
    approval_from_dict,
    approval_to_dict,
    approve,
    expire,
    is_terminal,
    matches_action,
    reject,
    transition,
)
from approvals.proposed_action import ActionType, ProposedAction

CREATED_AT = datetime(2026, 8, 1, 9, 0, tzinfo=timezone(timedelta(hours=1)))

VALID_TRANSITIONS = {
    (ApprovalState.PENDING, ApprovalState.APPROVED),
    (ApprovalState.PENDING, ApprovalState.REJECTED),
    (ApprovalState.PENDING, ApprovalState.EXPIRED),
}


def _sample_action(**overrides):
    parameters = {
        "title": "Team sync",
        "start": "2026-08-04T19:00:00+01:00",
        "end": "2026-08-04T20:00:00+01:00",
    }
    parameters.update(overrides.pop("parameters", {}))
    defaults = {
        "action_type": ActionType.CREATE_EVENT,
        "target_event_id": None,
        "parameters": parameters,
    }
    defaults.update(overrides)
    return ProposedAction(**defaults)


def _sample_approval(**overrides):
    defaults = {
        "action": _sample_action(),
        "requested_by": "U12345",
        "conversation_id": "slack:T1:C1:1690000000.000100",
        "created_at": CREATED_AT,
    }
    defaults.update(overrides)
    return Approval(**defaults)


# --- construction / validation -----------------------------------------


def test_new_approval_defaults_to_pending():
    approval = _sample_approval()
    assert approval.state is ApprovalState.PENDING


def test_approval_rejects_non_proposed_action():
    with pytest.raises(ValueError, match="must be a ProposedAction"):
        _sample_approval(action={"action_type": "calendar.create_event"})


@pytest.mark.parametrize("blank", ["", "   "])
def test_approval_rejects_blank_requested_by(blank):
    with pytest.raises(ValueError, match="requested_by"):
        _sample_approval(requested_by=blank)


@pytest.mark.parametrize("blank", ["", "   "])
def test_approval_rejects_blank_conversation_id(blank):
    with pytest.raises(ValueError, match="conversation_id"):
        _sample_approval(conversation_id=blank)


def test_approval_rejects_naive_created_at():
    with pytest.raises(ValueError, match="time zone"):
        _sample_approval(created_at=datetime(2026, 8, 1, 9, 0))


def test_approval_rejects_non_datetime_created_at():
    with pytest.raises(ValueError, match="must be a datetime"):
        _sample_approval(created_at="2026-08-01T09:00:00+01:00")


def test_approval_rejects_foreign_state_value():
    with pytest.raises(ValueError, match="Unsupported approval state"):
        _sample_approval(state="approved")


# --- immutability -----------------------------------------------------


def test_approval_state_field_is_frozen():
    approval = _sample_approval()
    with pytest.raises(dataclasses.FrozenInstanceError):
        approval.state = ApprovalState.APPROVED


def test_approval_action_field_is_frozen():
    approval = _sample_approval()
    with pytest.raises(dataclasses.FrozenInstanceError):
        approval.action = _sample_action(parameters={"title": "Hijacked"})


def test_transition_returns_new_object_and_leaves_original_pending():
    pending = _sample_approval()
    approved = approve(pending)

    assert pending.state is ApprovalState.PENDING
    assert approved.state is ApprovalState.APPROVED
    assert approved is not pending


def test_transition_preserves_the_same_action_object():
    pending = _sample_approval()
    approved = approve(pending)
    assert approved.action is pending.action


# --- state coverage -----------------------------------------------------


def test_every_approval_state_has_a_transition_table_entry():
    for state in ApprovalState:
        assert state in _ALLOWED_TRANSITIONS


def test_terminal_states_are_exactly_approved_rejected_expired():
    assert TERMINAL_STATES == {
        ApprovalState.APPROVED,
        ApprovalState.REJECTED,
        ApprovalState.EXPIRED,
    }


@pytest.mark.parametrize("state", list(ApprovalState))
def test_is_terminal_matches_terminal_states_set(state):
    assert is_terminal(state) == (state in TERMINAL_STATES)


# --- exhaustive transition table -----------------------------------------


@pytest.mark.parametrize(
    "from_state,to_state",
    list(itertools.product(ApprovalState, ApprovalState)),
)
def test_transition_matrix(from_state, to_state):
    approval = _sample_approval(state=from_state)

    if (from_state, to_state) in VALID_TRANSITIONS:
        result = transition(approval, to_state)
        assert result.state is to_state
    else:
        with pytest.raises(ValueError, match="Cannot transition approval"):
            transition(approval, to_state)


def test_reject_then_approve_is_rejected():
    rejected = reject(_sample_approval())
    with pytest.raises(ValueError, match="Cannot transition approval"):
        approve(rejected)


def test_expire_then_approve_is_rejected():
    expired = expire(_sample_approval())
    with pytest.raises(ValueError, match="Cannot transition approval"):
        approve(expired)


def test_approve_then_reject_is_rejected():
    approved = approve(_sample_approval())
    with pytest.raises(ValueError, match="Cannot transition approval"):
        reject(approved)


def test_transition_rejects_foreign_target_state():
    with pytest.raises(ValueError, match="Unsupported approval state"):
        transition(_sample_approval(), "approved")


# --- matches_action -----------------------------------------------------


def test_matches_action_true_for_identical_action():
    approval = _sample_approval()
    same_action = _sample_action()
    assert matches_action(approval, same_action) is True


def test_matches_action_false_for_modified_parameter():
    approval = _sample_approval()
    modified = _sample_action(parameters={"title": "Different title"})
    assert matches_action(approval, modified) is False


def test_matches_action_false_for_different_target():
    # _sample_action() merges overrides onto create_event's default
    # parameters, which doesn't fit delete_event (no parameters allowed at
    # all), so both actions here are constructed directly instead.
    approval = _sample_approval(
        action=ProposedAction(
            action_type=ActionType.DELETE_EVENT,
            target_event_id="event-1",
            parameters={},
        )
    )
    different_target = ProposedAction(
        action_type=ActionType.DELETE_EVENT,
        target_event_id="event-2",
        parameters={},
    )
    assert matches_action(approval, different_target) is False


def test_matches_action_survives_approval_transition():
    approved = approve(_sample_approval())
    assert matches_action(approved, _sample_action()) is True


# --- serialization ------------------------------------------------------


@pytest.mark.parametrize(
    "state", [ApprovalState.PENDING, ApprovalState.APPROVED, ApprovalState.REJECTED, ApprovalState.EXPIRED]
)
def test_approval_dict_round_trip(state):
    original = _sample_approval(state=state)
    restored = approval_from_dict(approval_to_dict(original))
    assert restored == original


def test_approval_to_dict_has_exactly_the_expected_keys():
    data = approval_to_dict(_sample_approval())
    assert set(data) == {
        "action",
        "requested_by",
        "conversation_id",
        "created_at",
        "state",
    }


def test_approval_from_dict_rejects_unknown_fields():
    data = approval_to_dict(_sample_approval())
    data["unexpected_field"] = "value"
    with pytest.raises(ValueError, match="Unsupported fields"):
        approval_from_dict(data)


@pytest.mark.parametrize(
    "missing_key",
    ["action", "requested_by", "conversation_id", "created_at", "state"],
)
def test_approval_from_dict_rejects_missing_fields(missing_key):
    data = approval_to_dict(_sample_approval())
    del data[missing_key]
    with pytest.raises(ValueError, match="Missing fields"):
        approval_from_dict(data)


def test_approval_from_dict_rejects_invalid_state():
    data = approval_to_dict(_sample_approval())
    data["state"] = "revoked"
    with pytest.raises(ValueError, match="Unsupported approval state"):
        approval_from_dict(data)


def test_approval_from_dict_rejects_invalid_created_at():
    data = approval_to_dict(_sample_approval())
    data["created_at"] = "not-a-datetime"
    with pytest.raises(ValueError, match="valid ISO 8601"):
        approval_from_dict(data)


def test_approval_from_dict_rejects_non_mapping():
    with pytest.raises(ValueError, match="must be a mapping"):
        approval_from_dict("not-a-mapping")


def test_approval_from_dict_rejects_non_mapping_action():
    data = approval_to_dict(_sample_approval())
    data["action"] = "not-a-mapping"
    with pytest.raises(ValueError, match="action must be a mapping"):
        approval_from_dict(data)
