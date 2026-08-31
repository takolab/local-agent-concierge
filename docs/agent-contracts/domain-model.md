# Agent Contracts Domain Model

This document describes the domain foundation added for Milestone 7
(Containerized Concierge Orchestrator): the `AgentRequest` and
`AgentResponse` schemas, implemented in `packages/agent-contracts`.

`AgentRequest` was added first, as the **first, bounded** slice of
Milestone 7's "Define the common agent request schema" task
([#17](https://github.com/takolab/local-agent-concierge/pull/17)). It
defines only the normalized request shape a caller hands to an Agent
implementation.

`AgentResponse` is the next bounded slice: Milestone 7's "Define the
common agent response schema" task. **It is a provisional domain
representation** — the minimal schema matching the response already shown
in `docs/architecture.md`'s Agent Contract example, not a final or
permanent Agent Contract. `docs/architecture.md` itself says the exact
schema will evolve; this change deliberately does not anticipate that
evolution (see `AgentResponse`'s own section below for what it leaves
open).

Neither schema implements the Orchestrator service, agent routing, or any
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

## `AgentResponse`

An immutable, normalized response returned by an Agent implementation —
defined in
`packages/agent-contracts/src/agent_contracts/agent_response.py`.
**Provisional**: this is the minimal shape needed to match
`docs/architecture.md`'s existing response example, not a final schema.

```python
@dataclass(frozen=True)
class AgentResponse:
    status: str
    summary: str
    proposed_actions: tuple[Mapping[str, str], ...] = ()
    memory_candidates: tuple[Mapping[str, str], ...] = ()
```

This mirrors the normalized response example already shown in
`docs/architecture.md`'s Agent Contract section:

```json
{
  "status": "needs_approval",
  "summary": "Tuesday from 19:00 to 21:00 is available.",
  "proposed_actions": [
    {
      "type": "calendar.create_event",
      "title": "Ollama study",
      "start": "2026-08-04T19:00:00+01:00",
      "end": "2026-08-04T21:00:00+01:00"
    }
  ],
  "memory_candidates": []
}
```

### Fields

| Field | Required | Type | Notes |
|---|---|---|---|
| `status` | yes | non-empty `str` | Opaque status label. No enum vocabulary is enforced (e.g. `needs_approval` is not a hardcoded value) — none is defined anywhere in this repository yet. |
| `summary` | yes | non-empty `str` | Free-text summary. No length limit or content validation. |
| `proposed_actions` | no | tuple of string-to-string mappings | Defaults to `()`. Each entry is opaque: any mapping whose keys and values are all non-empty strings. Not `packages/approvals.ProposedAction` — see "Deliberately deferred" below. |
| `memory_candidates` | no | tuple of string-to-string mappings | Defaults to `()`. Same entry shape and opaqueness as `proposed_actions`. |

`proposed_actions` and `memory_candidates` are deliberately treated as
opaque data in this slice: the domain model only constrains the *container*
(must be a `list`/`tuple`, so a bare string or a single mapping can't be
silently misread as the outer collection) and each *entry* (must be a
mapping of non-empty `str` keys to non-empty `str` values). It does not
interpret `type`, `title`, or any other key inside an entry.

### Validation

Construction rejects, with `ValueError`:

- a blank or non-`str` `status` or `summary` (blank means empty or
  all-whitespace after `.strip()`);
- a `proposed_actions` or `memory_candidates` value that isn't a `list` or
  `tuple` (mirroring `AgentRequest`'s `memory_scopes`/`permissions` rule —
  a bare string or a single mapping is rejected rather than silently
  misread as the outer collection);
- any entry within `proposed_actions`/`memory_candidates` that isn't a
  mapping, or whose keys/values are blank or not `str`.

As with `AgentRequest`, every rule runs in `__post_init__`, so
construction either succeeds completely or raises.

### Immutability and defensive copy

`AgentResponse` is a frozen dataclass. `proposed_actions` and
`memory_candidates` are defensively copied — not just into a new outer
`tuple` (as `AgentRequest` does for `memory_scopes`/`permissions`), but
with each entry individually copied into a new `dict` and wrapped in a
read-only `types.MappingProxyType`. As a result:

- mutating a `list` a caller originally passed in for `proposed_actions`
  or `memory_candidates`, *or* mutating a `dict` inside that list, after
  constructing the response, does not change the stored response;
- `response.status = "..."` (or any other field) raises
  `dataclasses.FrozenInstanceError`;
- `response.proposed_actions[0] = {...}` raises `TypeError` (tuples have
  no item assignment);
- `response.proposed_actions[0]["type"] = "..."` raises `TypeError`
  (`MappingProxyType` has no item assignment).

There is no supported way to edit an `AgentResponse`, or anything inside
it, in place.

### Serialization

`agent_response_to_dict` / `agent_response_from_dict` convert to and from
a JSON-compatible `dict` (plain `str`, `list`, and `dict` values only — no
tuples, `MappingProxyType`, enums, or other non-JSON-native types):

```python
def agent_response_to_dict(response: AgentResponse) -> dict[str, Any]: ...
def agent_response_from_dict(data: Mapping[str, Any]) -> AgentResponse: ...
```

`agent_response_from_dict(agent_response_to_dict(response)) == response`
for every valid response, including entry order within
`proposed_actions`/`memory_candidates` and key order within each entry
(order is preserved, not sorted).

`agent_response_from_dict` rejects, with `ValueError`, rather than
silently ignoring or defaulting:

- a non-mapping input;
- a mapping missing any of the 4 fields (including `proposed_actions` /
  `memory_candidates`, even though the constructor itself defaults them —
  the same all-fields-required-in-serialized-form behavior as
  `agent_request_from_dict`; see open design question 3 under
  `AgentRequest` above, which this change does not resolve either);
- a mapping with any field beyond the 4 known ones;
- a mapping whose field values fail the same validation construction
  applies directly.

### Deliberately deferred for `AgentResponse`

Beyond the general "Deliberately not implemented yet" list below, this
slice specifically does not:

- add a `status` enum or hardcode any specific status value (e.g.
  `needs_approval`) — the vocabulary isn't decided anywhere in this
  repository yet;
- add any field beyond the 4 shown above — no `task_id`, `trace_id`,
  `error`, `agent_id`, or `metadata`; whether any of these are needed is
  left for whichever future task wires `AgentResponse` into the
  Orchestrator;
- depend on `packages/approvals.ProposedAction` for `proposed_actions`
  entries, or otherwise give `proposed_actions`/`memory_candidates` a
  richer, non-opaque shape;
- change `AgentRequest` in any way, or resolve any of its existing open
  design questions.

## Deliberately not implemented yet

Out of scope for this change, per Milestone 7's remaining tasks and this
task's stated boundaries:

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

### Open design questions for `AgentResponse`

Not resolved by this change; logged here rather than decided, per this
schema's provisional status:

4. **`status` vocabulary.** No enum exists yet. Once the Orchestrator and
   Slack Gateway need to branch on `status` (e.g. to render an approval
   prompt for `needs_approval`), the actual set of valid values needs an
   explicit decision — and, separately, whether that decision belongs in
   `agent-contracts` at all or in the consuming service.
5. **`proposed_actions` vs. `packages/approvals.ProposedAction`.** This
   slice keeps `proposed_actions` entries opaque string-to-string
   mappings, matching `docs/architecture.md`'s example exactly, and takes
   no dependency on `packages/approvals`. Whether a future `AgentResponse`
   should instead hold typed `ProposedAction` values (requiring
   `agent-contracts` to depend on `approvals`, or some translation layer
   between them) is the same open cross-package dependency question
   `docs/approval/domain-model.md` already logs for `AgentRequest` — still
   undecided, now relevant on the response side too.
6. **`memory_candidates` shape.** `docs/architecture.md`'s separate Memory
   Access Model shows a much richer memory record (`scope`, `source_agent`,
   `confidence`, `expires_at`, ...). Whether an `AgentResponse`'s
   `memory_candidates` entries should eventually mirror that shape, stay
   opaque, or become something else again is left for whenever the Memory
   Service is designed.
7. **Wire-schema optionality**, extended to `AgentResponse`: the same
   question logged as open design question 3 for `AgentRequest` above
   (whether omitted optional fields should be allowed in serialized form)
   applies equally to `AgentResponse`'s `proposed_actions` /
   `memory_candidates`, which also have constructor defaults but are
   currently required keys in `agent_response_from_dict`.

## Tests

`packages/agent-contracts/tests/` — 156 tests, 100% line coverage of
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
- `test_agent_response.py` — the same depth of coverage for
  `AgentResponse`: construction and validation for `status`/`summary` and
  for the `proposed_actions`/`memory_candidates` container and entry
  rules (parametrized across both fields, since the rule is identical),
  immutability (frozen-field reassignment, outer-tuple item assignment,
  *and* per-entry `MappingProxyType` item assignment), caller-owned
  mutable list *and* mutable entry-dict mutation after construction,
  `to_dict`/`from_dict` round-trips (including actual JSON text and both
  entry-order and key-order preservation), missing/unknown/malformed
  serialized field rejection, value-equality for separately constructed
  responses, and a Hypothesis property test for generated valid
  responses.
- `test_dependency_boundary.py` — the package's `pyproject.toml` declares
  zero runtime dependencies; both `agent_request.py` and
  `agent_response.py` import only from the Python standard library
  (checked via `ast` + `sys.stdlib_module_names`, not a hand-maintained
  list, parametrized over both modules); every `AgentRequest` field holds
  only a plain built-in type, while every `AgentResponse` field holds only
  a `str`, `tuple`, or `MappingProxyType` (a stdlib type, not a plain
  built-in — deliberate, since entries must be individually immutable);
  and `agent_response_to_dict`'s output is recursively checked to contain
  only plain `dict`/`list`/`str` types. Together these confirm no Slack,
  Hermes, MCP, or HTTP framework object can cross either boundary, and
  that `AgentResponse`'s richer internal representation still serializes
  to plain JSON-compatible types only.
