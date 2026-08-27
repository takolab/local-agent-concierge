import dataclasses

import pytest
from hypothesis import given, strategies as st

from approvals.proposed_action import (
    ActionType,
    ProposedAction,
    _ALLOWED_PARAMETERS,
    _REQUIRED_PARAMETERS,
    action_from_dict,
    action_to_dict,
    describe_action,
)


def _create_action(**overrides):
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


def _update_action(**overrides):
    defaults = {
        "action_type": ActionType.UPDATE_EVENT,
        "target_event_id": "event-123",
        "parameters": {"title": "Renamed sync"},
    }
    defaults.update(overrides)
    return ProposedAction(**defaults)


def _delete_action(**overrides):
    defaults = {
        "action_type": ActionType.DELETE_EVENT,
        "target_event_id": "event-123",
        "parameters": {},
    }
    defaults.update(overrides)
    return ProposedAction(**defaults)


# --- action type coverage -------------------------------------------------


def test_every_action_type_has_parameter_rules():
    for action_type in ActionType:
        assert action_type in _REQUIRED_PARAMETERS
        assert action_type in _ALLOWED_PARAMETERS


def test_unsupported_action_type_string_rejected():
    with pytest.raises(ValueError, match="Unsupported action type"):
        ProposedAction(
            action_type="calendar.wipe_everything",
            target_event_id=None,
            parameters={},
        )


def test_action_type_is_not_coerced_from_plain_string():
    # Only real ActionType members are accepted, even one whose value
    # matches a real member exactly, so nothing can bypass validation by
    # constructing ProposedAction directly instead of going through
    # action_from_dict / ActionType(...).
    with pytest.raises(ValueError, match="Unsupported action type"):
        ProposedAction(
            action_type="calendar.create_event",
            target_event_id=None,
            parameters={
                "title": "x",
                "start": "2026-08-04T19:00:00+01:00",
                "end": "2026-08-04T20:00:00+01:00",
            },
        )


# --- create_event -----------------------------------------------------


def test_create_event_valid_construction():
    action = _create_action()
    assert action.action_type is ActionType.CREATE_EVENT
    assert action.target_event_id is None
    assert action.parameters["title"] == "Team sync"


@pytest.mark.parametrize("missing", ["title", "start", "end"])
def test_create_event_requires_title_start_end(missing):
    parameters = {
        "title": "Team sync",
        "start": "2026-08-04T19:00:00+01:00",
        "end": "2026-08-04T20:00:00+01:00",
    }
    del parameters[missing]

    with pytest.raises(ValueError, match="Missing required parameters"):
        ProposedAction(
            action_type=ActionType.CREATE_EVENT,
            target_event_id=None,
            parameters=parameters,
        )


def test_create_event_rejects_unknown_parameters():
    with pytest.raises(ValueError, match="Unsupported parameters"):
        _create_action(parameters={"attendees": "a@example.invalid"})


def test_create_event_rejects_target_event_id():
    with pytest.raises(ValueError, match="target_event_id must be None"):
        _create_action(target_event_id="event-123")


def test_create_event_rejects_end_before_start():
    with pytest.raises(ValueError, match="end must be later than start"):
        _create_action(
            parameters={
                "start": "2026-08-04T20:00:00+01:00",
                "end": "2026-08-04T19:00:00+01:00",
            }
        )


def test_create_event_rejects_equal_start_and_end():
    with pytest.raises(ValueError, match="end must be later than start"):
        _create_action(
            parameters={
                "start": "2026-08-04T19:00:00+01:00",
                "end": "2026-08-04T19:00:00+01:00",
            }
        )


def test_create_event_rejects_naive_start():
    with pytest.raises(ValueError, match="must include time zone"):
        _create_action(parameters={"start": "2026-08-04T19:00:00"})


def test_create_event_rejects_unparseable_start():
    with pytest.raises(ValueError, match="valid ISO 8601"):
        _create_action(parameters={"start": "not-a-datetime"})


@pytest.mark.parametrize("blank_title", ["", "   "])
def test_create_event_rejects_blank_title(blank_title):
    with pytest.raises(ValueError, match="non-empty string"):
        _create_action(parameters={"title": blank_title})


def test_parameters_reject_non_string_values():
    with pytest.raises(ValueError, match="non-empty string"):
        _create_action(parameters={"title": 123})


def test_parameters_must_be_a_mapping():
    with pytest.raises(ValueError, match="must be a mapping"):
        ProposedAction(
            action_type=ActionType.CREATE_EVENT,
            target_event_id=None,
            parameters="not-a-mapping",
        )


def test_parameters_reject_non_string_keys():
    with pytest.raises(ValueError, match="parameter keys must be strings"):
        ProposedAction(
            action_type=ActionType.CREATE_EVENT,
            target_event_id=None,
            parameters={
                "title": "x",
                "start": "2026-08-04T19:00:00+01:00",
                "end": "2026-08-04T20:00:00+01:00",
                1: "unexpected",
            },
        )


# --- update_event -------------------------------------------------------


def test_update_event_valid_construction():
    action = _update_action()
    assert action.target_event_id == "event-123"
    assert action.parameters == {"title": "Renamed sync"}


def test_update_event_requires_target_event_id():
    with pytest.raises(
        ValueError, match="target_event_id is required"
    ):
        _update_action(target_event_id=None)


@pytest.mark.parametrize("blank_target", ["", "   "])
def test_update_event_rejects_blank_target_event_id(blank_target):
    with pytest.raises(ValueError, match="target_event_id is required"):
        _update_action(target_event_id=blank_target)


def test_update_event_requires_at_least_one_change():
    with pytest.raises(ValueError, match="at least one changed parameter"):
        _update_action(parameters={})


def test_update_event_rejects_unknown_parameters():
    with pytest.raises(ValueError, match="Unsupported parameters"):
        _update_action(parameters={"location": "Room 4"})


def test_update_event_allows_partial_start_only_change():
    action = _update_action(
        parameters={"start": "2026-08-04T19:30:00+01:00"}
    )
    assert action.parameters == {"start": "2026-08-04T19:30:00+01:00"}


def test_update_event_validates_ordering_when_both_present():
    with pytest.raises(ValueError, match="end must be later than start"):
        _update_action(
            parameters={
                "start": "2026-08-04T20:00:00+01:00",
                "end": "2026-08-04T19:00:00+01:00",
            }
        )


def test_update_event_skips_ordering_check_for_partial_change():
    # No pre-existing event data is available in this milestone, so a
    # partial update (only one of start/end) cannot be range-checked here.
    action = _update_action(
        parameters={"end": "2026-08-04T19:00:00+01:00"}
    )
    assert action.parameters == {"end": "2026-08-04T19:00:00+01:00"}


# --- delete_event ---------------------------------------------------------


def test_delete_event_valid_construction():
    action = _delete_action()
    assert action.target_event_id == "event-123"
    assert action.parameters == {}


def test_delete_event_requires_target_event_id():
    with pytest.raises(ValueError, match="target_event_id is required"):
        _delete_action(target_event_id=None)


def test_delete_event_rejects_any_parameters():
    with pytest.raises(ValueError, match="Unsupported parameters"):
        _delete_action(parameters={"title": "irrelevant"})


# --- immutability -----------------------------------------------------


def test_parameters_are_immutable():
    action = _create_action()
    with pytest.raises(TypeError):
        action.parameters["title"] = "Hijacked"


def test_original_dict_mutation_does_not_affect_action():
    original = {
        "title": "Team sync",
        "start": "2026-08-04T19:00:00+01:00",
        "end": "2026-08-04T20:00:00+01:00",
    }
    action = ProposedAction(
        action_type=ActionType.CREATE_EVENT,
        target_event_id=None,
        parameters=original,
    )

    original["title"] = "Mutated after construction"

    assert action.parameters["title"] == "Team sync"


def test_action_type_field_is_frozen():
    action = _create_action()
    with pytest.raises(dataclasses.FrozenInstanceError):
        action.action_type = ActionType.DELETE_EVENT


def test_target_event_id_field_is_frozen():
    action = _delete_action()
    with pytest.raises(dataclasses.FrozenInstanceError):
        action.target_event_id = "event-456"


# --- equality (the "exact action binding" contract) ---------------------
#
# ProposedAction is a frozen dataclass, so `==` is already a stable,
# process-independent, field-by-field structural comparison — this is what
# matches_action() in approval.py relies on. These tests exist to lock that
# contract in directly, independent of any one call site.


def test_separately_constructed_equal_actions_compare_equal():
    first = _create_action()
    second = _create_action()
    assert first == second


@pytest.mark.parametrize(
    "overrides",
    [
        {"parameters": {"title": "Different title"}},
        {"parameters": {"start": "2026-08-04T18:00:00+01:00"}},
        {"parameters": {"end": "2026-08-04T21:00:00+01:00"}},
    ],
)
def test_actions_with_different_parameters_are_not_equal(overrides):
    baseline = _create_action()
    changed = _create_action(**overrides)
    assert baseline != changed


def test_actions_with_different_action_type_are_not_equal():
    create = _create_action()
    delete = _delete_action()
    assert create != delete


def test_equality_is_independent_of_parameter_insertion_order():
    forward = ProposedAction(
        action_type=ActionType.CREATE_EVENT,
        target_event_id=None,
        parameters={
            "title": "Team sync",
            "start": "2026-08-04T19:00:00+01:00",
            "end": "2026-08-04T20:00:00+01:00",
        },
    )
    reversed_order = ProposedAction(
        action_type=ActionType.CREATE_EVENT,
        target_event_id=None,
        parameters={
            "end": "2026-08-04T20:00:00+01:00",
            "start": "2026-08-04T19:00:00+01:00",
            "title": "Team sync",
        },
    )

    assert forward == reversed_order


@given(
    title=st.text(min_size=1, max_size=50).filter(lambda s: s.strip()),
)
def test_equality_holds_for_a_range_of_generated_titles(title):
    first = _create_action(parameters={"title": title})
    second = _create_action(parameters={"title": title})
    assert first == second


# --- describe_action --------------------------------------------------


def test_describe_create_event():
    description = describe_action(_create_action())
    assert "Create calendar event 'Team sync'" in description
    assert "2026-08-04T19:00:00+01:00" in description
    assert "2026-08-04T20:00:00+01:00" in description


def test_describe_update_event():
    description = describe_action(_update_action())
    assert description == (
        "Update calendar event event-123 (title=Renamed sync)"
    )


def test_describe_delete_event():
    description = describe_action(_delete_action())
    assert description == "Delete calendar event event-123"


# --- serialization ------------------------------------------------------


@pytest.mark.parametrize(
    "action_factory",
    [_create_action, _update_action, _delete_action],
)
def test_action_dict_round_trip(action_factory):
    original = action_factory()
    restored = action_from_dict(action_to_dict(original))
    assert restored == original


def test_action_to_dict_has_exactly_the_expected_keys():
    data = action_to_dict(_create_action())
    assert set(data) == {"action_type", "target_event_id", "parameters"}
    assert data["action_type"] == "calendar.create_event"


def test_action_from_dict_rejects_unknown_fields():
    data = action_to_dict(_create_action())
    data["unexpected_field"] = "value"
    with pytest.raises(ValueError, match="Unsupported fields"):
        action_from_dict(data)


@pytest.mark.parametrize(
    "missing_key", ["action_type", "target_event_id", "parameters"]
)
def test_action_from_dict_rejects_missing_fields(missing_key):
    data = action_to_dict(_create_action())
    del data[missing_key]
    with pytest.raises(ValueError, match="Missing fields"):
        action_from_dict(data)


def test_action_from_dict_rejects_invalid_action_type():
    data = action_to_dict(_create_action())
    data["action_type"] = "calendar.wipe_everything"
    with pytest.raises(ValueError, match="Unsupported action type"):
        action_from_dict(data)


def test_action_from_dict_rejects_non_mapping():
    with pytest.raises(ValueError, match="must be a mapping"):
        action_from_dict("not-a-mapping")


def test_action_from_dict_rejects_non_mapping_parameters():
    data = action_to_dict(_create_action())
    data["parameters"] = "not-a-mapping"
    with pytest.raises(ValueError, match="parameters must be a mapping"):
        action_from_dict(data)
