"""Verifies services/orchestrator stays within its declared dependency
boundary: exactly one runtime dependency (agent-contracts), and no other
third-party or framework object (Slack, HTTP, MCP, Docker, ...) imported
into the core modules.
"""

import ast
import sys
import tomllib
from pathlib import Path

import pytest

import orchestrator.__main__ as main_module
import orchestrator.agent as agent_module
import orchestrator.dev_agents as dev_agents_module
import orchestrator.hermes_agent as hermes_agent_module
import orchestrator.http_server as http_server_module
import orchestrator.orchestrator as orchestrator_module
import orchestrator.registry as registry_module

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_declares_exactly_the_agent_contracts_dependency():
    pyproject = tomllib.loads((_PACKAGE_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == [
        "local-agent-concierge-agent-contracts"
    ]


@pytest.mark.parametrize(
    "module",
    [
        agent_module,
        registry_module,
        orchestrator_module,
        http_server_module,
        dev_agents_module,
        hermes_agent_module,
        main_module,
    ],
    ids=[
        "agent",
        "registry",
        "orchestrator",
        "http_server",
        "dev_agents",
        "hermes_agent",
        "__main__",
    ],
)
def test_core_modules_only_import_stdlib_or_agent_contracts(module):
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
    unexpected = non_stdlib - {"agent_contracts", "orchestrator"}
    assert not unexpected, f"Unexpected non-stdlib imports: {sorted(unexpected)}"
