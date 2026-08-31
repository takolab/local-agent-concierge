import dataclasses
import json
from types import MappingProxyType

import pytest
from hypothesis import given, strategies as st

from agent_contracts.agent_response import (
    AgentResponse,
    agent_response_from_dict,
    agent_response_to_dict,
)


def _make_response(**overrides):
    defaults = {
        "status": "needs_approval",
        "summary": "Tuesday from 19:00 to 21:00 is available.",
        "proposed_actions": [
            {
                "type": "calendar.create_event",
                "title": "Ollama study",
                "start": "2026-08-04T19:00:00+01:00",
                "end": "2026-08-04T21:00:00+01:00",
            }
        ],
        "memory_candidates": [{"content": "Prefers evening study sessions"}],
    }
    defaults.update(overrides)
    return AgentResponse(**defaults)


# --- construction: happy path -----------------------------------------------


def test_valid_agent_response_with_all_fields():
    response = _make_response()
    assert response.status == "needs_approval"
    assert response.summary == "Tuesday from 19:00 to 21:00 is available."
    assert response.proposed_actions == (
        MappingProxyType(
            {
                "type": "calendar.create_event",
                "title": "Ollama study",
                "start": "2026-08-04T19:00:00+01:00",
                "end": "2026-08-04T21:00:00+01:00",
            }
        ),
    )
    assert response.memory_candidates == (
        MappingProxyType({"content": "Prefers evening study sessions"}),
    )


def test_valid_agent_response_with_only_required_fields():
    response = AgentResponse(status="ok", summary="Done.")
    assert response.proposed_actions == ()
    assert response.memory_candidates == ()


def test_proposed_actions_and_memory_candidates_accept_a_tuple():
    response = _make_response(
        proposed_actions=({"type": "calendar.create_event"},),
        memory_candidates=({"content": "note"},),
    )
    assert response.proposed_actions == (MappingProxyType({"type": "calendar.create_event"}),)
    assert response.memory_candidates == (MappingProxyType({"content": "note"}),)


# --- status --------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_status_is_rejected(blank):
    with pytest.raises(ValueError, match="status must be a non-empty string"):
        _make_response(status=blank)


@pytest.mark.parametrize("bad_value", [None, 123, ["needs_approval"]])
def test_non_string_status_is_rejected(bad_value):
    with pytest.raises(ValueError, match="status must be a non-empty string"):
        _make_response(status=bad_value)


def test_status_does_not_enforce_a_fixed_vocabulary():
    # No enum exists yet anywhere in this repository for status values —
    # any non-empty string is accepted.
    response = _make_response(status="some_future_status")
    assert response.status == "some_future_status"


# --- summary ---------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_summary_is_rejected(blank):
    with pytest.raises(ValueError, match="summary must be a non-empty string"):
        _make_response(summary=blank)


@pytest.mark.parametrize("bad_value", [None, 123, ["a", "b"]])
def test_non_string_summary_is_rejected(bad_value):
    with pytest.raises(ValueError, match="summary must be a non-empty string"):
        _make_response(summary=bad_value)


# --- proposed_actions / memory_candidates: shared container validation -----------


@pytest.mark.parametrize("field", ["proposed_actions", "memory_candidates"])
def test_container_accepts_an_empty_list(field):
    response = _make_response(**{field: []})
    assert getattr(response, field) == ()


@pytest.mark.parametrize("field", ["proposed_actions", "memory_candidates"])
@pytest.mark.parametrize(
    "bad_value",
    [
        "calendar.create_event",  # bare string misread as outer collection
        {"type": "calendar.create_event"},  # single mapping misread as outer collection
        42,
        None,
    ],
)
def test_container_rejects_non_list_or_tuple(field, bad_value):
    with pytest.raises(ValueError, match=f"{field} must be a list or tuple"):
        _make_response(**{field: bad_value})


@pytest.mark.parametrize("field", ["proposed_actions", "memory_candidates"])
def test_container_rejects_non_mapping_entries(field):
    with pytest.raises(ValueError, match=f"{field} entry must be a mapping"):
        _make_response(**{field: ["not-a-mapping"]})


@pytest.mark.parametrize("field", ["proposed_actions", "memory_candidates"])
def test_container_rejects_entries_with_non_string_keys(field):
    with pytest.raises(ValueError, match=f"{field} entry key must be a non-empty string"):
        _make_response(**{field: [{1: "value"}]})


@pytest.mark.parametrize("field", ["proposed_actions", "memory_candidates"])
def test_container_rejects_entries_with_blank_keys(field):
    with pytest.raises(ValueError, match=f"{field} entry key must be a non-empty string"):
        _make_response(**{field: [{"   ": "value"}]})


@pytest.mark.parametrize("field", ["proposed_actions", "memory_candidates"])
@pytest.mark.parametrize(
    "value",
    [None, True, False, 0, -3, 3.14, "", "  "],
    ids=["none", "true", "false", "zero", "negative-int", "float", "empty-str", "blank-str"],
)
def test_container_accepts_json_compatible_scalar_values(field, value):
    # Entry values are opaque JSON-compatible data, not specifically
    # non-empty strings — unlike entry *keys*, which still must be
    # non-empty strings (see key tests above).
    response = _make_response(**{field: [{"v": value}]})
    assert getattr(response, field)[0]["v"] == value


@pytest.mark.parametrize("field", ["proposed_actions", "memory_candidates"])
def test_container_accepts_nested_list_and_mapping_values(field):
    response = _make_response(
        **{field: [{"tags": ["a", "b"], "detail": {"nested": "value"}}]}
    )
    entry = getattr(response, field)[0]
    assert entry["tags"] == ("a", "b")
    assert entry["detail"] == MappingProxyType({"nested": "value"})


@pytest.mark.parametrize("field", ["proposed_actions", "memory_candidates"])
def test_container_accepts_proposed_action_shaped_entry(field):
    # Regression test: packages.approvals.ProposedAction's own
    # action_to_dict() shape — a nullable target_event_id and a nested
    # parameters mapping — must fit here without agent-contracts taking a
    # dependency on packages/approvals (see PR #19 review discussion).
    action_shaped_entry = {
        "action_type": "calendar.create_event",
        "target_event_id": None,
        "parameters": {
            "title": "Ollama study",
            "start": "2026-08-04T19:00:00+01:00",
            "end": "2026-08-04T21:00:00+01:00",
        },
    }
    response = _make_response(**{field: [action_shaped_entry]})
    entry = getattr(response, field)[0]
    assert entry["target_event_id"] is None
    assert entry["parameters"] == MappingProxyType(action_shaped_entry["parameters"])


class _NotJsonCompatible:
    pass


@pytest.mark.parametrize("field", ["proposed_actions", "memory_candidates"])
@pytest.mark.parametrize(
    "bad_value",
    [{1, 2, 3}, b"bytes", _NotJsonCompatible()],
    ids=["set", "bytes", "custom-object"],
)
def test_container_rejects_non_json_compatible_values(field, bad_value):
    with pytest.raises(ValueError, match=f"{field} entry.*must be a JSON-compatible value"):
        _make_response(**{field: [{"v": bad_value}]})


@pytest.mark.parametrize("field", ["proposed_actions", "memory_candidates"])
def test_container_rejects_non_json_compatible_nested_values(field):
    with pytest.raises(ValueError, match=f"{field} entry.*must be a JSON-compatible value"):
        _make_response(**{field: [{"detail": {"bad": _NotJsonCompatible()}}]})


# --- immutability ------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("status", "other"),
        ("summary", "other"),
        ("proposed_actions", ()),
        ("memory_candidates", ()),
    ],
)
def test_fields_cannot_be_reassigned(field, value):
    response = _make_response()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(response, field, value)


def test_proposed_actions_tuple_cannot_be_item_assigned():
    response = _make_response()
    with pytest.raises(TypeError):
        response.proposed_actions[0] = {"type": "hijacked"}


def test_memory_candidates_tuple_cannot_be_item_assigned():
    response = _make_response()
    with pytest.raises(TypeError):
        response.memory_candidates[0] = {"content": "hijacked"}


def test_proposed_action_entry_mapping_cannot_be_item_assigned():
    response = _make_response()
    with pytest.raises(TypeError):
        response.proposed_actions[0]["type"] = "hijacked"


def test_nested_mapping_value_cannot_be_item_assigned():
    response = _make_response(
        proposed_actions=[{"detail": {"nested": "value"}}]
    )
    with pytest.raises(TypeError):
        response.proposed_actions[0]["detail"]["nested"] = "hijacked"


def test_nested_list_value_cannot_be_item_assigned():
    response = _make_response(proposed_actions=[{"tags": ["a", "b"]}])
    with pytest.raises(TypeError):
        response.proposed_actions[0]["tags"][0] = "hijacked"


# --- caller-owned mutable collections -----------------------------------------------


def test_mutating_source_list_after_construction_does_not_affect_response():
    actions = [{"type": "calendar.create_event"}]
    response = _make_response(proposed_actions=actions)

    actions.append({"type": "calendar.delete_event"})
    actions[0] = {"type": "hijacked"}

    assert response.proposed_actions == (MappingProxyType({"type": "calendar.create_event"}),)


def test_mutating_source_entry_dict_after_construction_does_not_affect_response():
    entry = {"type": "calendar.create_event"}
    response = _make_response(proposed_actions=[entry])

    entry["type"] = "hijacked"
    entry["extra"] = "also hijacked"

    assert response.proposed_actions == (MappingProxyType({"type": "calendar.create_event"}),)


def test_mutating_source_nested_dict_value_after_construction_does_not_affect_response():
    nested = {"nested": "value"}
    entry = {"detail": nested}
    response = _make_response(proposed_actions=[entry])

    nested["nested"] = "hijacked"
    entry["detail"] = {"replaced": "hijacked"}

    assert response.proposed_actions[0]["detail"] == MappingProxyType({"nested": "value"})


def test_mutating_source_nested_list_value_after_construction_does_not_affect_response():
    tags = ["a", "b"]
    entry = {"tags": tags}
    response = _make_response(proposed_actions=[entry])

    tags.append("c")
    tags[0] = "hijacked"

    assert response.proposed_actions[0]["tags"] == ("a", "b")


# --- serialization: to_dict --------------------------------------------------------


def test_to_dict_has_exactly_the_expected_keys():
    data = agent_response_to_dict(_make_response())
    assert set(data) == {"status", "summary", "proposed_actions", "memory_candidates"}


def test_to_dict_uses_plain_lists_and_dicts_not_tuples_or_mapping_proxies():
    data = agent_response_to_dict(_make_response())
    assert isinstance(data["proposed_actions"], list)
    assert all(type(entry) is dict for entry in data["proposed_actions"])
    assert isinstance(data["memory_candidates"], list)
    assert all(type(entry) is dict for entry in data["memory_candidates"])


def test_to_dict_recursively_converts_nested_values_to_plain_types():
    response = _make_response(
        proposed_actions=[
            {"target_event_id": None, "parameters": {"title": "x"}, "tags": ["a"]}
        ]
    )
    data = agent_response_to_dict(response)
    entry = data["proposed_actions"][0]
    assert type(entry) is dict
    assert type(entry["parameters"]) is dict
    assert type(entry["tags"]) is list
    assert entry["target_event_id"] is None


def test_to_dict_is_actually_json_serializable():
    data = agent_response_to_dict(_make_response())
    reloaded = json.loads(json.dumps(data))
    assert reloaded == data


# --- serialization: round trip -----------------------------------------------------


def test_round_trip_with_all_fields():
    original = _make_response()
    restored = agent_response_from_dict(agent_response_to_dict(original))
    assert restored == original


def test_round_trip_with_only_required_fields():
    original = AgentResponse(status="ok", summary="Done.")
    restored = agent_response_from_dict(agent_response_to_dict(original))
    assert restored == original


def test_round_trip_preserves_entry_and_key_order():
    original = _make_response(
        proposed_actions=[{"b": "2", "a": "1"}, {"seq": "second"}],
        memory_candidates=[{"z": "26", "y": "25"}, {"seq": "second"}],
    )
    restored = agent_response_from_dict(agent_response_to_dict(original))

    # Key order within each entry is preserved (not sorted).
    assert list(restored.proposed_actions[0].keys()) == ["b", "a"]
    assert list(restored.memory_candidates[0].keys()) == ["z", "y"]

    # Entry order within each list is preserved.
    assert [dict(entry) for entry in restored.proposed_actions] == [
        {"b": "2", "a": "1"},
        {"seq": "second"},
    ]
    assert [dict(entry) for entry in restored.memory_candidates] == [
        {"z": "26", "y": "25"},
        {"seq": "second"},
    ]


def test_round_trip_through_actual_json_text():
    original = _make_response()
    json_text = json.dumps(agent_response_to_dict(original))
    restored = agent_response_from_dict(json.loads(json_text))
    assert restored == original


def test_round_trip_with_nested_and_scalar_entry_values():
    original = _make_response(
        proposed_actions=[
            {
                "action_type": "calendar.create_event",
                "target_event_id": None,
                "parameters": {"title": "Ollama study"},
                "count": 2,
                "confirmed": False,
                "tags": ["study", "recurring"],
            }
        ],
    )
    json_text = json.dumps(agent_response_to_dict(original))
    restored = agent_response_from_dict(json.loads(json_text))
    assert restored == original


# --- serialization: missing / unknown / malformed fields ---------------------------


@pytest.mark.parametrize(
    "missing_key", ["status", "summary", "proposed_actions", "memory_candidates"]
)
def test_from_dict_rejects_missing_fields(missing_key):
    data = agent_response_to_dict(_make_response())
    del data[missing_key]
    with pytest.raises(ValueError, match="Missing fields"):
        agent_response_from_dict(data)


def test_from_dict_rejects_unknown_fields():
    data = agent_response_to_dict(_make_response())
    data["task_id"] = "task-123"
    with pytest.raises(ValueError, match="Unsupported fields"):
        agent_response_from_dict(data)


def test_from_dict_rejects_non_mapping():
    with pytest.raises(ValueError, match="must be a mapping"):
        agent_response_from_dict("not-a-mapping")


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("status", ""),
        ("status", 123),
        ("summary", "   "),
        ("proposed_actions", "calendar.create_event"),
        ("proposed_actions", {"type": "calendar.create_event"}),
        ("memory_candidates", ["not-a-mapping"]),
    ],
)
def test_from_dict_rejects_malformed_values(field, bad_value):
    data = agent_response_to_dict(_make_response())
    data[field] = bad_value
    with pytest.raises(ValueError):
        agent_response_from_dict(data)


# --- equality ------------------------------------------------------------------------


def test_separately_constructed_equal_responses_compare_equal():
    assert _make_response() == _make_response()


def test_responses_built_from_different_but_equal_entry_dicts_compare_equal():
    a = _make_response(proposed_actions=[{"type": "calendar.create_event"}])
    b = _make_response(proposed_actions=[dict({"type": "calendar.create_event"})])
    assert a == b


def test_responses_with_different_status_are_not_equal():
    assert _make_response(status="ok") != _make_response(status="needs_approval")


# --- property-based: round trip holds for generated valid responses ------------------


_non_blank_text = st.text(min_size=1, max_size=40).filter(lambda s: s.strip())

_json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1000, max_value=1000),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)
_json_value = st.recursive(
    _json_scalar,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(_non_blank_text, children, max_size=3),
    ),
    max_leaves=6,
)
_entry_mapping = st.dictionaries(_non_blank_text, _json_value, max_size=3)


@given(
    status=_non_blank_text,
    summary=_non_blank_text,
    proposed_actions=st.lists(_entry_mapping, max_size=3),
    memory_candidates=st.lists(_entry_mapping, max_size=3),
)
def test_round_trip_holds_for_generated_valid_responses(
    status, summary, proposed_actions, memory_candidates
):
    original = AgentResponse(
        status=status,
        summary=summary,
        proposed_actions=proposed_actions,
        memory_candidates=memory_candidates,
    )
    restored = agent_response_from_dict(agent_response_to_dict(original))
    assert restored == original
