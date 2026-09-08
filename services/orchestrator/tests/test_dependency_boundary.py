"""Verifies services/orchestrator stays within its declared dependency
boundary.

Until this slice that boundary was "exactly one runtime dependency
(agent-contracts), and nothing else imported anywhere". Adding trace
context propagation makes OpenTelemetry a real, necessary runtime
dependency, so pinning "standard library only" would now only pin a fact
that is no longer true rather than protect anything.

What is worth protecting -- and what these tests assert instead -- is
*where* that dependency is allowed to reach:

- The routing core (`agent`, `registry`, `orchestrator`) stays free of it
  entirely. `Orchestrator.dispatch()` is deliberately three lines of
  lookup-call-return (see docs/orchestrator/domain-model.md, "Why explicit
  -name routing"); telemetry lives at the transport boundaries that
  actually own an HTTP request, not in the domain logic. If a future
  change moves span handling into `dispatch()`, `TELEMETRY_FREE_MODULES`
  below fails.
- The transport/adapter modules (`http_server`, `hermes_agent`,
  `telemetry`, `__main__`) may additionally import `opentelemetry`, and
  nothing else.

Either way no *other* third-party or framework object (Slack, httpx, MCP,
a web framework, a Docker client, ...) may be imported into any of them,
which is the part of the original boundary that always mattered.
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
import orchestrator.telemetry as telemetry_module

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# Always allowed: the standard library, this package itself, and the one
# domain package this service is built on.
_BASELINE_ALLOWED = {"agent_contracts", "orchestrator"}

# Modules that must never reach for OpenTelemetry -- the routing core.
TELEMETRY_FREE_MODULES = [
    agent_module,
    registry_module,
    orchestrator_module,
    dev_agents_module,
]

# Modules that own an HTTP boundary, and so may also import OpenTelemetry.
TELEMETRY_ALLOWED_MODULES = [
    http_server_module,
    hermes_agent_module,
    telemetry_module,
    main_module,
]


def test_pyproject_declares_exactly_the_expected_dependencies():
    pyproject = tomllib.loads((_PACKAGE_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == [
        "local-agent-concierge-agent-contracts",
        "opentelemetry-api>=1.44.0,<2.0",
        "opentelemetry-exporter-otlp-proto-grpc>=1.44.0,<2.0",
        "opentelemetry-sdk>=1.44.0,<2.0",
    ]


def test_opentelemetry_pins_match_the_other_instrumented_services():
    """The three OpenTelemetry packages must be pinned to the same range
    apps/slack-gateway and mcp/google-calendar already use.

    All three services export to the same Collector, and OpenTelemetry's
    api/sdk/exporter packages are only guaranteed to work together within
    a release train -- letting one service drift to a different major
    range would be a real, silent interoperability risk, not a style
    difference.
    """
    sibling_paths = _sibling_pyprojects()
    if sibling_paths is None:
        pytest.skip(
            "sibling service pyproject.toml files are not present -- this "
            "runs inside the orchestrator container, whose build context "
            "copies only services/orchestrator. The check runs for real in "
            ".github/workflows/orchestrator.yml, which checks out the "
            "whole repository."
        )

    ours = _opentelemetry_requirements(_PACKAGE_ROOT / "pyproject.toml")
    assert ours, "services/orchestrator declares no OpenTelemetry dependency"

    for sibling_path in sibling_paths:
        theirs = _opentelemetry_requirements(sibling_path)
        shared = set(ours) & set(theirs)
        assert shared, f"{sibling_path} shares no OpenTelemetry package with ours"

        for package in sorted(shared):
            assert ours[package] == theirs[package], (
                f"{package} is pinned to {ours[package]!r} here but "
                f"{theirs[package]!r} in {sibling_path}"
            )


def _sibling_pyprojects() -> list[Path] | None:
    """The two other instrumented services' pyproject.toml files.

    `None` when this package is not sitting inside a full repository
    checkout, which is the case inside the orchestrator container.
    """
    for candidate in [_PACKAGE_ROOT, *_PACKAGE_ROOT.parents]:
        if not (candidate / "docker-compose.yml").is_file():
            continue

        paths = [
            candidate / "apps/slack-gateway/pyproject.toml",
            candidate / "mcp/google-calendar/pyproject.toml",
        ]
        return paths if all(path.is_file() for path in paths) else None

    return None


def _opentelemetry_requirements(pyproject_path: Path) -> dict[str, str]:
    pyproject = tomllib.loads(pyproject_path.read_text())
    requirements: dict[str, str] = {}

    for requirement in pyproject["project"]["dependencies"]:
        name, _, specifier = requirement.partition(">=")
        if name.startswith("opentelemetry-"):
            requirements[name] = ">=" + specifier

    return requirements


@pytest.mark.parametrize(
    "module",
    TELEMETRY_FREE_MODULES,
    ids=lambda module: module.__name__,
)
def test_routing_core_imports_no_third_party_code_at_all(module):
    unexpected = _non_stdlib_imports(module) - _BASELINE_ALLOWED
    assert not unexpected, (
        f"{module.__name__} must stay free of third-party imports "
        f"(including OpenTelemetry); found: {sorted(unexpected)}"
    )


@pytest.mark.parametrize(
    "module",
    TELEMETRY_ALLOWED_MODULES,
    ids=lambda module: module.__name__,
)
def test_transport_modules_import_only_agent_contracts_or_opentelemetry(module):
    allowed = _BASELINE_ALLOWED | {"opentelemetry"}
    unexpected = _non_stdlib_imports(module) - allowed
    assert not unexpected, f"Unexpected non-stdlib imports: {sorted(unexpected)}"


def _non_stdlib_imports(module) -> set[str]:
    source = Path(module.__file__).read_text()
    tree = ast.parse(source)

    imported_top_level_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_top_level_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported_top_level_modules.add(node.module.split(".")[0])

    return imported_top_level_modules - sys.stdlib_module_names
