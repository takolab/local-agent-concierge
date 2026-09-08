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

**Slice 4** adds correlation logging to the HTTP boundary's existing
`POST /dispatch` handling: every dispatch — success, unknown-agent, or an
Agent-raised exception — now emits a server-side log line carrying
`agent_name` and the dispatched `AgentRequest`'s `task_id`,
`conversation_id`, and `trace_id`. This is plain stdlib `logging`, not
OpenTelemetry trace propagation — see "Dispatch Correlation Logging
(Slice 4)" below for why these are different things and why that
distinction matters here. Slices 1-3's dispatch/registry/HTTP-boundary/
`HermesAgent` mechanism is unchanged by Slice 4; only `http_server.py`'s
`_handle_dispatch` gained log calls.

**Slice 5** makes the Orchestrator a participant in the distributed
trace instead of a break in it: `POST /dispatch` starts an OpenTelemetry
SERVER span parented by the incoming request's W3C trace context, and
`HermesAgent`'s outgoing call runs in a CLIENT span whose context is
injected into that request's headers. The caller, the Orchestrator, and
Hermes Agent's HTTP boundaries are now one trace. This is the slice that
supersedes Slice 4's "no OpenTelemetry here" statements; where those two
sections disagree, Slice 5 below is current. Slices 1-4's dispatch,
registry, HTTP status codes, response bodies, `AgentRequest` /
`AgentResponse` handling, and correlation logging are all unchanged. See
"Trace Context Propagation (Slice 5)" below.

None of these five slices implement request classification, automatic
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

**Why redirects are never followed.** `urllib.request`'s default
`HTTPRedirectHandler` follows 3xx responses automatically — for 301/302/303
it silently converts the request from `POST` to `GET`, and it forwards
every request header except `Content-Length`/`Content-Type` to the
redirect target, with **no same-origin check**: `Authorization` is carried
over even to a completely different host. Confirmed empirically (a
throwaway local two-server script, not just read from the urllib source):
unpatched `urlopen()` usage in this exact request shape followed a 302 to
a second server and delivered the `Bearer` credential to it. `HermesAgent`
therefore builds its own `urllib.request.OpenerDirector` via
`build_opener(_NoRedirectHandler)` — a small `HTTPRedirectHandler`
subclass whose `redirect_request` raises `HTTPError` instead of building a
followable request — so any 3xx from the configured Hermes base URL is
treated exactly like a 4xx/5xx (`handle()` raises, the target named in
`Location` is never contacted, and the bearer credential is therefore
never sent anywhere but `base_url` itself). `test_hermes_agent.py`'s
`test_redirect_response_raises_and_does_not_forward_credentials` proves
this against a second stub server standing in for the redirect target,
confirming it never receives a request. Found by this repo's Independent
AI Review on this slice's own PR, and verified independently (the
throwaway before/after scripts above) before being fixed — not a
hypothetical hardening measure.

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
introduce. As of Slice 5, `HermesAgent` *does* inject OpenTelemetry trace
context into its outgoing request, the same way `HermesClient` already
did — see "Trace Context Propagation (Slice 5)" below. (Before Slice 5 it
did not, because the Orchestrator had no tracing instrumentation of its
own to inject from.)

## Dispatch Correlation Logging (Slice 4)

Before this slice, a real dispatch failure — most importantly, a real
`HermesAgent` call failing because Hermes Agent or Ollama is unreachable —
produced a full stack trace in the Orchestrator's server logs with no way
to tell which caller, task, or conversation it belonged to:
`http_server.py`'s exception handling logged `self.command`/`self.path`
only, and an unknown-agent dispatch logged nothing at all. This is a step
toward `docs/roadmap.md` Milestone 7's "Preserve trace and conversation
identifiers" task — the one remaining unchecked item small and safe enough
for its own slice — but delivers *dispatch correlation logging*
specifically, not a complete end-to-end guarantee; see "Current
limitations, extended by Slice 4" below for exactly what remains open, and
the Slice 4 design proposal delivered in the session that produced this
slice for the full comparison against the alternatives considered and
rejected for now (configurable Hermes timeout, gating `dev-echo` behind an
opt-in, and a live-stack integration check that CI cannot run).

**What changed.** `http_server.py`'s `_handle_dispatch` now logs one
correlation line per outcome — INFO for success and unknown-agent, ERROR
(via `logger.exception`, with traceback) for an Agent-raised exception —
always via `%r`-formatted named fields, never the
`AgentRequest`/`AgentResponse` object itself:

- Success: `agent_name`, `task_id`, `conversation_id`, `trace_id`, and the
  resulting `AgentResponse.status`.
- Unknown agent (404): `agent_name`, `task_id`, `conversation_id`,
  `trace_id` — this path logged nothing at all before this slice.
- An Agent-raised exception (500): the same four request-side fields,
  via `logger.exception(...)` so the existing full traceback is still
  captured too — this is now caught inside `_handle_dispatch` itself,
  specifically so the log call has access to `agent_name`/`agent_request`,
  rather than falling through to `do_POST`'s outer generic
  `_handle_unexpected_error` (which only ever sees
  `self.command`/`self.path`). The HTTP response for this case is
  unchanged: the same `500 internal_error` body, constructed with the same
  status/error/detail values as before — only the server-side log line is
  richer.

**Why this lives in `http_server.py`, not in `Orchestrator.dispatch()`.**
`orchestrator.py`'s own docstring says `dispatch` "does exactly three
things" (look up the Agent, call it, return its response) — deliberately
minimal, per Slice 1's design. Adding logging there would be a fourth
thing, and would tie every future transport (not just this HTTP boundary)
to this specific logging behavior. `http_server.py` is already documented
as "a thin transport adapter" over the unchanged `dispatch()` call, and
access logging is an ordinary, expected responsibility for a transport
adapter to carry. `Orchestrator.dispatch()`, `AgentRegistry`, the `Agent`
Protocol, and `HermesAgent` itself are all unchanged by this slice.

**Why this is not OpenTelemetry / trace propagation.** `AgentRequest.trace_id`
is logged exactly as received — as an opaque, caller-supplied string (or
`None`). Correlation logging and W3C Trace Context remain two different
mechanisms, and Slice 5 did not merge them: see "`AgentRequest.trace_id`
is not W3C Trace Context" under Slice 5 below.

> **Superseded in part by Slice 5.** When this section was written, no
> `opentelemetry` package was imported anywhere in
> `services/orchestrator`, `HermesAgent`'s outgoing call carried no
> `traceparent`, and wiring the Slack Gateway to the Orchestrator would
> therefore have silently dropped the already-verified Milestone 5
> distributed trace (`concierge.request` → `hermes.request` →
> `/v1/responses`). Slice 5 is what removed that specific blocker. The
> statements above about *`trace_id` itself* still hold unchanged.

**What is logged, and what is deliberately never logged.** The new
*structured correlation log calls* this slice adds — the three described
above — pass only `agent_name`, `task_id`, `conversation_id`, `trace_id`,
and (on success) `AgentResponse.status`: plain opaque identifiers, matching
the redaction discipline `docs/observability/collector-redaction.md`
already established for OpenTelemetry span attributes elsewhere in this
repo, applied here to plain log lines instead. `AgentRequest.instruction`
(free text a user wrote) and `AgentResponse.summary` (a real Hermes model
response) are never passed to these calls, and neither is any
`Authorization`/API-key value — `http_server.py` never has access to
`HermesAgent`'s configured `api_key` in the first place, so there is no
code path here that could log it this way. `test_http_server.py`'s
`test_dispatch_logging_never_contains_instruction_text` and
`test_dispatch_logging_never_contains_hermes_api_key` assert this
directly, using a distinctive per-test sentinel value rather than a
generic placeholder. Both were confirmed to be real, load-bearing checks
during implementation, not vacuously-passing assertions: temporarily
logging the whole `AgentRequest`/`AgentResponse` objects instead of their
named fields (a plausible naive mistake, since both are otherwise
convenient to log directly) made the instruction-leak test fail exactly
as expected, via the object's default dataclass `repr()` — the same
"does this test actually detect the bug it claims to detect" check this
repo's Independent AI Review already applied to the Slice 3 redirect
regression test.

**This is narrower than an "instruction never appears in server logs"
guarantee.** The exception path's `logger.exception(...)` call (both the
one added by this slice and the pre-existing one in
`_handle_unexpected_error`, unchanged) still records the raised
exception's own message and full traceback. If a future Agent
implementation raised an exception whose message embedded request content
— e.g. `raise ValueError(f"invalid instruction: {request.instruction}")`
— that text would still reach the server logs via that traceback,
confirmed directly (a throwaway script raising an exception with a
distinctive message through `logger.exception` showed that message text
in the captured output). This is not a new risk introduced by this slice
— the same `logger.exception` behavior, with the same property, already
existed on the previously-unchanged `_handle_unexpected_error` path before
this slice — and no currently-registered Agent (`EchoAgent`, `HermesAgent`)
does this today. A true "never appears anywhere in server logs" guarantee
for `instruction` would require a separate exception-message redaction
design with its own failure-path regression tests; this slice's structured
correlation logging does not attempt to provide that, and the tests named
above only cover the paths they exercise (a successful dispatch, and a
`HermesAgent` connection failure), not every possible Agent exception.
Found by this repo's Independent AI Review on this slice's own PR.

### Current limitations, extended by Slice 4

The "Current limitations" and "Deliberately not implemented yet" lists
below are otherwise unchanged by this slice. Additions:

- Correlation logging exists only for the `POST /dispatch` HTTP path
  (`http_server.py`). `GET /health` is unchanged (still liveness-only, no
  logging added). Nothing outside `services/orchestrator` reads these
  logs programmatically — this is server-log-only, human/`docker compose
  logs`-oriented output, not a new API or contract.
- `AgentRequest.trace_id` is now visible in the Orchestrator's own logs
  when a caller supplies one, but nothing in this repository populates it
  yet in practice (the Slack Gateway does not call the Orchestrator at
  all).
- **This slice delivers dispatch correlation logging specifically, not a
  complete "preserve trace and conversation identifiers" guarantee.**
  `conversation_id`/`trace_id` were already fields on `AgentRequest`
  (Slice 1) and were already passed through `Orchestrator.dispatch()`
  unmodified before this slice existed — in that narrow sense they were
  already "preserved" inside the HTTP boundary. What was actually missing,
  and what this slice adds, is *visibility*: making dispatch outcomes
  observable via correlation identifiers in the server's own logs.
  End-to-end preservation is still incomplete in at least two ways this
  slice does not address: `HermesAgent` forwards `conversation_id` to
  Hermes but not `trace_id` (see "HermesAgent (Slice 3)" above), and there
  is still no real caller that populates `trace_id` in practice (Slack
  Gateway integration remains unimplemented). `docs/roadmap.md`'s
  "Preserve trace and conversation identifiers" checkbox is therefore left
  unchecked by this slice, rather than claimed complete. Found by this
  repo's Independent AI Review on this slice's own PR. (Slice 5 addresses
  neither of those two gaps: it propagates *W3C trace context*, which is
  carried in HTTP headers and is not `AgentRequest.trace_id`, and it adds
  no caller. That checkbox stays unchecked.)

## Trace Context Propagation (Slice 5)

Before this slice the Orchestrator was a break in the distributed trace.
`apps/slack-gateway` starts a trace and injects `traceparent` into its
own direct Hermes call, and Hermes Agent's auto-instrumentation extracts
it (`docs/observability/hermes-trace-context.md`) — but a request routed
through the Orchestrator lost that context at the HTTP boundary and
Hermes started an unrelated root trace. That made "route the Slack
Gateway through the Orchestrator" a change that would have *regressed*
the already-verified Milestone 5 trace. This slice removes that
blocker; it does not itself connect the Slack Gateway.

### What the trace looks like now

```text
concierge.request                  (Slack Gateway, or any caller)
  |
  +-- POST /dispatch               (Orchestrator, SERVER span,
        |                           parent extracted from traceparent)
        |
        +-- hermes.request         (Orchestrator, CLIENT span,
              |                     injects traceparent)
              |
              +-- /v1/responses    (Hermes Agent, SERVER span,
                                    extracted from traceparent)
```

Two spans, and nothing else. `GET /health` is deliberately untraced: it
is a container liveness probe running every 10 seconds with no caller
context to continue and nothing to observe, so tracing it would only add
volume.

### Where each piece lives

- `telemetry.py` (new) — `configure_tracing()`, the two span context
  managers, `trace_context_headers()`, and the bounded `error.type`
  vocabulary. This is the only module that imports OpenTelemetry
  directly for span creation.
- `http_server.py` — `do_POST` wraps the existing, unchanged
  `_handle_dispatch()` in the SERVER span and records the status that
  was actually sent.
- `hermes_agent.py` — `handle()` wraps the existing, unchanged call in
  the CLIENT span; `_call_hermes()` merges the injected headers into the
  request it already built.
- `__main__.py` — installs the tracer provider before serving and shuts
  it down (flushing batched spans) after the server stops.

`orchestrator.py`, `registry.py`, and `agent.py` — the routing core —
import no OpenTelemetry at all, and `Orchestrator.dispatch()` still does
exactly the three things described under "`Orchestrator.dispatch`" above.
Telemetry belongs to the transport boundaries that own an HTTP request,
not to the domain logic. `test_dependency_boundary.py` enforces that
split directly.

### No custom header parsing

`traceparent` and `tracestate` are never parsed by this repository.
`opentelemetry.propagate.extract()` and `inject()` hand the carrier to
the configured propagator, and its `TraceContextTextMapPropagator`
decides everything: which versions are acceptable, what counts as
well-formed, and what a malformed value means. Concretely, that means a
missing, empty, or malformed `traceparent` — or the reserved `ff`
version, or an all-zero trace or span id — simply yields no parent and
the request becomes a new root trace, while an *unknown but well-formed
future version* (`99-…`) is accepted, as W3C Trace Context requires.
None of that is a decision made here, and none of it changes the HTTP
response in any way. Both behaviors are asserted in
`test_trace_propagation.py` so they are recorded as deliberate.

The one piece of header handling this repository does do is lowercasing
header names before handing them to `extract()`: HTTP header names are
case-insensitive, and a `dict` lookup is not, so a caller sending
`Traceparent` would otherwise silently start a new trace. That is
carrier normalization, not trace-context parsing.

**Baggage.** The default propagator is `tracecontext,baggage`, so
`extract()` does parse a caller's `baggage` header. It does not reach
Hermes: `start_as_current_span(context=...)` uses the passed context only
to resolve the parent span and then attaches the new span onto the
*ambient* context, so the extracted context's baggage is not what
`inject()` later reads. `test_caller_supplied_baggage_does_not_reach_hermes_or_span_data`
pins that, because the alternative would mean forwarding arbitrary
caller-supplied key/values to an internal service from an endpoint with
no authentication. Treat that test as a regression tripwire rather than a
security control — a deployment that needs the guarantee should set the
standard `OTEL_PROPAGATORS=tracecontext`.

### `AgentRequest.trace_id` is not W3C Trace Context

These are two different things and this slice keeps them separate:

| | `AgentRequest.trace_id` | W3C Trace Context |
|---|---|---|
| Where it travels | a field inside the JSON body | the `traceparent` / `tracestate` HTTP headers |
| Format | any non-empty string; no format enforced (`docs/agent-contracts/domain-model.md`) | the W3C format, validated by OpenTelemetry's propagator |
| Who reads it | Slice 4's correlation log lines | OpenTelemetry, to parent a span |
| Set by this repo | nobody yet — no caller populates it | the Slack Gateway today; any instrumented caller |

The Orchestrator therefore **never** reconstructs an OpenTelemetry parent
context from `AgentRequest.trace_id`, and never overwrites the incoming
HTTP trace context with it. It also never writes `trace_id` back into the
request. A caller may send a JSON `trace_id` that has nothing to do with
its HTTP `traceparent` — propagation follows the HTTP context, and the
`AgentRequest` reaches the Agent byte-for-byte as it arrived
(`test_json_trace_id_neither_overrides_nor_is_altered_by_http_context`,
`test_agent_receives_the_request_unmodified_when_tracing_is_active`).

Doing the opposite — treating the JSON field as the propagation
mechanism — would mean inventing a parent span id the caller never sent
and trusting a format the schema explicitly does not enforce. That is
also why `docs/agent-contracts/domain-model.md`'s open question 1 stays
open as a *schema* question: this slice decides only what the
Orchestrator does, which is to leave the field alone.

### Trace context is not authentication

A `traceparent` header is caller-supplied, unauthenticated, and trivially
forgeable — as is `AgentRequest.permissions`. Nothing in this slice
treats either as evidence of anything. `POST /dispatch` has no
authentication or authorization (see "Authorization boundary" above,
unchanged), and joining a caller's trace grants no capability: the only
effect is which trace the resulting spans are filed under.

### What goes on a span, and what never does

| Span | Attributes |
|---|---|
| `POST /dispatch` (SERVER) | `concierge.operation`, `http.method`, `http.route`, `http.status_code`, and `error.type` on 5xx |
| `hermes.request` (CLIENT) | `concierge.downstream.service`, `concierge.operation`, `http.status_code` on an HTTP failure, and `error.type` on any failure |

That is the complete list, asserted as a closed set by
`test_span_attribute_keys_are_limited_to_the_expected_set`. It follows
the same discipline `docs/observability/collector-redaction.md`
established: no instruction text, no Hermes response text, no
`Authorization` value or API key, no raw exception message or traceback,
and no per-user, per-task, or per-conversation identifier.

`error.type` is a closed vocabulary of four values
(`telemetry.ERROR_TYPES`): `dispatch.server_error`,
`hermes.http_status_error`, `hermes.connection_error`, and
`hermes.invalid_response`. `telemetry._mark_error` raises on anything
else, so an exception's class name or message cannot become an
`error.type` by accident, and
`test_every_recorded_error_type_comes_from_the_declared_vocabulary`
asserts the emitted set stays within it.

**`agent_name` is deliberately not a span attribute**, even though
"which Agent was selected" is a Milestone 9 goal. It is a free string
supplied by an unauthenticated caller: unbounded in cardinality, and not
guaranteed to be free of content the caller should not have put there. It
stays in Slice 4's correlation *logs*, which never leave the container.
Attaching it safely needs the registered-agent set to be the source of
the value rather than the request — a small design decision, deferred
rather than made silently here.

**Exceptions are never recorded by the SDK.** Both spans are created with
`record_exception=False` and `set_status_on_exception=False`, matching
`apps/slack-gateway` and `mcp/google-calendar`. Without that, an
exception propagating through a span would be attached as an `exception`
event carrying `exception.message` and `exception.stacktrace` — text this
service does not control. Failures are recorded as a status plus one
`error.type` instead. The CLIENT span is where this is load-bearing
(`hermes_agent.handle()` really does let its `RuntimeError` escape the
context manager); on the SERVER span it is defense-in-depth, since
`_handle_dispatch` catches every exception itself.

There is no auto-instrumentation in this service — no
`opentelemetry-instrument`, no `sitecustomize` hook, no instrumented
HTTP library. Every span here is created by the code above, which is
what makes the closed attribute set above assertable at all.

### Injection never overwrites an existing header

`trace_context_headers()` injects into a *fresh* dict, which
`_call_hermes` then merges into the headers it already built. The
direction is deliberate: the propagator can add `traceparent` /
`tracestate` but can never replace `Authorization` or `Content-Type`
(`test_outgoing_request_keeps_its_authorization_and_content_type`).

### Failure and disabled-telemetry behavior

Telemetry is never allowed to change what a caller sees:

- **No Collector reachable.** Spans leave through a
  `BatchSpanProcessor` on a background thread, so an absent or failing
  Collector cannot delay or fail a dispatch. `docker-compose.yml`
  deliberately gives `orchestrator` no `depends_on: otel-collector` for
  this reason, and CI's runtime smoke test starts the container with no
  Collector reachable at its configured endpoint — which makes that
  smoke test a live check of this property.
- **Telemetry disabled.** With `OTEL_SDK_DISABLED=true`, or with no
  provider installed at all, the OpenTelemetry API hands out
  non-recording spans; every call site here tolerates that, and no
  `traceparent` is sent rather than a fabricated one
  (`test_hermes_call_without_a_tracer_provider_sends_no_traceparent`).
- **Broken telemetry configuration.** `__main__` catches an exception
  from `configure_tracing()`, logs it, and serves untraced rather than
  refusing to start. An Orchestrator that cannot export telemetry must
  still dispatch.

### Context is never leaked between requests

Both spans are entered with `start_as_current_span` as a context manager,
so the span ends and detaches on the success path and on an exception
alike. This matters concretely because `ThreadingHTTPServer` reuses one
handler instance and one thread for several keep-alive requests on a
connection: a span left attached would be silently inherited as the
parent of the *next* caller's request.
`test_a_traced_request_does_not_leak_context_into_an_untraced_one` and
`test_context_does_not_survive_an_agent_exception` assert exactly that,
including that no span is left unended after a failure.

### Configuration

`configure_tracing()` mirrors `apps/slack-gateway/src/slack_gateway/
telemetry.py` and `mcp/google-calendar/src/google_calendar_mcp/
telemetry.py`: a `Resource` of `service.name=orchestrator` /
`service.namespace=local-agent-concierge`, an OTLP/gRPC exporter
configured entirely through the standard `OTEL_EXPORTER_OTLP_ENDPOINT`
environment variable, and a `BatchSpanProcessor`. `docker-compose.yml`
sets that endpoint to `http://otel-collector:4317`, the same value the
other two services use. Nothing here talks to Phoenix or MLflow: the
Collector owns backend fan-out.

Two deliberate differences from those two services:

- `OTEL_SDK_DISABLED=true` is honored, giving a real "telemetry off"
  mode that CI and tests can exercise.
- `configure_tracing()` returns the provider, and `__main__` shuts it
  down after the HTTP server stops, flushing spans still queued in the
  batch processor. The Orchestrator already had a graceful SIGTERM/SIGINT
  shutdown path (Slice 2) to hang this on; the other two services do not.

### Dependencies

This slice makes `services/orchestrator` depend on
`opentelemetry-api`, `opentelemetry-sdk`, and
`opentelemetry-exporter-otlp-proto-grpc`, pinned to the same
`>=1.44.0,<2.0` range `apps/slack-gateway` and `mcp/google-calendar`
already use — all three services export to the same Collector, and
OpenTelemetry's packages are only guaranteed to work together within a
release train.

`test_dependency_boundary.py` previously asserted "exactly one runtime
dependency, standard library only everywhere". Keeping that assertion
would have pinned a fact that is no longer true rather than protected
anything, so it was replaced with the invariant that still matters: the
routing core imports no third-party code at all, the transport modules
may additionally import `opentelemetry` and nothing else, and the
OpenTelemetry pins must match the sibling services'. Neither the
Dockerfile nor `.github/workflows/orchestrator.yml` needed changes — both
install from `pyproject.toml`.

### What this slice does not do

- It does not connect the Slack Gateway to the Orchestrator. That is
  still a separate change; this one removes the trace-regression reason
  it was unsafe, not the rest of the work.
- It does not close Hermes Agent's known outbound-MCP propagation gap
  (`docs/observability/hermes-trace-context.md`, "Known gap"). A trace
  reaching Hermes still stops at Hermes' own MCP boundary, for reasons
  upstream of this repository.
- It adds no spans for anything other than the two HTTP boundaries — no
  agent-selection, model-call, memory, or approval spans, which are the
  rest of Milestone 9.
- It does not resolve `AgentRequest.trace_id`'s schema question, enforce
  permissions, or add authentication.

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
- ~~Trace propagation~~ — implemented for both HTTP boundaries by Slice
  5 (see "Trace Context Propagation (Slice 5)" above). Still absent:
  spans for anything other than those two boundaries (agent selection,
  model calls, memory, approvals), which is the rest of Milestone 9.
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
  trace ID. Slice 5 answers this for the *Orchestrator's behavior* — it
  leaves the field entirely alone and propagates W3C Trace Context in
  HTTP headers instead — but not for the *schema*: what a caller is
  supposed to put in `trace_id`, if anything, is still undecided. See
  "`AgentRequest.trace_id` is not W3C Trace Context" above.
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
  fields), a non-2xx Hermes status raising `RuntimeError`, a 3xx redirect
  response raising `RuntimeError` *and* a second stub server standing in
  for the redirect target confirming it never receives a request (see
  "Why redirects are never followed" above), a connection error (nothing
  listening on the target port) raising `RuntimeError`, a non-JSON
  response body raising `RuntimeError`, a JSON-but-non-object response
  body raising `RuntimeError`, and a response with no extractable output
  text raising `RuntimeError`.
- `test_http_server.py` (extended in Slice 3) — adds
  `test_dispatch_to_unreachable_hermes_agent_returns_500_without_leaking_internal_details`,
  registering a real `HermesAgent` (not the abstract `ExplodingAgent`
  stub) pointed at a host nothing is listening on, confirming the same
  generic-500-without-leaked-detail guarantee holds for a real network
  failure — including that the configured Hermes base URL itself does not
  leak — and that the server is still responsive afterward.
- `test_http_server.py` (extended in Slice 4) — six new tests using
  pytest's built-in `caplog` fixture (no new dependency), against the same
  real `OrchestratorHTTPServer` the rest of this file already drives:
  correlation identifiers appearing in the logged output for a successful
  dispatch (including the resulting `AgentResponse.status`), for a
  dispatch with `trace_id=None` (asserting its absence is logged
  explicitly as `trace_id=None`, not silently omitted), for an
  unknown-agent dispatch, and for an Agent-raised exception; plus two
  negative assertions — a distinctive `instruction` sentinel and a
  distinctive `HermesAgent` `api_key` (via the same
  real-`HermesAgent`-at-an-unreachable-port idiom as the test above) never
  appearing anywhere in the captured log output.
- `test_trace_propagation.py` (Slice 5) — 37 tests driving the real
  `OrchestratorHTTPServer` over real HTTP, with a real stub Hermes server
  receiving the Orchestrator's real outgoing request, so what is asserted
  is the actual `traceparent` bytes on the wire alongside the spans an
  in-memory exporter recorded. Covers: a valid incoming `traceparent`
  becoming the SERVER span's parent; header-name case-insensitivity;
  absent / empty / malformed / `ff`-version / all-zero-id `traceparent`
  values all falling back to a root trace with the dispatch response
  unchanged; an unknown-but-well-formed future version still being joined
  (the propagator's job, not this repo's); `GET /health` producing no
  span; SERVER span, CLIENT span, and the header Hermes actually received
  all sharing one trace with the right parent chain; `Authorization` and
  `Content-Type` surviving injection; no `traceparent` sent when nothing
  is recording; a JSON `trace_id` that differs from the HTTP context
  changing neither propagation nor the `AgentRequest` the Agent receives;
  context isolation across two requests, across a traced-then-untraced
  pair, and after an Agent exception (including that no span is left
  unended and no context stays attached); `http.status_code` recorded for
  200/404/500 with 4xx deliberately not marked as a server-span error;
  Hermes non-2xx, connection failure, and unusable-body cases keeping
  their existing error responses while recording a bounded `error.type`;
  the emitted `error.type` set staying inside `telemetry.ERROR_TYPES`;
  the span attribute keys staying inside a closed allowlist; and
  redaction — distinctive sentinels for the instruction, conversation id,
  user id, task id, Hermes response text and API key never appearing in
  any span's attributes, events, status description, or resource, and no
  `exception` event on the CLIENT span an exception actually propagates
  through.

  Also pinned: a caller's `baggage` header reaching neither Hermes nor
  span data, and a set of hostile `traceparent` / `tracestate` values
  (malformed tracestate, a 4 KB value, whitespace padding, baggage with
  no traceparent) never failing a dispatch.

  Four of these were confirmed load-bearing by mutation during
  implementation rather than assumed: removing the `extract()` call,
  removing header lowercasing, injecting nothing, and flipping the CLIENT
  span to `record_exception=True` each fail exactly the test that claims
  to cover them. The fourth mutation is why the exception-redaction check
  is asserted on the CLIENT span: the same mutation on the SERVER span
  changes nothing observable, because `_handle_dispatch` catches every
  exception itself, so a SERVER-span-only assertion would have passed
  vacuously.
- `test_dependency_boundary.py` (rewritten in Slice 5) — `pyproject.toml`
  declaring exactly the expected four dependencies; the three
  OpenTelemetry pins matching `apps/slack-gateway`'s and
  `mcp/google-calendar`'s; the routing core (`agent.py`, `registry.py`,
  `orchestrator.py`, `dev_agents.py`) importing no third-party code at
  all, OpenTelemetry included; and the transport modules
  (`http_server.py`, `hermes_agent.py`, `telemetry.py`, `__main__.py`)
  importing only `agent_contracts`, `orchestrator`'s own modules, or
  `opentelemetry`. This replaces Slice 3's "exactly one dependency,
  standard library only" assertions, which Slice 5 made false — see
  "Dependencies" under Slice 5 above for why the replacement is the
  invariant worth keeping.
- `stub_agents.py` — not a test module itself; the `RecordingAgent` /
  `ExplodingAgent` stub Agents shared by `test_orchestrator.py` and (as of
  Slice 2) `test_http_server.py`. Both exist only under `tests/` — not to
  be confused with `orchestrator.dev_agents.EchoAgent`, which ships in
  `services/orchestrator/src` because the running container needs a real
  registered Agent at startup, but is equally not production code (see
  "Synthetic Agent" above for the distinction between the two).
