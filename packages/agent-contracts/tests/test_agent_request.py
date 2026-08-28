import dataclasses
import json

import pytest
from hypothesis import given, strategies as st

from agent_contracts.agent_request import (
    AgentRequest,
    agent_request_from_dict,
    agent_request_to_dict,
)


def _make_request(**overrides):
    defaults = {
        "task_id": "task-123",
        "user_id": "user-123",
        "conversation_id": "slack-thread-456",
        "instruction": "Find a two-hour study slot next week",
        "memory_scopes": ["user:user-123", "project:ml-systems"],
        "permissions": ["calendar.read"],
        "trace_id": "trace-789",
    }
    defaults.update(overrides)
    return AgentRequest(**defaults)


# --- construction: happy path -----------------------------------------------


def test_valid_agent_request_with_all_fields():
    request = _make_request()
    assert request.task_id == "task-123"
    assert request.user_id == "user-123"
    assert request.conversation_id == "slack-thread-456"
    assert request.instruction == "Find a two-hour study slot next week"
    assert request.memory_scopes == ("user:user-123", "project:ml-systems")
    assert request.permissions == ("calendar.read",)
    assert request.trace_id == "trace-789"


def test_valid_agent_request_with_only_required_fields():
    request = AgentRequest(
        task_id="task-123",
        user_id="user-123",
        conversation_id="slack-thread-456",
        instruction="Find a two-hour study slot next week",
    )
    assert request.memory_scopes == ()
    assert request.permissions == ()
    assert request.trace_id is None


# --- required identifiers and instruction -----------------------------------


@pytest.mark.parametrize(
    "field", ["task_id", "user_id", "conversation_id", "instruction"]
)
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_required_fields_are_rejected(field, blank):
    with pytest.raises(ValueError, match="non-empty string"):
        _make_request(**{field: blank})


@pytest.mark.parametrize(
    "field", ["task_id", "user_id", "conversation_id", "instruction"]
)
@pytest.mark.parametrize("bad_value", [None, 123, ["not", "a", "string"]])
def test_non_string_required_fields_are_rejected(field, bad_value):
    with pytest.raises(ValueError, match="non-empty string"):
        _make_request(**{field: bad_value})


def test_blank_task_id_is_rejected():
    with pytest.raises(ValueError, match="task_id must be a non-empty string"):
        _make_request(task_id="")


def test_blank_user_id_is_rejected():
    with pytest.raises(ValueError, match="user_id must be a non-empty string"):
        _make_request(user_id="   ")


def test_blank_conversation_id_is_rejected():
    with pytest.raises(
        ValueError, match="conversation_id must be a non-empty string"
    ):
        _make_request(conversation_id="")


def test_blank_instruction_is_rejected():
    with pytest.raises(ValueError, match="instruction must be a non-empty string"):
        _make_request(instruction="   ")


# --- memory_scopes -----------------------------------------------------------


def test_memory_scopes_accepts_an_empty_list():
    request = _make_request(memory_scopes=[])
    assert request.memory_scopes == ()


def test_memory_scopes_accepts_a_tuple():
    request = _make_request(memory_scopes=("user:user-123",))
    assert request.memory_scopes == ("user:user-123",)


@pytest.mark.parametrize("blank", ["", "   "])
def test_memory_scopes_reject_blank_entries(blank):
    with pytest.raises(
        ValueError, match="memory_scopes entry must be a non-empty string"
    ):
        _make_request(memory_scopes=["user:user-123", blank])


def test_memory_scopes_reject_non_string_entries():
    with pytest.raises(
        ValueError, match="memory_scopes entry must be a non-empty string"
    ):
        _make_request(memory_scopes=["user:user-123", 42])


@pytest.mark.parametrize(
    "bad_value", ["user:user-123", {"user:user-123": True}, 42, None]
)
def test_memory_scopes_reject_non_list_or_tuple(bad_value):
    with pytest.raises(ValueError, match="memory_scopes must be a list or tuple"):
        _make_request(memory_scopes=bad_value)


# --- permissions ---------------------------------------------------------------


def test_permissions_accepts_an_empty_list():
    request = _make_request(permissions=[])
    assert request.permissions == ()


@pytest.mark.parametrize("blank", ["", "   "])
def test_permissions_reject_blank_entries(blank):
    with pytest.raises(
        ValueError, match="permissions entry must be a non-empty string"
    ):
        _make_request(permissions=["calendar.read", blank])


def test_permissions_reject_non_string_entries():
    with pytest.raises(
        ValueError, match="permissions entry must be a non-empty string"
    ):
        _make_request(permissions=["calendar.read", 1.5])


@pytest.mark.parametrize(
    "bad_value", ["calendar.read", {"calendar.read": True}, 42, None]
)
def test_permissions_reject_non_list_or_tuple(bad_value):
    with pytest.raises(ValueError, match="permissions must be a list or tuple"):
        _make_request(permissions=bad_value)


# --- trace_id ------------------------------------------------------------------


def test_trace_id_defaults_to_none():
    request = AgentRequest(
        task_id="task-123",
        user_id="user-123",
        conversation_id="slack-thread-456",
        instruction="Find a two-hour study slot next week",
    )
    assert request.trace_id is None


def test_trace_id_accepts_explicit_none():
    request = _make_request(trace_id=None)
    assert request.trace_id is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_trace_id_rejects_blank_string(blank):
    with pytest.raises(ValueError, match="trace_id must be a non-empty string"):
        _make_request(trace_id=blank)


def test_trace_id_rejects_non_string():
    with pytest.raises(ValueError, match="trace_id must be a non-empty string"):
        _make_request(trace_id=123)


# --- immutability ----------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("task_id", "other"),
        ("user_id", "other"),
        ("conversation_id", "other"),
        ("instruction", "other"),
        ("memory_scopes", ("other",)),
        ("permissions", ("other",)),
        ("trace_id", "other"),
    ],
)
def test_fields_cannot_be_reassigned(field, value):
    request = _make_request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(request, field, value)


def test_memory_scopes_tuple_cannot_be_item_assigned():
    request = _make_request()
    with pytest.raises(TypeError):
        request.memory_scopes[0] = "hijacked"


def test_permissions_tuple_cannot_be_item_assigned():
    request = _make_request()
    with pytest.raises(TypeError):
        request.permissions[0] = "hijacked"


# --- caller-owned mutable collections ---------------------------------------------


def test_mutating_source_memory_scopes_list_after_construction_does_not_affect_request():
    scopes = ["user:user-123"]
    request = _make_request(memory_scopes=scopes)

    scopes.append("project:ml-systems")
    scopes[0] = "hijacked"

    assert request.memory_scopes == ("user:user-123",)


def test_mutating_source_permissions_list_after_construction_does_not_affect_request():
    permissions = ["calendar.read"]
    request = _make_request(permissions=permissions)

    permissions.append("calendar.write")

    assert request.permissions == ("calendar.read",)


# --- serialization: to_dict ------------------------------------------------------


def test_to_dict_has_exactly_the_expected_keys():
    data = agent_request_to_dict(_make_request())
    assert set(data) == {
        "task_id",
        "user_id",
        "conversation_id",
        "instruction",
        "memory_scopes",
        "permissions",
        "trace_id",
    }


def test_to_dict_uses_lists_not_tuples_for_json_compatibility():
    data = agent_request_to_dict(_make_request())
    assert isinstance(data["memory_scopes"], list)
    assert isinstance(data["permissions"], list)


def test_to_dict_is_actually_json_serializable():
    data = agent_request_to_dict(_make_request())
    reloaded = json.loads(json.dumps(data))
    assert reloaded == data


# --- serialization: round trip ---------------------------------------------------


def test_round_trip_with_all_fields():
    original = _make_request()
    restored = agent_request_from_dict(agent_request_to_dict(original))
    assert restored == original


def test_round_trip_with_only_required_fields():
    original = AgentRequest(
        task_id="task-123",
        user_id="user-123",
        conversation_id="slack-thread-456",
        instruction="Find a two-hour study slot next week",
    )
    restored = agent_request_from_dict(agent_request_to_dict(original))
    assert restored == original


def test_round_trip_preserves_memory_scope_and_permission_order():
    original = _make_request(
        memory_scopes=["project:ml-systems", "user:user-123"],
        permissions=["calendar.write", "calendar.read"],
    )
    restored = agent_request_from_dict(agent_request_to_dict(original))
    assert restored.memory_scopes == ("project:ml-systems", "user:user-123")
    assert restored.permissions == ("calendar.write", "calendar.read")


def test_round_trip_through_actual_json_text():
    original = _make_request()
    json_text = json.dumps(agent_request_to_dict(original))
    restored = agent_request_from_dict(json.loads(json_text))
    assert restored == original


# --- serialization: missing / unknown / malformed fields --------------------------


@pytest.mark.parametrize(
    "missing_key",
    [
        "task_id",
        "user_id",
        "conversation_id",
        "instruction",
        "memory_scopes",
        "permissions",
        "trace_id",
    ],
)
def test_from_dict_rejects_missing_fields(missing_key):
    data = agent_request_to_dict(_make_request())
    del data[missing_key]
    with pytest.raises(ValueError, match="Missing fields"):
        agent_request_from_dict(data)


def test_from_dict_rejects_unknown_fields():
    data = agent_request_to_dict(_make_request())
    data["unexpected_field"] = "value"
    with pytest.raises(ValueError, match="Unsupported fields"):
        agent_request_from_dict(data)


def test_from_dict_rejects_non_mapping():
    with pytest.raises(ValueError, match="must be a mapping"):
        agent_request_from_dict("not-a-mapping")


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("task_id", 123),
        ("task_id", ""),
        ("memory_scopes", "user:user-123"),
        ("memory_scopes", {"user:user-123": True}),
        ("permissions", "calendar.read"),
        ("trace_id", 123),
    ],
)
def test_from_dict_rejects_malformed_values(field, bad_value):
    data = agent_request_to_dict(_make_request())
    data[field] = bad_value
    with pytest.raises(ValueError):
        agent_request_from_dict(data)


# --- equality (the basis the round-trip assertions above rely on) -----------------


def test_separately_constructed_equal_requests_compare_equal():
    assert _make_request() == _make_request()


def test_requests_with_different_instructions_are_not_equal():
    assert _make_request(instruction="a") != _make_request(instruction="b")


# --- property-based: round trip holds for generated valid requests ----------------


_non_blank_text = st.text(min_size=1, max_size=40).filter(lambda s: s.strip())


@given(
    task_id=_non_blank_text,
    user_id=_non_blank_text,
    conversation_id=_non_blank_text,
    instruction=_non_blank_text,
    memory_scopes=st.lists(_non_blank_text, max_size=5),
    permissions=st.lists(_non_blank_text, max_size=5),
)
def test_round_trip_holds_for_generated_valid_requests(
    task_id, user_id, conversation_id, instruction, memory_scopes, permissions
):
    original = AgentRequest(
        task_id=task_id,
        user_id=user_id,
        conversation_id=conversation_id,
        instruction=instruction,
        memory_scopes=memory_scopes,
        permissions=permissions,
    )
    restored = agent_request_from_dict(agent_request_to_dict(original))
    assert restored == original
