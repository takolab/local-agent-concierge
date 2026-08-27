# Proposed-Action and Approval Domain Model

This document describes the domain foundation added for Milestone 6
(Human Approval for Calendar Writes): the `ProposedAction` schema and the
`ApprovalState` / `Approval` lifecycle model, implemented in
`packages/approvals`.

This is deliberately the **first** slice of Milestone 6 — defining what a
proposed calendar write looks like and what states a human decision about
it can be in. It does not implement the Approval Service, Slack controls,
expiration, approval tokens, or calendar writes themselves. See
"Deliberately not implemented yet" below and `docs/roadmap.md` Milestone 6
for what comes next.

## Where this lives, and why

`packages/approvals` is a plain Python library package (`src/` layout,
`pyproject.toml`, no runtime dependencies), not a service: no Dockerfile,
no `docker-compose.yml` entry, nothing added to the containerized
architecture. Nothing in this milestone needs it to run anywhere — it only
needs to be importable and tested.

It lives at the repository root next to `apps/` and `mcp/`, rather than
inside `apps/slack-gateway` or `mcp/google-calendar`, because it is not
specific to either: `docs/architecture.md`'s Agent Contract already shows
`proposed_actions` as part of a normalized agent *response* (not a Slack
concept or a Calendar-API concept), and the future Approval Service,
Slack Gateway, and a calendar write tool will all need the same
`ProposedAction` / `Approval` shapes. Putting it inside either existing
service would make the other one import a sibling application's package
later, which is worse than a shared package both can depend on.

`packages/` matches the location `docs/architecture.md`'s Repository
Mapping already reserves for "Shared request and response schemas"
(`packages/agent-contracts/`). This package is deliberately narrower than
that: `packages/agent-contracts/` (task/conversation/permissions/memory
scopes) is Milestone 7 scope ("Define the common agent request schema" /
"...response schema"), not this milestone's. `packages/approvals` only
covers what Milestone 6 needs right now. A future Milestone 7 may have
`agent-contracts` depend on this package, or the two may stay separate —
that decision is out of scope here.

## `ProposedAction`

An exact, immutable description of one calendar write a user may approve —
defined in `packages/approvals/src/approvals/proposed_action.py`.

```python
@dataclass(frozen=True)
class ProposedAction:
    action_type: ActionType
    target_event_id: str | None
    parameters: Mapping[str, str]
```

### Supported action types

```python
class ActionType(StrEnum):
    CREATE_EVENT = "calendar.create_event"
    UPDATE_EVENT = "calendar.update_event"
    DELETE_EVENT = "calendar.delete_event"
```

These are exactly the three string values `docs/architecture.md`'s Agent
Contract example already uses (`"type": "calendar.create_event"`). A value
that is not one of these three is rejected — both when constructing a
`ProposedAction` directly and when deserializing one (see Validation).

### `target_event_id`

The identity of the *existing* event an action operates on:

| Action type | `target_event_id` |
|---|---|
| `calendar.create_event` | must be `None` — the event does not exist yet |
| `calendar.update_event` | required, non-empty |
| `calendar.delete_event` | required, non-empty |

### `parameters`

A flat string-to-string mapping. Every key must be a plain `str`; every
value must be a non-empty string once stripped.

| Action type | Allowed parameters | Required parameters |
|---|---|---|
| `calendar.create_event` | `title`, `start`, `end` | all three |
| `calendar.update_event` | `title`, `start`, `end` | at least one (a no-op update is rejected) |
| `calendar.delete_event` | none | none |

A parameter key outside the allowed set for that action type is rejected
— unrelated or unknown fields are never silently accepted.

`start` and `end` must each parse as an ISO 8601 date-time that includes
time zone information (`datetime.fromisoformat`, then a check that
`tzinfo`/`utcoffset()` is set) — the same rule `mcp/google-calendar`
already applies to calendar tool input
(`google_calendar_mcp.server._parse_datetime`). When both `start` and
`end` are present, `end` must be later than `start`. When only one of them
is present (a partial `update_event`), no ordering check is done — this
milestone has no calendar read access, so the other bound is unknown; that
remains semantic validation for whatever eventually calls the Calendar API
in a later Milestone 6 task.

Values are kept exactly as given (not reformatted through
`datetime.isoformat()`), so the stored value is always what was validated,
not a re-derived one.

### Immutability and exact-parameter preservation

`parameters` is defensively copied into a new `dict` and wrapped in
`types.MappingProxyType` in `__post_init__`, so:

- mutating the dict a caller originally passed in, after constructing a
  `ProposedAction`, does not change the stored action;
- `action.parameters["title"] = "..."` raises `TypeError`;
- `action.action_type = ...` / `action.target_event_id = ...` raise
  `dataclasses.FrozenInstanceError` (the dataclass itself is `frozen=True`).

There is no supported way to edit a `ProposedAction` in place. Producing a
"changed" action always means constructing a new one, and because
`ProposedAction` is a frozen dataclass, `==` is already an exact,
field-by-field structural comparison between two actions — this is the
basis `matches_action` (under `Approval` below) uses to answer "is this
the exact same proposed action that was approved?", and it is the concrete
answer this milestone gives to the roadmap's "Prevent modified actions
from reusing an old approval." An earlier version of this change computed
a separate SHA-256 content hash for that comparison instead; it was
removed as unjustified complexity once review established that `==`
already gives the same stable, cross-process-safe comparison `hash()`
alone would not (Python's per-process-randomized `hash()` affects
`hash()`/iteration order, not `==`, so nothing here ever needed to route
around it).

### `describe_action`

```python
def describe_action(action: ProposedAction) -> str
```

Returns a plain-text human-readable summary (e.g. `"Create calendar event
'Team sync' from 2026-08-04T19:00:00+01:00 to
2026-08-04T20:00:00+01:00"`), computed from the same three fields rather
than stored separately, so it can never drift out of sync with the action
it describes. This is the raw material a future Slack approval message
would render — this milestone does not build that message.

### Serialization

`action_to_dict` / `action_from_dict` convert to and from a plain,
JSON-compatible `dict` with exactly the keys `action_type`,
`target_event_id`, `parameters` — both directions reject unknown or
missing keys. `action_from_dict(action_to_dict(x)) == x` for every
supported action type (tested). This is the minimum needed for a future
Approval Service to receive/store/transmit a `ProposedAction`; no
persistence mechanism is implemented here.

## `Approval`

A record of a human decision request bound to one exact `ProposedAction` —
defined in `packages/approvals/src/approvals/approval.py`.

```python
@dataclass(frozen=True)
class Approval:
    action: ProposedAction
    requested_by: str
    conversation_id: str
    created_at: datetime
    state: ApprovalState = ApprovalState.PENDING
```

`requested_by` and `conversation_id` are opaque, non-empty strings — the
domain model does not know they usually come from Slack. `conversation_id`
is expected to carry the same shape `apps/slack-gateway` already builds
(`f"slack:{workspace_id}:{channel_id}:{root_thread_ts}"`), matching the
`conversation_id` field `docs/architecture.md`'s Agent Contract already
documents, but nothing here enforces that shape specifically. `created_at`
must be a time-zone-aware `datetime`.

This maps onto most of `docs/architecture.md`'s Human Approval binding
list — but not all of it:

| architecture.md says an approval should be bound to... | Field / mechanism |
|---|---|
| "The exact proposed action" / "The action parameters" | `Approval.action`, compared via `matches_action` (structural `==`) |
| "The requesting user" | `Approval.requested_by` |
| "An expiration time" | `Approval.created_at` exists as the anchor a future expiration rule needs; the expiration rule itself is not implemented (see below) |
| "The originating task" | **Not covered.** `Approval.conversation_id` is a distinct concept (the Slack conversation) from the `task_id` in `docs/architecture.md`'s own Agent Contract example — this repository has not formalized task identity anywhere yet (Milestone 7: "Define the common agent request schema"). A `task_id` field can be added to `Approval` once that exists. |

### `ApprovalState`

```python
class ApprovalState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
```

This is the four-state model `docs/roadmap.md` names for Milestone 6,
adopted as-is — it already satisfies every requirement this task has
(explicit states, one clear initial state, terminal states that can't be
escaped) without inventing something narrower or broader. No fifth
"executed" state was added: recording the result of an approved action is
a separate, later Milestone 6 task ("Record the result of approved
actions") that this foundation does not need to anticipate — `APPROVED`
means permission was granted, not that a write happened.

### Transitions

```text
                +-----------+
   (created) -> |  PENDING  |
                +-----------+
                 |     |     |
           approve  reject  expire
                 |     |     |
                 v     v     v
          +----------+ +----------+ +---------+
          | APPROVED | | REJECTED | | EXPIRED |
          +----------+ +----------+ +---------+
             (terminal)   (terminal)   (terminal)
```

`PENDING` is the only non-terminal state, with exactly the three outward
edges above. `APPROVED`, `REJECTED`, and `EXPIRED` have none — every
transition attempted out of them, including back to `PENDING` or to each
other, is rejected. This is enforced by one exhaustive table
(`_ALLOWED_TRANSITIONS`) and tested exhaustively: all 16 `(from, to)`
state pairs are parametrized, asserting the 3 valid ones succeed and the
other 13 (including same-state "transitions" like `APPROVED -> APPROVED`)
raise `ValueError`.

```python
def transition(approval: Approval, to_state: ApprovalState) -> Approval
def approve(approval: Approval) -> Approval
def reject(approval: Approval) -> Approval
def expire(approval: Approval) -> Approval
def is_terminal(state: ApprovalState) -> bool
```

Every transition function returns a **new** `Approval`; the original is
untouched (`Approval` is `frozen=True`, and `state = ...` raises
`FrozenInstanceError` directly, same as `ProposedAction`). This means
rejecting or expiring an approval can never be done by mutating an
in-flight object that other code might still hold a reference to — the
only way to change state is through `transition`/`approve`/`reject`/
`expire`, and those refuse anything the table doesn't allow.

### `matches_action`

```python
def matches_action(approval: Approval, action: ProposedAction) -> bool
```

Compares `approval.action == action` (see the equality note under
`ProposedAction` above). A future executor is expected to call this
immediately before performing a write and refuse to proceed when it
returns `False`. Tested to return `True` only for an action with identical
`action_type` / `target_event_id` / `parameters`, and `False` for any
single-field change (including after the approval has already transitioned
to `APPROVED`, so approving doesn't loosen the comparison).

### Serialization

`approval_to_dict` / `approval_from_dict` mirror the proposed-action pair:
a plain `dict` with exactly `action`, `requested_by`, `conversation_id`,
`created_at` (ISO 8601 string), `state`; both directions reject unknown or
missing keys, and an invalid `state` string is rejected via
`ApprovalState(...)`. Round-trips exactly for every state (tested).

## Deliberately not implemented yet

Everything below is explicitly out of scope for this change and remains
open Milestone 6 work (`docs/roadmap.md`):

- **Approval Service.** No service, container, HTTP API, or persistence.
  Everything above is in-memory Python objects.
- **Approval tokens / cryptographic signing.** `matches_action`'s equality
  check is not a signed or unforgeable token — nothing here is designed
  against an adversary who controls both the "approved" and "executed"
  ends of a comparison.
- **Persistence.** No database, no Redis, no file storage. An `Approval`
  only exists for as long as something in the same process holds it (or
  its serialized `dict`, if the caller stores that themselves).
- **Expiration.** `Approval.created_at` exists, and `EXPIRED` is a real
  state `transition`/`expire` can move an approval into, but nothing
  computes *when* an approval should expire or calls `expire()`
  automatically. There is no scheduler here.
- **Binding enforcement.** `Approval.requested_by` and `conversation_id`
  are stored, but nothing here checks that the Slack user clicking
  "Approve" matches `requested_by` — that check belongs to the future
  Slack interaction handler / Approval Service, once one exists.
- **Slack UI.** No message formatting, no interactive buttons, no
  approve/reject event handling.
- **Calendar writes.** No Google Calendar API calls of any kind. Executing
  an approved action is entirely future work.

## How a future Approval Service is expected to use this

Roughly, once it exists:

1. A Calendar Agent constructs a `ProposedAction` (validated by
   construction — an invalid one cannot exist) and wraps it in an
   `Approval` (`state` defaults to `PENDING`).
2. The Approval Service persists it (mechanism TBD) and the Slack Gateway
   renders `describe_action(approval.action)` with approve/reject
   controls, keyed to `approval.requested_by`.
3. On a Slack interaction, the service calls `approve(approval)` or
   `reject(approval)` — both already refuse to fire on anything but a
   `PENDING` approval — and persists the returned (new) `Approval`.
4. A scheduler (not implemented here) calls `expire(approval)` on
   approvals whose `created_at` is older than some future expiration
   policy.
5. Immediately before executing a write, the executor reconstructs the
   `ProposedAction` it is about to run and calls `matches_action(approval,
   that_action)`; a `False` result, or `approval.state is not
   ApprovalState.APPROVED`, means refuse to execute.

None of step 1–5's *service* logic is implemented here — only the data
shapes and rules steps 1, 3, and 5 depend on.

## Security assumptions and limitations

- This is a foundation, not an enforcement point. Nothing in this
  milestone stops anyone from constructing an `Approval` directly in
  `state=ApprovalState.APPROVED` and skipping `PENDING` entirely — there
  is no authorization check on who is allowed to call these functions, by
  design, since there is no caller yet. The contract this package
  guarantees is narrower and still meaningful: *given* a `PENDING`
  approval, it can only reach `APPROVED`/`REJECTED`/`EXPIRED` through the
  transition table, and can never leave a terminal state once there.
- `matches_action` detects any change to `action_type`, `target_event_id`,
  or `parameters`. It says nothing about `requested_by` or
  `conversation_id` — those are separate, intentionally independent
  binding facets per `docs/architecture.md` (see the mapping table above),
  and checking that the approving user matches `requested_by` is future
  work, not something `matches_action` does.
- No secrets or personal data were added to this domain model: no OAuth
  tokens, Google credentials, Slack tokens, or `Authorization` headers
  appear anywhere in `ProposedAction` or `Approval`, and this is checked
  by an automated test that walks every string in both objects' serialized
  form for credential-shaped substrings
  (`packages/approvals/tests/test_security_boundary.py`).
- `MappingProxyType` blocks direct mutation of `parameters` through the
  reference an `Approval`/`ProposedAction` exposes, but Python has no true
  deep immutability — code with a reference to the *original* dict before
  construction cannot affect an already-built action (tested), but this is
  a defensive-copy guarantee, not a language-level one.
- Values are validated as non-empty after `.strip()` but stored exactly as
  given, unstripped. Two otherwise-identical actions that differ only by
  insignificant leading/trailing whitespace in a value (e.g.
  `target_event_id="evt-1"` vs `"evt-1 "`) are therefore `!=`, and
  `matches_action` would report a mismatch between them. This is a
  deliberate consequence of preserving parameters exactly as validated
  rather than silently normalizing them, not an oversight, but it means a
  future caller that constructs a "comparison" action from user input
  should not introduce incidental whitespace differences from the
  original proposal.

## Tests

`packages/approvals/tests/` (128 tests, 100% line coverage of
`packages/approvals/src` at the time of this change):

- `test_proposed_action.py` — construction/validation for all three action
  types, immutability, the structural-equality contract (including a
  Hypothesis property test), `describe_action`, and
  `action_to_dict`/`action_from_dict` round-trips and rejection of
  unknown/missing fields.
- `test_approval.py` — construction/validation, the exhaustive 16-case
  transition matrix, `matches_action`, and `approval_to_dict`/
  `approval_from_dict` round-trips and rejection of unknown/missing
  fields/invalid state.
- `test_security_boundary.py` — the three properties named in this
  milestone's security boundary requirements directly: an approved
  action's parameters cannot be mutated and a changed action no longer
  matches its approval; invalid/incomplete proposals are never
  constructable; a rejected or expired approval cannot become approved.
  Plus the no-credential-data serialization check described above.

Run locally with:

```bash
pip install -e "packages/approvals[test]"
python -m pytest packages/approvals/tests
```

CI runs the same command (`.github/workflows/pytest.yml`).
