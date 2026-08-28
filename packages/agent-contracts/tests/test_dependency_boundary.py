"""Verifies packages/agent-contracts stays a standalone domain package: no
Slack, Hermes, MCP, HTTP client, or other framework-specific object can
cross the AgentRequest boundary, because the module has nothing to import
one from and every field holds only a plain built-in type.
"""

import ast
import sys
import tomllib
from pathlib import Path

import agent_contracts.agent_request as agent_request_module
from agent_contracts.agent_request import AgentRequest

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_declares_no_runtime_dependencies():
    pyproject = tomllib.loads((_PACKAGE_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == []


def test_agent_request_module_only_imports_from_the_standard_library():
    source = Path(agent_request_module.__file__).read_text()
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
