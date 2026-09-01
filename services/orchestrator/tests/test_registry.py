"""Tests for orchestrator.registry.AgentRegistry: registration, retrieval,
duplicate-name rejection without mutating the existing registration,
unknown-name rejection, membership checks, and the agent-name identifier
rule (non-empty, no leading/trailing whitespace).
"""

import pytest

from orchestrator.registry import AgentRegistry, DuplicateAgentError, UnknownAgentError

from stub_agents import RecordingAgent
from agent_contracts.agent_response import AgentResponse


def _agent() -> RecordingAgent:
    return RecordingAgent(AgentResponse(status="ok", summary="stub"))


def test_register_then_get_returns_the_same_agent():
    registry = AgentRegistry()
    agent = _agent()

    registry.register("concierge", agent)

    assert registry.get("concierge") is agent


@pytest.mark.parametrize("bad_name", ["", "   ", 123, None])
def test_register_rejects_non_empty_string_names(bad_name):
    registry = AgentRegistry()

    with pytest.raises(ValueError):
        registry.register(bad_name, _agent())

    assert bad_name not in registry


@pytest.mark.parametrize("padded_name", [" calendar", "calendar ", " calendar "])
def test_register_rejects_names_with_leading_or_trailing_whitespace(padded_name):
    registry = AgentRegistry()

    with pytest.raises(ValueError):
        registry.register(padded_name, _agent())

    assert padded_name not in registry


def test_padded_name_is_never_treated_as_equivalent_to_its_trimmed_form():
    registry = AgentRegistry()
    registry.register("calendar", _agent())

    with pytest.raises(ValueError):
        registry.register(" calendar ", _agent())

    with pytest.raises(UnknownAgentError):
        registry.get(" calendar ")

    assert " calendar " not in registry
    assert "calendar" in registry


def test_register_duplicate_name_raises_and_does_not_mutate_registration():
    registry = AgentRegistry()
    original = _agent()
    registry.register("concierge", original)

    with pytest.raises(DuplicateAgentError):
        registry.register("concierge", _agent())

    assert registry.get("concierge") is original


def test_get_unknown_name_raises():
    registry = AgentRegistry()

    with pytest.raises(UnknownAgentError):
        registry.get("missing")


def test_contains_reflects_registration_state():
    registry = AgentRegistry()

    assert "concierge" not in registry

    registry.register("concierge", _agent())

    assert "concierge" in registry
