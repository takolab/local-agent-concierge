# AgentRequest Domain Model

This document describes the domain foundation added for Milestone 7
(Containerized Concierge Orchestrator): the `AgentRequest` schema,
implemented in `packages/agent-contracts`.

This is deliberately the **first, bounded** slice of Milestone 7's "Define
the common agent request schema" task. It defines only the normalized
request shape a caller hands to an Agent implementation. It does not
implement `AgentResponse`, the Orchestrator service, agent routing, or any
transport (HTTP, MCP, or otherwise). See "Deliberately not implemented
yet" below and `docs/roadmap.md` Milestone 7 for what comes next.

## Where this lives, and why

`packages/agent-contracts` is a plain Python library package (`src/`
layout, `pyproject.toml`, no runtime dependencies), not a service: no
Dockerfile, no `docker-compose.yml` entry, nothing added to the
containerized architecture or to any existing runtime (Slack Gateway,
Hermes Agent, Google Calendar MCP). Nothing in this milestone slice needs
it to run anywhere — it only needs to be importable and tested.

It lives at the repository root next to `apps/`, `mcp/`, and
`packages/approvals`, matching the location `docs/architecture.md`'s
Repository Mapping already reserves for it by name: "Shared request and
response schemas" (`packages/agent-contracts/`). It has no dependency on
`packages/approvals` (or vice versa) — the two packages cover different,
currently-unrelated parts of the Agent Contract, and `docs/approval/domain-model.md`
already noted that whether a future `agent-contracts` should depend on
`approvals` is an open decision left for whenever both are actually wired
together. This change does not make that decision.

## `AgentRequest`

An immutable, normalized request handed to an Agent implementation —
defined in `packages/agent-contracts/src/agent_contracts/agent_request.py`.

```python
@dataclass(frozen=True)
class AgentRequest:
    task_id: str
    user_id: str
    conversation_id: str
    instruction: str
    memory_scopes: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    trace_id: str | None = None
```

This mirrors the normalized request example already shown in
`docs/architecture.md`'s Agent Contract section:

```json
{
  "task_id": "task-123",
  "user_id": "user-123",
  "conversation_id": "slack-thread-456",
  "instruction": "Find a two-hour study slot next week",
  "memory_scopes": ["user:user-123", "project:ml-systems"],
  "permissions": ["calendar.read"],
  "trace_id": "trace-789"
}
```

### Fields

| Field | Required | Type | Notes |
|---|---|---|---|
| `task_id` | yes | non-empty `str` | Opaque identifier. No format is enforced (no UUID requirement) — none is defined anywhere in this repository yet. |
| `user_id` | yes | non-empty `str` | Opaque identifier. No identity/authorization semantics are attached here. |
| `conversation_id` | yes | non-empty `str` | Opaque identifier (e.g. a Slack thread id in the example above). No Slack-specific parsing or validation. |
| `instruction` | yes | non-empty `str` | Free-text instruction. No length limit or content validation. |
| `memory_scopes` | no | tuple of non-empty `str` | Defaults to `()`. No memory-scope grammar is enforced — each entry is just required to be a non-empty string. |
| `permissions` | no | tuple of non-empty `str` | Defaults to `()`. No permission taxonomy is enforced — same rule as `memory_scopes`. |
| `trace_id` | no | non-empty `str` or `None` | Defaults to `None`. No W3C Trace Context format is enforced. |

Deliberately absent: everything this schema doesn't need yet. In
particular, this is not a general-purpose validation framework — it does
not guess at rules the repository hasn't defined, such as identifier
formats, a memory-scope grammar, a permission taxonomy, or a trace id
format. Adding any of those is a separate future decision, not implied by
this change.

### Validation

Construction rejects, with `ValueError`:

- a blank or non-`str` `task_id`, `user_id`, `conversation_id`, or
  `instruction` (blank means empty or all-whitespace after `.strip()`);
- a `memory_scopes` or `permissions` value that isn't a `list` or `tuple`
  (a bare string is rejected rather than silently iterating into
  one-character entries; a `dict` is rejected rather than silently
  collapsing into just its keys);
- any `memory_scopes` or `permissions` entry that is blank or not a `str`;
- a `trace_id` that is present but blank or not a `str` (`None` is the way
  to omit it).

There is no code path that produces a partially-valid `AgentRequest` —
every rule above runs in `__post_init__`, so construction either succeeds
completely or raises.

### Immutability and exact-collection preservation

`AgentRequest` is a frozen dataclass, and `memory_scopes`/`permissions`
are defensively copied into new `tuple`s in `__post_init__`. As a result:

- mutating a `list` a caller originally passed in for `memory_scopes` or
  `permissions`, after constructing the request, does not change the
  stored request;
- `request.task_id = "..."` (or any other field) raises
  `dataclasses.FrozenInstanceError`;
- `request.memory_scopes[0] = "..."` raises `TypeError` (tuples have no
  item assignment).

There is no supported way to edit an `AgentRequest` in place.

### Serialization

`agent_request_to_dict` / `agent_request_from_dict` convert to and from a
JSON-compatible `dict` (plain `str`, `list`, and `None` values only — no
tuples, enums, or other non-JSON-native types):

```python
def agent_request_to_dict(request: AgentRequest) -> dict[str, Any]: ...
def agent_request_from_dict(data: Mapping[str, Any]) -> AgentRequest: ...
```

`agent_request_from_dict(agent_request_to_dict(request)) == request` for
every valid request, including insertion order of `memory_scopes` and
`permissions` entries (order is preserved, not sorted or deduplicated).

`agent_request_from_dict` rejects, with `ValueError`, rather than silently
ignoring or defaulting:

- a non-mapping input;
- a mapping missing any of the 7 fields;
- a mapping with any field beyond the 7 known ones;
- a mapping whose field values fail the same validation construction
  applies directly (e.g. a blank `task_id`, or `memory_scopes` given as a
  string instead of a list) — `agent_request_from_dict` builds an
  `AgentRequest` from the parsed fields, so it cannot accept anything the
  constructor itself would reject.

## Deliberately not implemented yet

Out of scope for this change, per Milestone 7's remaining tasks and this
task's stated boundaries:

- `AgentResponse` (the other half of the Agent Contract).
- The `services/orchestrator` application, its Dockerfile, or its
  `docker-compose.yml` entry.
- Agent registration, request classification, or agent selection.
- Any transport: HTTP API, MCP protocol changes, or otherwise.
- Any integration with the Slack Gateway, Hermes Agent, or Google
  Calendar MCP runtimes.
- Any change to `packages/approvals` or the Approval Service.
- Identifier formats (e.g. UUID), a W3C Trace Context format for
  `trace_id`, a memory-scope grammar, or a permission taxonomy — all
  remain unconstrained beyond "non-empty string" until this repository
  defines them somewhere.

## Open design questions for future Milestone 7 work

Raised during review of the PR that added this package
([#17](https://github.com/takolab/local-agent-concierge/pull/17)) as
non-blocking for this bounded schema, but worth deciding explicitly
before `AgentRequest` is wired into the Orchestrator or another runtime:

1. **`trace_id` vs. OpenTelemetry context.** Whether `trace_id` is only a
   logical correlation identifier, or is expected to correspond to the
   active OpenTelemetry trace ID. Distributed trace propagation should
   likely continue to rely on W3C Trace Context / `traceparent` rather
   than this field becoming the propagation mechanism itself.
2. **`permissions` enforcement boundary.** The schema stays opaque here
   by design; once the Orchestrator and tool integrations exist, where a
   permission like `calendar.read` is actually enforced (Orchestrator,
   agent adapter, tool service, or some combination) needs an explicit
   decision, so this field doesn't stay advisory-only metadata.
3. **Optional fields in serialized form.** `memory_scopes`, `permissions`,
   and `trace_id` have constructor defaults, but `agent_request_from_dict`
   currently requires all 7 fields to be present in the serialized form —
   internally consistent, but once this becomes an actual transport
   contract, whether the wire schema should keep requiring explicit `[]`
   / `null`, or should let omitted optional fields map to the constructor
   defaults, needs a decision.

None of these are resolved by this change; they're carried forward as
open questions for whichever future task wires `AgentRequest` into the
Orchestrator or another transport.

## Tests

`packages/agent-contracts/tests/` — 87 tests, 100% line coverage of
`packages/agent-contracts/src` (verified with `pytest-cov` locally; not
part of the CI step itself, matching `packages/approvals`' precedent).

- `test_agent_request.py` — construction and validation for every field
  (required-identifier and instruction blank/wrong-type rejection,
  `memory_scopes`/`permissions` entry and container-type rejection,
  `trace_id` optionality), immutability (frozen-field reassignment and
  tuple item assignment), caller-owned mutable list mutation after
  construction, `to_dict`/`from_dict` round-trips (including actual
  `json.dumps`/`json.loads` text and insertion-order preservation),
  missing/unknown/malformed serialized field rejection, and a Hypothesis
  property test that the round trip holds for generated valid requests.
- `test_dependency_boundary.py` — the package's `pyproject.toml` declares
  zero runtime dependencies, `agent_request.py` imports only from the
  Python standard library (checked via `ast` + `sys.stdlib_module_names`,
  not a hand-maintained list), and every `AgentRequest` field holds only a
  plain built-in type — so no Slack, Hermes, MCP, or HTTP framework object
  can cross this boundary.
