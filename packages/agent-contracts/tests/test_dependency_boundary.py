"""Verifies packages/agent-contracts stays a standalone domain package: no
Slack, Hermes, MCP, HTTP client, or other framework-specific object can
cross the AgentRequest / AgentResponse boundary.

For AgentRequest, every field holds only a plain built-in type. For
AgentResponse, `proposed_actions`/`memory_candidates` entries are
deliberately immutable `types.MappingProxyType` mappings rather than plain
`dict`s (see agent_response.py) — a stdlib type, but not a "plain built-in"
one — so its fields are checked against that shape instead. Either way, the
only types actually crossing the boundary in *serialized* form
(`agent_response_to_dict`) are plain JSON-compatible builtins, checked
separately below.
"""

import ast
import sys
import tomllib
from pathlib import Path
from types import MappingProxyType

import pytest

import agent_contracts.agent_request as agent_request_module
import agent_contracts.agent_response as agent_response_module
from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse, agent_response_to_dict

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_declares_no_runtime_dependencies():
    pyproject = tomllib.loads((_PACKAGE_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == []


@pytest.mark.parametrize(
    "module",
    [agent_request_module, agent_response_module],
    ids=["agent_request", "agent_response"],
)
def test_module_only_imports_from_the_standard_library(module):
    source = Path(module.__file__).read_text()
    tree = ast.parse(source)

    imported_top_level_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_top_level_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_top_level_modules.add(node.module.split(".")[0])

    non_stdlib = imported_top_level_modules - sys.stdlib_module_names
    assert not non_stdlib, f"Unexpected non-stdlib imports: {sorted(non_stdlib)}"


def test_agent_request_fields_are_plain_built_in_types():
    request = AgentRequest(
        task_id="task-123",
        user_id="user-123",
        conversation_id="slack-thread-456",
        instruction="Find a two-hour study slot next week",
        memory_scopes=["user:user-123"],
        permissions=["calendar.read"],
        trace_id="trace-789",
    )

    for value in (
        request.task_id,
        request.user_id,
        request.conversation_id,
        request.instruction,
        request.trace_id,
    ):
        assert isinstance(value, str)

    assert isinstance(request.memory_scopes, tuple)
    assert all(isinstance(scope, str) for scope in request.memory_scopes)
    assert isinstance(request.permissions, tuple)
    assert all(isinstance(permission, str) for permission in request.permissions)


_STDLIB_ENTRY_VALUE_TYPES = (str, int, float, bool, type(None), tuple, MappingProxyType)


def _assert_stdlib_only_value(value: object) -> None:
    assert isinstance(value, _STDLIB_ENTRY_VALUE_TYPES), (
        f"Non-stdlib type crossed the AgentResponse boundary: {type(value)!r}"
    )
    if isinstance(value, MappingProxyType):
        for key, item in value.items():
            assert isinstance(key, str)
            _assert_stdlib_only_value(item)
    elif isinstance(value, tuple):
        for item in value:
            _assert_stdlib_only_value(item)


def test_agent_response_fields_are_stdlib_only_types():
    # A nested/heterogeneous entry (mirroring a real
    # packages.approvals.ProposedAction's action_to_dict() shape) so the
    # recursive check below actually exercises int/float/bool/None/nested
    # tuple/mapping, not just flat strings.
    response = AgentResponse(
        status="needs_approval",
        summary="Tuesday from 19:00 to 21:00 is available.",
        proposed_actions=[
            {
                "action_type": "calendar.create_event",
                "target_event_id": None,
                "confirmed": False,
                "attempt": 1,
                "score": 0.5,
                "parameters": {"title": "Ollama study"},
                "tags": ["study", "recurring"],
            }
        ],
        memory_candidates=[{"content": "Prefers evening study sessions"}],
    )

    assert isinstance(response.status, str)
    assert isinstance(response.summary, str)

    for container in (response.proposed_actions, response.memory_candidates):
        assert isinstance(container, tuple)
        for entry in container:
            # A stdlib immutable mapping, not a plain dict — see module
            # docstring above.
            assert isinstance(entry, MappingProxyType)
            _assert_stdlib_only_value(entry)


def _assert_plain_json_compatible_type(value: object) -> None:
    if type(value) is dict:
        for key, item in value.items():
            assert type(key) is str
            _assert_plain_json_compatible_type(item)
    elif type(value) is list:
        for item in value:
            _assert_plain_json_compatible_type(item)
    elif type(value) in (str, int, float, bool, type(None)):
        pass
    else:
        raise AssertionError(f"Non-JSON-compatible type crossed the boundary: {type(value)!r}")


def test_agent_response_to_dict_output_is_plain_json_compatible_types():
    response = AgentResponse(
        status="needs_approval",
        summary="Tuesday from 19:00 to 21:00 is available.",
        proposed_actions=[
            {
                "action_type": "calendar.create_event",
                "target_event_id": None,
                "confirmed": False,
                "attempt": 1,
                "score": 0.5,
                "parameters": {"title": "Ollama study"},
                "tags": ["study", "recurring"],
            }
        ],
        memory_candidates=[{"content": "Prefers evening study sessions"}],
    )

    data = agent_response_to_dict(response)
    assert type(data) is dict
    _assert_plain_json_compatible_type(data)
