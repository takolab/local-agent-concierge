# Orchestrator Domain Model

This document describes `services/orchestrator`'s implementation slices
for Milestone 7 (Containerized Concierge Orchestrator), implemented in
`services/orchestrator/src/orchestrator`.

**Slice 1** ([PR #23](https://github.com/takolab/local-agent-concierge/pull/23))
proved that the existing Agent contracts (`packages/agent-contracts`'s
`AgentRequest` / `AgentResponse`) can be connected through an
`Orchestrator` implementation — nothing more. A caller explicitly
supplies an Agent name; the `Orchestrator` looks up that Agent in an
`AgentRegistry`, passes it an existing `AgentRequest` unmodified, and
returns its `AgentResponse` unchanged. This dispatch mechanism (`Agent`,
`AgentRegistry`, `Orchestrator.dispatch` below) is unchanged by Slice 2.

**Slice 2** turns that in-process library into a runnable, containerized
process with a minimal, provisional HTTP boundary (`GET /health`,
`POST /dispatch`), verified by a real Docker container health check and
real HTTP requests crossing the container boundary — not just in-process
Python calls. It deliberately does not connect to Hermes Agent or the
Slack Gateway. See "Runtime HTTP Boundary" below.

**Slice 3** registers the first real, non-synthetic Agent: `HermesAgent`,
which dispatches to the actual, running Hermes Agent service over its
existing `/v1/responses` HTTP API — the same API
`apps/slack-gateway/src/slack_gateway/hermes_client.py`'s `HermesClient`
already calls. It is registered alongside, not instead of, the still
-unchanged `dev-echo` `EchoAgent`. It deliberately does not connect to the
Slack Gateway, and does not implement classification or agent selection —
the caller still names the target Agent explicitly. See "HermesAgent
(Slice 3)" below. Slice 1 and Slice 2's dispatch/registry/HTTP-boundary
mechanism is unchanged by Slice 3.

None of the three slices implement request classification, automatic
agent selection, Slack Gateway integration, or any of the other
Milestone 7 tasks — see "Deliberately not implemented yet" below and
`docs/roadmap.md` Milestone 7 for what comes next.

## Where this lives, and why

`services/orchestrator` is a Python library package (`src/` layout,
`pyproject.toml`) that, as of Slice 2, also runs as a containerized
process: `services/orchestrator/Dockerfile` and `docker-compose.yml`'s
`orchestrator` / `orchestrator-test` services now exist (see "Docker /
Compose runtime" below). Slice 1's dispatch/registry/agent code
(`agent.py`, `registry.py`, `orchestrator.py`) is unchanged by Slice 2 —
Slice 2 adds a thin HTTP layer (`http_server.py`), a runtime entrypoint
(`__main__.py`), and a development-only synthetic Agent
(`dev_agents.py`) around it, not a rewrite of the dispatch mechanism
itself.

It lives under `services/` rather than `packages/` because
`docs/architecture.md`'s Repository Mapping already reserves
`services/orchestrator/` by name for "Agent routing and coordination," and
`docs/roadmap.md` Milestone 7 names it "Create the `services/orchestrator`
application." Slice 2 is the next bounded step toward the eventual
production containerized service, not the finished service — see
"Deliberately not implemented yet" below for what still doesn't exist.

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

- `register(name, agent)` requires `name` to be a non-empty string with no
  leading or trailing whitespace (`ValueError` otherwise — a padded name
  like `" calendar "` is rejected outright rather than silently becoming a
  key distinct from `"calendar"`, or silently normalized into the same
  key; see `test_padded_name_is_never_treated_as_equivalent_to_its_trimmed_form`)
  and raises `DuplicateAgentError` if `name` is already registered. On a
  duplicate, the existing registration is left untouched — the rejected
  call has no side effect.
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
retry, or combine results from multiple Agents. Two distinct failure
paths propagate to the caller unchanged, neither caught, translated, nor
retried: `UnknownAgentError`, raised by `AgentRegistry.get()` when
`agent_name` isn't registered (the Agent is never called in that case),
and any exception the Agent itself raises from `handle()`.

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

**This is a temporary monorepo convention, not a long-term dependency
standard.** Because the dependency is declared by plain package name with
no index behind it, install order is load-bearing: running `pip install
-e "services/orchestrator[test]"` in an environment that does not already
have `agent-contracts` installed would have pip search the configured
index (PyPI by default) for `local-agent-concierge-agent-contracts` and
fail, since that name is not published anywhere. This works today only
because every install path that matters — this doc, the CI workflow, and
(as of Slice 2) `services/orchestrator/Dockerfile` — installs
`agent-contracts` first, into the same environment, before
`services/orchestrator`.

**Resolved for the containerized path in Slice 2, as anticipated above.**
`services/orchestrator/Dockerfile` builds from the *repository root* as
its Docker build context (`docker-compose.yml`'s `orchestrator` /
`orchestrator-test` services set `context: .` and
`dockerfile: services/orchestrator/Dockerfile`, unlike
`apps/slack-gateway` or `mcp/google-calendar`, which build from their own
subdirectory) specifically so it can `COPY packages/agent-contracts` and
`pip install` it before installing `services/orchestrator` itself —
reproducing this same two-step order as a Dockerfile build step rather
than two separate `pip install` invocations. This is still not a private
package index or a lock file; it is the "Dockerfile-controlled build
order" option named above, not a long-term dependency standard.

## Current boundaries

- Dispatch is manual and explicit: the caller — not this slice — decides
  which Agent handles a request.
- Registration is a plain in-memory mapping, populated by direct
  `register()` calls from whatever constructs the `AgentRegistry`; nothing
  here loads Agents from configuration or discovers them automatically.
- `Orchestrator` and `AgentRegistry` are plain Python objects, used
  in-process — this is unchanged by Slice 2. What changed is that
  something now constructs and runs them as a service: as of Slice 2,
  `orchestrator.__main__.build_orchestrator()` builds this same
  `Orchestrator`/`AgentRegistry` pair and serves it over the minimal HTTP
  boundary described in "Runtime HTTP Boundary" below.

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

## Runtime HTTP Boundary (Slice 2)

Slice 2 adds a minimal, provisional HTTP transport around Slice 1's
unchanged `Orchestrator.dispatch()`:

```text
HTTP request -> AgentRequest deserialization -> Orchestrator.dispatch()
-> AgentResponse serialization -> HTTP response
```

Implemented in `services/orchestrator/src/orchestrator/http_server.py`
using Python's standard library `http.server`
(`ThreadingHTTPServer` + `BaseHTTPRequestHandler`) — **no new runtime
dependency**. Two routes only; any other path returns `404`, and a known
path called with the wrong HTTP method is not specially handled beyond
that (out of scope for this minimal a surface). `services/orchestrator`
still declares exactly the one `local-agent-concierge-agent-contracts`
runtime dependency it declared after Slice 1 —
`test_dependency_boundary.py` now also parametrizes over `http_server`,
`dev_agents`, and `__main__` to confirm they, too, import only the
standard library, `agent_contracts`, or `orchestrator`'s own modules.

**Why the standard library instead of a framework.** The surface is two
routes with simple JSON in/out. A hand-rolled handler needs a small,
bounded amount of code to get JSON parsing, routing, and — most
importantly — exception-to-4xx/5xx translation right (see "No leaked
internal detail" below), but none of that is unnatural or contorted at
this size, and it keeps the dependency footprint at exactly what Slice 1
already established (one runtime dependency, `agent-contracts`) rather
than adding a web framework for two routes. This is a deliberate choice
for *this* minimal slice, not a standing rule for whatever the
Orchestrator's HTTP surface eventually grows into.

### `GET /health`

Liveness only. Always `200 {"status": "ok"}` if the HTTP process is
running. No business logic, no registry inspection, no Agent
connectivity check — deliberately, per this slice's own scope.

### `POST /dispatch`

Request body:

```json
{
  "agent_name": "dev-echo",
  "request": {
    "task_id": "task-1",
    "user_id": "user-1",
    "conversation_id": "conversation-1",
    "instruction": "do something",
    "memory_scopes": [],
    "permissions": [],
    "trace_id": null
  }
}
```

`request` must include all 7 `AgentRequest` fields explicitly (including
`memory_scopes: []`, `permissions: []`, `trace_id: null` when unused) —
`agent_request_from_dict` (Slice 1's existing deserializer, reused
unchanged) requires every field to be present in the serialized form; see
open design question 3 in `docs/agent-contracts/domain-model.md`, which
this slice does not resolve.

Response bodies and status codes:

| Condition | Status | Body |
|---|---|---|
| Known Agent, valid request | `200` | `agent_response_to_dict(response)` — the Agent's exact `AgentResponse`, unwrapped |
| Unknown `agent_name` | `404` | `{"error": "unknown_agent", "detail": "No agent is registered under '<name>'."}` |
| Body is not valid JSON | `400` | `{"error": "invalid_json", "detail": "..."}` |
| Body is valid JSON but not `{"agent_name": str, "request": object}`, or `request` fails `AgentRequest` validation | `400` | `{"error": "invalid_request", "detail": "..."}` (`detail` may echo the underlying `ValueError` message from `agent_contracts` — a field-validation message, e.g. `"task_id must be a non-empty string"`, never an internal exception or traceback) |
| Unknown path | `404` | `{"error": "not_found", "detail": "..."}` |
| Anything else unexpected (e.g. an Agent's `handle()` raises) | `500` | `{"error": "internal_error", "detail": "An unexpected error occurred while dispatching the request."}` |

This exact request/response shape, and these status codes, are
**provisional** — a deliberately minimal contract sized for this slice's
proof target, not a stable or versioned public API. Expect it to change
once Hermes Agent integration, classification, or Slack Gateway
connectivity are designed.

**No leaked internal detail.** Every code path in `do_GET/do_POST` is
wrapped so that an unexpected exception (anything not already translated
into one of the defined 4xx cases above) is logged in full server-side
via `logging.exception(...)`, but the HTTP response always stays the
generic, bounded `internal_error` body above — never the exception's
message, its type, or a traceback. `test_http_server.py`'s
`test_dispatch_agent_exception_returns_500_without_leaking_internal_details`
asserts this directly (an Agent that raises with a distinctive message;
the response body is asserted not to contain that message, `"Traceback"`,
or the exception's type name), and also asserts the server is still
responsive (`GET /health` still returns `200`) immediately afterward —
one bad dispatch must not crash or hang the runtime process.

### Serialization flow

No new domain schema. `POST /dispatch` reuses Slice 1's existing
`agent_contracts` (de)serializers unchanged: `agent_request_from_dict`
for the incoming `request` object, `agent_response_to_dict` for the
outgoing body. `AgentRequest` / `AgentResponse` themselves, and their
validation rules, are exactly as documented in
`docs/agent-contracts/domain-model.md` — this slice does not touch
`packages/agent-contracts`.

### Synthetic Agent

`services/orchestrator/src/orchestrator/dev_agents.py` defines
`EchoAgent`, registered by `orchestrator.__main__` under the name
`"dev-echo"` — deliberately not `"hermes"`, `"calendar"`, or any name
that could be mistaken for a real domain Agent. `EchoAgent.handle()`
deterministically returns
`AgentResponse(status="completed", summary=f"echo: {request.instruction}")`
with no reasoning, no model call, and no side effects.

**This is a development/smoke-test fixture, not a production Agent.** It
exists only so the HTTP -> `Orchestrator.dispatch()` -> Agent contract
path has something real to dispatch to when the container starts — for
the Docker health check, the CI runtime smoke test, and optional manual
verification. `orchestrator.__main__.build_orchestrator()` hardcodes
this one registration; there is no configuration file, discovery
mechanism, or environment-variable-driven registration. A production
Agent registration mechanism (e.g. a `HermesAgent` adapter wrapping
Hermes Agent's existing `/v1/responses` API, the way
`apps/slack-gateway/src/slack_gateway/hermes_client.py` already does) is
a separate, later task and is not designed here.

### Docker / Compose runtime

`services/orchestrator/Dockerfile` follows the same
`base -> test -> runtime` multi-stage pattern as
`apps/slack-gateway/Dockerfile` and `mcp/google-calendar/Dockerfile`,
with one deliberate difference: its Docker build **context is the
repository root**, not `services/orchestrator/`, so it can install
`packages/agent-contracts` before `services/orchestrator` itself — see
"Resolved for the containerized path in Slice 2" above. The `runtime`
stage's `CMD` actually starts the HTTP process (`python -m orchestrator`)
— it is not a placeholder or no-op container.

`docker-compose.yml` adds:

- `orchestrator` — the runtime container. Published to the host as
  `127.0.0.1:8700:8700` (unlike `hermes-agent` or `google-calendar-mcp`,
  which are internal-only, reached only by sibling containers today).
  This slice publishes the port deliberately, so both the CI runtime
  smoke test and an optional human `curl` can reach it directly from
  outside the Compose network, matching the human-observable-boundary
  goal of this slice. A `healthcheck` polls `GET /health` via
  `urllib.request` (the same pattern already used by the `phoenix` and
  `mlflow` services). Not depended on by, and does not depend on, any
  other service yet.
- `orchestrator-test` — `profiles: [test]`, builds the `test` stage,
  runs `services/orchestrator/tests` inside the container (same shape as
  `slack-gateway-test` / `google-calendar-mcp-test`).

`.github/workflows/pytest.yml` runs `orchestrator-test`, then separately
starts the real `orchestrator` runtime container
(`docker compose up -d --wait --wait-timeout 60 orchestrator`) and drives
it with real `curl` requests over the published port — `GET /health`, a
known-Agent `POST /dispatch`, and an unknown-Agent `POST /dispatch` —
asserting both status codes and response bodies, before tearing the
container down. This is deliberately not just an in-process test client:
it is the automated evidence that the HTTP <-> Orchestrator boundary
holds across the real container boundary, not only inside a single
Python process. `.github/workflows/orchestrator.yml` (the fast,
Docker-free `pip install` + `pytest` path) is unchanged and still runs
the full `services/orchestrator/tests` suite, including
`test_http_server.py`, entirely in-process.

### Authorization boundary — carried over from PR #23 review

[PR #23](https://github.com/takolab/local-agent-concierge/pull/23)'s
review flagged, non-blocking, that the dispatch layer's silent pass-through
of `AgentRequest.permissions` should be documented explicitly as *not*
an authorization boundary before the Orchestrator is wired into a real
runtime. Slice 2 is that runtime wiring, so this is now recorded
explicitly: **neither `Orchestrator.dispatch()` nor the `POST /dispatch`
HTTP layer added in Slice 2 is an authorization boundary.**
`AgentRequest.permissions` continues to be passed through unexamined —
Slice 2 does not inspect, enforce, or validate it. The presence of a
`permissions` entry in a request must not be read by any future caller as
evidence that authorization has already occurred anywhere in this path.
Where a permission such as `calendar.read` is actually enforced remains
the same open question already logged in
`docs/agent-contracts/domain-model.md` ("`permissions` enforcement
boundary").

Separately, and for the same reason: the `POST /dispatch` HTTP endpoint
itself has **no authentication and no authorization**. Anything that can
reach `127.0.0.1:8700` (today: the local host and other containers on
`concierge-network`) can call it. This is an explicit, deliberate scope
boundary for this slice — see "Deliberately not implemented yet" below —
not an oversight, and not yet suitable for exposure beyond local
development.

### Current limitations

- The `POST /dispatch` request/response shape and status codes above are
  provisional, not a stable or versioned contract.
- No authentication or authorization on the HTTP endpoint.
- `AgentRequest.permissions` is not enforced anywhere in this path.
- No connection to the Slack Gateway (as of Slice 3, `HermesAgent` does
  connect to Hermes Agent — see "HermesAgent (Slice 3)" below).
- As of Slice 3, two Agents are registered — `dev-echo` (synthetic) and
  `hermes` (real) — both still hardcoded in `__main__.build_orchestrator()`;
  there is no production Agent registration mechanism (config file,
  discovery, or otherwise).
- No request classification or automatic Agent selection — `agent_name`
  is still supplied explicitly by the caller, exactly as in Slice 1.
- No trace context propagation into or out of the HTTP layer, including
  `HermesAgent`'s outgoing call to Hermes Agent.
- `orchestrator`'s Docker Compose service is still not depended on by any
  other service, and (at the Compose-topology level) does not `depends_on`
  any other service either — but as of Slice 3 it does call another
  service (Hermes Agent) at the application level, lazily, inside
  `HermesAgent.handle()`. See "HermesAgent (Slice 3)" for why this is not
  expressed as a Compose `depends_on`.

## HermesAgent (Slice 3)

`services/orchestrator/src/orchestrator/hermes_agent.py` defines
`HermesAgent`, registered by `orchestrator.__main__` under the name
`"hermes"` (`HERMES_AGENT_NAME`), alongside the unchanged `dev-echo`
`EchoAgent` — this slice adds a second registration, it does not replace
the first. `HermesAgent.handle(request)` satisfies the `Agent` Protocol by
calling the real, running Hermes Agent service's existing `/v1/responses`
HTTP API and mapping its output into an `AgentResponse`:

```text
AgentRequest -> POST <base_url>/v1/responses -> Hermes Agent -> Ollama
             -> Hermes response JSON -> output-text extraction
             -> AgentResponse(status="completed", summary=...)
```

**Request mapping.** `AgentRequest.instruction` becomes Hermes's `input`;
`AgentRequest.conversation_id` becomes Hermes's `conversation`; the request
also sends `"model": "hermes-agent"` and `"store": true`. This is the same
request shape `apps/slack-gateway/src/slack_gateway/hermes_client.py`'s
`HermesClient.create_response` already sends — `HermesAgent` is a second,
independent implementation of the same call, not a shared dependency
(`services/orchestrator` and `apps/slack-gateway` still share no code or
package; see "Why no shared client" below).

**Response mapping.** `HermesAgent` extracts output text the same way
`HermesClient` does: prefer a direct `output_text` string field, otherwise
concatenate `output[].content[].text` entries where `type == "message"` /
`type == "output_text"`. A successful call always returns
`AgentResponse(status="completed", summary=<extracted text>)` — no new
`status` value is introduced (see "Why raise instead of a new status
value" below).

**Configuration.** `HermesAgent` takes `base_url` and `api_key` as plain
constructor arguments — it does not read environment variables itself.
`orchestrator.__main__.build_orchestrator()` reads
`HERMES_API_BASE_URL` and `HERMES_API_SERVER_KEY` (via a small
`_require_env` helper that raises `RuntimeError` with a clear message if
either is unset or blank) and passes them in. These reuse the exact
variable names `apps/slack-gateway` already requires for the same
purpose — `docker-compose.yml`'s `orchestrator` service sets
`HERMES_API_BASE_URL` to the same literal `http://hermes-agent:8642`
`slack-gateway` uses, and `HERMES_API_SERVER_KEY` to the same
`${HERMES_API_SERVER_KEY:?...}` required-secret reference already used by
both `hermes-agent` and `slack-gateway` — no new `.env` entry is needed.

**Why no `depends_on: hermes-agent` in `docker-compose.yml`.** `hermes-agent`
itself `depends_on` `ollama` and `google-calendar-mcp` with
`condition: service_healthy`. Adding `depends_on: hermes-agent` to
`orchestrator` would transitively require that whole GPU-backed chain to
start (and become healthy) every time `orchestrator` starts — including in
CI's runtime smoke test (`.github/workflows/pytest.yml`), which starts
`orchestrator` in isolation and cannot satisfy that chain (no GPU, no
pre-pulled Ollama model, and its `--wait-timeout 60` is sized only for the
lightweight `orchestrator` container itself). Because `HermesAgent` only
calls Hermes Agent lazily, inside `handle()` — never at construction or at
container startup — the Orchestrator container does not need Hermes Agent
to be reachable to start or to pass its own liveness-only health check.
This is a deliberate scope boundary for this slice, not an oversight.

**Why raise instead of a new `status` value.** On any failure — a non-2xx
HTTP status, a connection/timeout error, a non-JSON or non-object response
body, or a response with no extractable output text — `HermesAgent.handle()`
raises `RuntimeError` rather than returning an
`AgentResponse(status="error", ...)` or similar. This reuses
`Orchestrator.dispatch()`'s existing behavior of letting an Agent's
exception propagate uncaught, and `http_server.py`'s existing generic
`500 internal_error` handling (see "No leaked internal detail" above) —
both already fully cover this without any change. Introducing a new
`status` value here would prematurely resolve open design question 4
under `docs/agent-contracts/domain-model.md` ("`status` vocabulary"),
which this slice deliberately leaves open, exactly as Slice 1 and Slice 2
did.

**Why standard library `urllib` instead of a new HTTP client dependency.**
Mirrors `http_server.py`'s own "why the standard library instead of a
framework" rationale for the same reason: `services/orchestrator` has
declared exactly one runtime dependency (`agent-contracts`) since Slice 1,
enforced by `test_dependency_boundary.py`. `HermesAgent` needs only a
single POST request with a JSON body, a bearer token header, and a
timeout — all directly expressible with `urllib.request` — so this slice
keeps that one-dependency boundary intact rather than adding `httpx` (or
another client library) for one call site. This is a choice sized for
*this* adapter, not a standing rule against ever adding an HTTP client
dependency to this package.

**Why no shared client with `apps/slack-gateway`.** `HermesAgent`'s
`_extract_output_text` is ported from, not imported from,
`HermesClient._extract_output_text` — the two are separate services with
no shared local package today (confirmed: no app or package in this repo
declares a dependency on a sibling app/package, only on `packages/`
libraries), and creating one for roughly 20 lines of extraction logic
would be a new cross-service architectural dependency this slice does not
introduce. `HermesAgent` also does not inject OpenTelemetry trace context
into its outgoing request the way `HermesClient` does — the Orchestrator
has no tracing instrumentation of its own yet at all (tracked under
Milestone 9), so there is no active local span to inject from.

## Deliberately not implemented yet

Out of scope for Slice 2, per its stated boundaries and
`docs/roadmap.md` Milestone 7's remaining tasks:

- MCP, or any transport beyond the minimal, provisional HTTP boundary
  described in "Runtime HTTP Boundary" above.
- Transport authentication or authorization.
- Slack Gateway integration.
- Request classification.
- Automatic agent selection.
- Permission enforcement.
- Memory.
- Approval workflow.
- Multi-agent delegation.
- Result aggregation.
- Trace propagation — including into or out of `HermesAgent`'s outgoing
  call to Hermes Agent (see "HermesAgent (Slice 3)" above).
- Production Agent registration/discovery — as of Slice 3, both
  `EchoAgent` ("Synthetic Agent" above) and `HermesAgent` ("HermesAgent
  (Slice 3)" above) are hardcoded in `__main__.build_orchestrator()`;
  there is still no configuration file, discovery mechanism, or
  environment-variable-driven registration *list*.
- Retries, timeouts tuning, circuit breaking, or any other resilience
  behavior beyond `HermesAgent`'s single request with a fixed default
  timeout and a plain raised exception on failure.

Also out of scope: persistence, dynamic loading, configuration-file-based
registration, network registration, and any change to
`packages/agent-contracts` or `packages/approvals`.

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
  rejecting non-string/blank names and names with leading or trailing
  whitespace (and confirming a padded name is never silently treated as
  equivalent to, nor coexists as a distinct key alongside, its trimmed
  form), a duplicate name raising `DuplicateAgentError` while leaving the
  original registration in place, `get()` raising `UnknownAgentError` for
  an unregistered name, and membership checks via `in`.
- `test_orchestrator.py` — successful dispatch returning the exact
  `AgentResponse` instance the Agent produced, the exact `AgentRequest`
  instance being passed through to `Agent.handle()`, explicit-name
  selection between multiple registered Agents (confirming the
  non-selected Agent is never called), `UnknownAgentError` for an
  unregistered name, confirmation that no Agent is called when dispatch
  targets an unknown name, and an Agent-raised exception propagating out
  of `dispatch()` as the identical exception instance (not caught,
  wrapped, or translated).
- `test_http_server.py` (Slice 2) — runs the real
  `OrchestratorHTTPServer` on an ephemeral localhost port in a background
  thread and drives it with real HTTP requests (`urllib`, standard
  library only): `GET /health`; a known-Agent `POST /dispatch` returning
  the exact `AgentResponse` (round-tripped through `agent_contracts`);
  an unknown-Agent `POST /dispatch` returning `404` and confirming no
  Agent is called; invalid-JSON, non-object, missing-`agent_name`, and
  incomplete/invalid `AgentRequest` bodies each returning `400`; an
  Agent that raises returning `500` with a body asserted *not* to contain
  the exception's message, its type name, or `"Traceback"`, followed by a
  `GET /health` check confirming the server is still responsive; and an
  unknown path returning `404`.
- `test_hermes_agent.py` (Slice 3) — runs a minimal stub HTTP server
  (standard library only, same background-thread idiom as
  `test_http_server.py`'s `running_server` fixture) standing in for
  Hermes Agent's `/v1/responses` endpoint, so these tests need no live
  Ollama or Hermes Agent container: successful dispatch extracting a
  direct `output_text`, successful dispatch extracting text from
  `output[].content[]` when `output_text` is absent, the exact request
  shape `HermesAgent` sends (path, `Authorization: Bearer`, JSON body
  fields), a non-2xx Hermes status raising `RuntimeError`, a connection
  error (nothing listening on the target port) raising `RuntimeError`, a
  non-JSON response body raising `RuntimeError`, a JSON-but-non-object
  response body raising `RuntimeError`, and a response with no
  extractable output text raising `RuntimeError`.
- `test_http_server.py` (extended in Slice 3) — adds
  `test_dispatch_to_unreachable_hermes_agent_returns_500_without_leaking_internal_details`,
  registering a real `HermesAgent` (not the abstract `ExplodingAgent`
  stub) pointed at a host nothing is listening on, confirming the same
  generic-500-without-leaked-detail guarantee holds for a real network
  failure — including that the configured Hermes base URL itself does not
  leak — and that the server is still responsive afterward.
- `test_dependency_boundary.py` — `pyproject.toml` declares exactly the
  one `local-agent-concierge-agent-contracts` dependency, and (as of
  Slice 3) `agent.py` / `registry.py` / `orchestrator.py` / `http_server.py`
  / `dev_agents.py` / `hermes_agent.py` / `__main__.py` all import only
  from the standard library, `agent_contracts`, or `orchestrator`'s own
  modules — confirming `HermesAgent`'s use of `urllib` did not introduce a
  new declared dependency.
- `stub_agents.py` — not a test module itself; the `RecordingAgent` /
  `ExplodingAgent` stub Agents shared by `test_orchestrator.py` and (as of
  Slice 2) `test_http_server.py`. Both exist only under `tests/` — not to
  be confused with `orchestrator.dev_agents.EchoAgent`, which ships in
  `services/orchestrator/src` because the running container needs a real
  registered Agent at startup, but is equally not production code (see
  "Synthetic Agent" above for the distinction between the two).
