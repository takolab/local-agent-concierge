# Orchestrator Domain Model

This document describes the first bounded implementation slice of
Milestone 7 (Containerized Concierge Orchestrator): `services/orchestrator`,
a minimal in-process routing skeleton, implemented in
`services/orchestrator/src/orchestrator`.

This slice proves that the existing Agent contracts
(`packages/agent-contracts`'s `AgentRequest` / `AgentResponse`) can be
connected through an `Orchestrator` implementation — nothing more. A
caller explicitly supplies an Agent name; the `Orchestrator` looks up that
Agent in an `AgentRegistry`, passes it an existing `AgentRequest`
unmodified, and returns its `AgentResponse` unchanged.

It does not implement request classification, automatic agent selection,
any transport, or any of the other Milestone 7 tasks — see "Deliberately
not implemented yet" below and `docs/roadmap.md` Milestone 7 for what
comes next.

## Where this lives, and why

`services/orchestrator` is a plain, in-process Python library package
(`src/` layout, `pyproject.toml`), not a running service yet: no
Dockerfile, no `docker-compose.yml` entry, no HTTP or other transport.
Nothing in this slice needs it to run anywhere on its own — it only needs
to be importable and tested, the same way `packages/agent-contracts` and
`packages/approvals` are today.

It lives under `services/` rather than `packages/` because
`docs/architecture.md`'s Repository Mapping already reserves
`services/orchestrator/` by name for "Agent routing and coordination," and
`docs/roadmap.md` Milestone 7 names it "Create the `services/orchestrator`
application." The name anticipates the future containerized service; this
slice is the first, bounded step toward it, not the service itself.

## `Agent`

A structural (`typing.Protocol`) contract, defined in
`services/orchestrator/src/orchestrator/agent.py`:

```python
class Agent(Protocol):
    def handle(self, request: AgentRequest) -> AgentResponse: ...
```

`AgentRequest` and `AgentResponse` are imported directly from
`agent_contracts.agent_request` / `agent_contracts.agent_response` —
this slice defines no new request or response schema. Because `Agent` is
a `Protocol`, any object with a matching `handle` method satisfies it
structurally; no base class or registration decorator is required. The
stub Agents under `services/orchestrator/tests/stub_agents.py` rely on
exactly this — they satisfy `Agent` without importing or inheriting from
it.

## `AgentRegistry`

A simple in-memory `dict[str, Agent]`, defined in
`services/orchestrator/src/orchestrator/registry.py`:

```python
class AgentRegistry:
    def register(self, name: str, agent: Agent) -> None: ...
    def get(self, name: str) -> Agent: ...
    def __contains__(self, name: object) -> bool: ...
```

- `register(name, agent)` requires `name` to be a non-empty string
  (`ValueError` otherwise) and raises `DuplicateAgentError` if `name` is
  already registered. On a duplicate, the existing registration is left
  untouched — the rejected call has no side effect.
- `get(name)` returns the registered Agent, or raises `UnknownAgentError`
  if `name` was never registered.
- Membership can be checked with `name in registry` (`__contains__`).

`DuplicateAgentError` and `UnknownAgentError` are plain `Exception`
subclasses defined locally in `registry.py` — no shared error hierarchy,
no error codes, nothing beyond what this package itself needs.

This registry deliberately does not implement discovery, persistence,
dynamic loading, configuration files, or network registration. It exists
only to make explicit-name dispatch (below) meaningful. Full "Implement
agent registration" — a separate item in `docs/roadmap.md` Milestone 7 —
is intentionally not fully addressed here; see "Relationship to the
Milestone 7 registration task" below.

## `Orchestrator.dispatch`

Defined in `services/orchestrator/src/orchestrator/orchestrator.py`:

```python
class Orchestrator:
    def __init__(self, registry: AgentRegistry) -> None: ...
    def dispatch(self, agent_name: str, request: AgentRequest) -> AgentResponse: ...
```

`dispatch` does exactly three things:

1. Looks up `agent_name` in the `AgentRegistry` (raising
   `UnknownAgentError` if it isn't registered).
2. Calls `agent.handle(request)`.
3. Returns that exact `AgentResponse`.

It deliberately does not modify the request, inspect `request.instruction`
for routing, wrap the response, catch or translate Agent exceptions,
retry, or combine results from multiple Agents. An exception raised by
`Agent.handle()` — including `UnknownAgentError` from an unregistered
name — propagates to the caller unchanged.

## Why explicit-name routing

This slice intentionally uses explicit name-based dispatch: the caller
decides which Agent handles a request, by name, up front. Request
classification and automatic agent selection — both separate
`docs/roadmap.md` Milestone 7 tasks — are not implemented here.

Explicit-name dispatch is the smallest possible foundation that still
proves the Agent contracts connect through a real `Orchestrator`: it
needs no classification model, no routing rules, and no decision about
*how* an instruction maps to an Agent. Because `dispatch(agent_name,
request)` takes the Agent name as a plain argument rather than deriving
it internally, later work can introduce a classifier that *computes*
`agent_name` and hands it to this same `dispatch` method — this slice
does not need to be redesigned, only called differently, when that
happens.

## Dependency on `agent-contracts`

`services/orchestrator` declares exactly one runtime dependency:
`local-agent-concierge-agent-contracts` (`packages/agent-contracts`),
reusing its `AgentRequest` / `AgentResponse` types rather than duplicating
them. `services/orchestrator/tests/test_dependency_boundary.py` enforces
this: `pyproject.toml` declares exactly that one dependency, and every
core module (`agent.py`, `registry.py`, `orchestrator.py`) imports only
from the standard library or `agent_contracts`.

### Installation

There is no private package index in this repository, so the local
dependency is installed by editable-installing `agent-contracts` first,
then `services/orchestrator`:

```bash
pip install -e packages/agent-contracts
pip install -e "services/orchestrator[test]"
```

`services/orchestrator/pyproject.toml` declares the dependency by plain
package name (`local-agent-concierge-agent-contracts`), with no `file://`
URL and no version pin. Verified from a clean Python 3.12 virtual
environment: the second install reports the requirement as
"already satisfied" against the editable install from the first step and
does not attempt to reach an index for it, so this two-step sequence is
reproducible without a `file://` dependency or a private package index.
The Orchestrator CI workflow (`.github/workflows/orchestrator.yml`)
installs in this same order.

## Current boundaries

- Dispatch is manual and explicit: the caller — not this slice — decides
  which Agent handles a request.
- Registration is a plain in-memory mapping, populated by direct
  `register()` calls from whatever constructs the `AgentRegistry`; nothing
  here loads Agents from configuration or discovers them automatically.
- `Orchestrator` and `AgentRegistry` are plain Python objects, used
  in-process. Nothing here runs as a service yet.

### Relationship to the Milestone 7 registration task

`docs/roadmap.md` Milestone 7 separately lists "Implement agent
registration" as its own task. This slice already includes a minimal
in-memory `AgentRegistry` — accepted as necessary scope overlap, because a
routing skeleton with no way to register Agents would not be meaningful to
test or reason about. This does not fully address that roadmap task:
`AgentRegistry` here is exactly a `name -> Agent` mapping with `register` /
`get` / membership-check, and deliberately nothing more (see
"Deliberately not implemented yet" below). Whether the roadmap task needs
anything beyond what already exists here is left for whichever future
task revisits Milestone 7's checkboxes — this document does not update
`docs/roadmap.md`.

## Deliberately not implemented yet

Out of scope for this slice, per its stated boundaries and
`docs/roadmap.md` Milestone 7's remaining tasks:

- HTTP, MCP, or any other transport.
- Docker.
- Docker Compose.
- Slack Gateway integration.
- Hermes Agent integration.
- Request classification.
- Automatic agent selection.
- Permission enforcement.
- Memory.
- Approval workflow.
- Multi-agent delegation.
- Result aggregation.
- Trace propagation.

Also out of scope: agent discovery, persistence, dynamic loading,
configuration-file-based registration, network registration, and any
change to `packages/agent-contracts` or `packages/approvals`.

## Open questions carried forward

This slice resolves none of the open questions already logged in
`docs/agent-contracts/domain-model.md`; they remain open:

- Whether `AgentRequest.trace_id` is only a logical correlation
  identifier, or is expected to correspond to the active OpenTelemetry
  trace ID (trace propagation, per the list above, is not implemented in
  this slice).
- Where an `AgentRequest.permissions` entry such as `calendar.read` is
  actually enforced — this slice does not enforce permissions anywhere;
  `dispatch` passes `request` through to `Agent.handle()` unexamined.
- The `AgentResponse.status` vocabulary — `dispatch` returns whatever
  `status` the Agent produced without inspecting or branching on it.

## Tests

`services/orchestrator/tests/`:

- `test_registry.py` — registering then retrieving an Agent, `register()`
  rejecting non-string/blank names, a duplicate name raising
  `DuplicateAgentError` while leaving the original registration in place,
  `get()` raising `UnknownAgentError` for an unregistered name, and
  membership checks via `in`.
- `test_orchestrator.py` — successful dispatch returning the exact
  `AgentResponse` instance the Agent produced, the exact `AgentRequest`
  instance being passed through to `Agent.handle()`, explicit-name
  selection between multiple registered Agents (confirming the
  non-selected Agent is never called), `UnknownAgentError` for an
  unregistered name, confirmation that no Agent is called when dispatch
  targets an unknown name, and an Agent-raised exception propagating out
  of `dispatch()` as the identical exception instance (not caught,
  wrapped, or translated).
- `test_dependency_boundary.py` — `pyproject.toml` declares exactly the
  one `local-agent-concierge-agent-contracts` dependency, and
  `agent.py` / `registry.py` / `orchestrator.py` import only from the
  standard library or `agent_contracts`.
- `stub_agents.py` — not a test module itself; the `RecordingAgent` /
  `ExplodingAgent` stub Agents shared by the two test modules above. Both
  exist only under `tests/` — there are no stub Agents under
  `services/orchestrator/src`.
