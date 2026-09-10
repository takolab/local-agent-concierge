# Orchestrator Trace Context Propagation

How `services/orchestrator` joins the distributed trace, and how that
relates to the trace context work already documented for the Slack
Gateway (`docs/roadmap.md` Milestone 5), Hermes Agent
(`docs/observability/hermes-trace-context.md`), and Google Calendar MCP
(`docs/observability/google-calendar-mcp-telemetry.md`).

The full design rationale lives in `docs/orchestrator/domain-model.md`
("Trace Context Propagation (Slice 5)"). This file covers only the
cross-service picture and the one distinction that is easy to get wrong.

## The chain

```text
concierge.request                  Slack Gateway (CONSUMER)
  |
  +-- orchestrator.dispatch        Slack Gateway (CLIENT)
  |     |
  |     +-- POST /dispatch         Orchestrator (SERVER)
  |           |
  |           +-- hermes.request   Orchestrator (CLIENT)
  |                 |
  |                 +-- /v1/responses    Hermes Agent (SERVER)
  |                       |
  |                       X  tools/call  Google Calendar MCP -- NEW trace
  |
  +-- slack.response               Slack Gateway (CLIENT)
```

There is one path now. The Slack Gateway dispatches through the
Orchestrator (`docs/slack-gateway/orchestrator-dispatch.md`); its previous
direct `hermes.request` CLIENT span, and the `HermesClient` that produced
it, were removed rather than kept as a second route. `hermes.request`
still appears in the trace — emitted by the Orchestrator for the hop it
now owns.

This chain is established by automated tests on both sides. It has **not**
been observed end to end on the live stack from a real Slack message; see
"Still not verified" below.

The `X` is the known upstream gap: Hermes Agent's outbound MCP calls do
not carry trace context, so a Calendar tool call starts an unrelated
trace. That is
[tracked upstream](https://github.com/NousResearch/hermes-agent/issues/60177)
and unchanged by anything here — see "Known gap" in
`docs/observability/hermes-trace-context.md`.

## Two different mechanisms, often confused

| | `AgentRequest.trace_id` | W3C Trace Context |
|---|---|---|
| Carried in | the JSON request body | the `traceparent` / `tracestate` HTTP headers |
| Format | any non-empty string; none enforced | W3C format, validated by OpenTelemetry's propagator |
| Purpose | logical correlation, for log lines | parenting spans across services |
| Who sets it | nobody in this repository — the Slack Gateway deliberately leaves it `null` | the Slack Gateway; any instrumented caller |

The Orchestrator keeps them separate: it never builds an OpenTelemetry
parent context out of `AgentRequest.trace_id`, never lets that field
override the incoming HTTP context, and never writes it back into the
request. A request whose JSON `trace_id` disagrees with its HTTP
`traceparent` is propagated according to the HTTP context and forwarded
to the Agent exactly as received.

`traceparent` is caller-supplied and unauthenticated. It is never treated
as evidence of anything — neither is `AgentRequest.permissions`. Joining
a caller's trace grants no capability.

## Redaction

Same discipline as `docs/observability/collector-redaction.md`, which the
Collector then backstops. The Orchestrator emits only these attributes:

- `POST /dispatch` (SERVER): `concierge.operation`, `http.method`,
  `http.route`, `http.status_code`, plus `error.type` on a 5xx.
- `hermes.request` (CLIENT): `concierge.downstream.service`,
  `concierge.operation`, `http.status_code` on an HTTP failure, plus
  `error.type` on any failure.

`error.type` is a closed four-value vocabulary. No instruction text,
Hermes response text, `Authorization` value, API key, exception message,
traceback, or per-user/conversation identifier is ever attached — and
`agent_name` is deliberately excluded too, being an unbounded string from
an unauthenticated caller.

There is no auto-instrumentation in this service: no
`opentelemetry-instrument`, no `sitecustomize` hook, no instrumented HTTP
library. Every span is created by hand, which is what makes that closed
attribute list assertable at all — unlike Hermes Agent, which is
auto-instrumented and therefore relies on the Collector's redaction
processor as a second line of defense.

## Baggage

The default propagator is `tracecontext,baggage`, so a caller's `baggage`
header is parsed — but it is not forwarded to Hermes and never becomes
span data (see `docs/orchestrator/domain-model.md`, "Baggage", for why,
and for the test that pins it). Set the standard
`OTEL_PROPAGATORS=tracecontext` if you want that guaranteed by
configuration rather than observed.

## Configuration

`OTEL_EXPORTER_OTLP_ENDPOINT` (`http://otel-collector:4317` in
`docker-compose.yml`), the same OTLP/gRPC endpoint the Slack Gateway and
Google Calendar MCP use. Spans go to the Collector only; Phoenix and
MLflow are the Collector's business.

`OTEL_SDK_DISABLED=true` turns tracing off entirely. Dispatch behavior is
identical either way, and the `orchestrator` service deliberately has no
`depends_on: otel-collector` — an absent or failing Collector must never
delay or fail a dispatch.

## Verification status

Automated, in CI (`services/orchestrator/tests/test_trace_propagation.py`,
40 tests): parent/child relationships and trace ids across the SERVER
span, the CLIENT span, and the `traceparent` header actually received by
a stub Hermes server; fallback for missing and malformed headers;
repeated `tracestate` header fields surviving in order all the way to the
header Hermes receives, and repeated `traceparent` fields starting a
fresh root trace instead of resolving to either of them; context
isolation between requests and after exceptions; the closed attribute and
`error.type` sets; and sentinel-based redaction checks.

Also in CI, against the real container
(`.github/workflows/pytest.yml`): `POST /dispatch` with a `traceparent`
header returning the identical response, with no Collector reachable at
the configured endpoint — a live check that telemetry export failure does
not change dispatch behavior.

### End-to-end verification (manual)

Run on 2026-09-10 against the real stack — the actual `orchestrator`,
`hermes-agent` and `ollama` containers, exporting through the real
`otel-collector` to Phoenix and MLflow. No stubs. Two `POST /dispatch`
requests with `agent_name: "hermes"`, differing only in whether they
carried an incoming `traceparent`.

#### Exact state this evidence came from

"The real stack" is not self-identifying: `orchestrator` and
`hermes-agent` are built from the worktree, and `otel-collector` and
`ollama` track floating `:latest` tags. So the run is pinned to this:

```text
repository   4f79828  (master, clean worktree)
orchestrator     image sha256:f021e28af4b6c2eda0ab29113c152336a81fa1cdfdb03151ff350fbdc397a739
hermes-agent     image sha256:470aa3b68074d9d752ff2d9d83f0110161e62cb60d62d09eb74347fd01b449ac
otel-collector   otel/opentelemetry-collector-contrib@sha256:1f2c54a30e713fac6b3ae77a1ec84010c2007e29ced8ec666214fc2f6739c1cc
ollama           ollama/ollama@sha256:4dea9fb511947e24a84237bb636b0203abcb2ff0d3fbc7b4ff865deb91362131
```

The `orchestrator` image was built from `214ea00`, whose tree is
byte-identical to `4f79828` (`git diff 214ea00 4f79828` is empty), so the
image and the recorded repository SHA describe the same code. Every
container above reported `RestartCount=0` and a start time before the
run, so these are the processes that actually served it — not a later
replacement.

Without this block the section would record *when* something was
verified but not *what*: a future reader could not reconstruct which code
and which images produced the evidence below.

**With an incoming `traceparent`.** Sent
`00-7075ec6bb22fa7f31f5840bceaa7850f-40d6e99612795bc1-01`; HTTP 200 in
9.4s with the model's real answer. Phoenix's span API
(`GET /v1/projects/{project}/spans?trace_id=...`) returned three spans on
that one trace, whose `span_id`/`parent_id` values chain exactly:

```text
40d6e99612795bc1                             <- the traceparent that was sent
  └─ a6019548472c8eaf   POST /dispatch       orchestrator   SERVER
       └─ 99f4b9329c8063be   hermes.request  orchestrator   CLIENT
            └─ 31e467eae5fc6fb2   /v1/responses   hermes-agent   SERVER
```

The last link is the one that could not be proven before: every earlier
check of `hermes.request` → Hermes' own SERVER span used a stub Hermes
server, not the real, auto-instrumented one.

**Without a `traceparent`.** `POST /dispatch` became a true root span and
the same three-span tree formed beneath it.

**Attributes actually stored in Phoenix.** The two Orchestrator spans
carried exactly the closed sets documented above, and nothing else:

```text
POST /dispatch   concierge.operation, http.method, http.route,
                 http.status_code
hermes.request   concierge.downstream.service, concierge.operation
```

(`hermes.request` shows no `http.status_code` because that is only set on
a failure, and this call succeeded.) Hermes Agent's own `/v1/responses`
span — produced by its auto-instrumentation, not by this repository —
carried standard HTTP semantic-convention fields only: scheme, host,
method, route, target, url, status code, flavor, server name, port and
user agent. No request or response body, and no `Authorization`.

No span carried `redaction.masked.count`, only `redaction.ignored.count`
— nothing was flagged sensitive by the Collector because nothing
sensitive was sent.

**Redaction, checked with sentinels.** Each of these exact strings was
searched for across both Phoenix's stored spans for the two trace ids and
the Collector's full debug output — **zero occurrences** for every one:

```text
task_id           e2e-verify-1, e2e-verify-2
user_id           e2e-user, e2e-user2
conversation_id   e2e-verify, e2e-verify-2
JSON trace_id     json-side-correlation-id
instruction text  "Reply with exactly", and the per-request sentinel
model response    the same per-request sentinel, echoed back
credential        Bearer
```

Separately, all 6 spans across the two traces were walked attribute by
attribute — keys *and* values — against `auth`, `bearer`, `token`,
`secret`, `key`, and the sentinel strings. Nothing matched.

One honest caveat, because a naive grep suggests otherwise: the string
`authorization` does occur twice in the Collector's log, but on neither
of these traces. Both hits are synthetic probe spans emitted by
`infra/observability/tests/test_redaction.py`
(`operation.name: synthetic-operation`), and in both the value is already
masked to `****` by the Collector's redaction processor. No span produced
by the Orchestrator or Hermes Agent carries an `authorization` key at
all.

The request also carried a deliberately mismatched JSON
`"trace_id": "json-side-correlation-id"`. It appears nowhere in the
telemetry, and propagation followed the HTTP `traceparent` — confirming
on the live stack what "`AgentRequest.trace_id` is not W3C Trace Context"
above asserts.

Incidentally, `/v1/responses` carries
`http.user_agent: Python-urllib/3.12`, which identifies the caller as the
Orchestrator rather than the Slack Gateway (which uses `httpx`) — useful
when telling the two paths apart in a backend.

### A missing root span holds a trace `IN_PROGRESS` in MLflow

The first request's trace sat at `state: IN_PROGRESS` in MLflow while the
second showed `state: OK`. That pairing is only *consistent with* a
missing root span being the cause, so it was isolated directly rather
than inferred, on a third trace
(`353caf00ceb3c9bec7a953f6ae645385`):

1. `POST /dispatch` was sent with
   `traceparent: 00-353caf…-0330406cb8ca6bed-01`, naming a parent span
   that had never been exported. Phoenix showed the expected three spans,
   with `POST /dispatch`'s parent id matching nothing in the trace.
   MLflow: **`IN_PROGRESS`**.
2. Nothing else was changed. A single span was then exported to the same
   Collector with that exact trace id and span id — `353caf…` /
   `0330406cb8ca6bed` — and no parent of its own: precisely the root that
   had been missing. (Reproducible with a `TracerProvider` given an
   `IdGenerator` that returns those two fixed ids, run from inside the
   `orchestrator` container so it reaches the Collector on the compose
   network.)
3. Phoenix then showed four spans, with the newly arrived root resolving
   the previously missing parent of `POST /dispatch`, and the same MLflow
   trace moved to **`OK`**. Nothing about the child span changed: its
   `parent_span_id` was `0330406cb8ca6bed` all along, and the backend
   simply had nothing to resolve it against until step 2.

**What this establishes.** A missing root span is *sufficient* to hold a
trace `IN_PROGRESS`, and the arrival of exactly that root is sufficient
to release it — one variable, changed on an already-`IN_PROGRESS` trace,
with the transition following.

**What it does not establish.** The converse. `IN_PROGRESS` does not
imply a missing root span; other causes are not ruled out, and this
experiment says nothing about them.

So treat this as the first thing to check, not as a diagnosis. The Slack
Gateway is now the caller and does emit a root `concierge.request` span,
so this particular cause should no longer apply to a Slack-originated
trace — but that has not been confirmed live. If `IN_PROGRESS` persists,
verify whether the caller's own root span reached the Collector; a missing
root is one known sufficient cause of this state, not its only possible
one.

### Still not verified

- **The Slack Gateway as the caller, on the live stack.** The link from
  `concierge.request` through `orchestrator.dispatch` to `POST /dispatch`
  is implemented and covered by automated tests on both sides
  (`apps/slack-gateway/tests`, `services/orchestrator/tests`), but no real
  Slack message has been sent through it and no resulting trace has been
  observed in Phoenix or MLflow. Implemented and automatically verified is
  not live operationally verified; this one is still the latter's to
  prove, and should be recorded with the same provenance as the manual run
  above.
- **The error paths, on the live stack.** Hermes returning a non-success
  status, an unreachable Hermes, and an unusable response body are all
  covered by automated tests, including the `error.type` values recorded
  — but none has been observed live.
- **Hermes Agent's outbound MCP calls**, which remain an upstream gap
  (see "The chain" above) and are unaffected by any of this.
