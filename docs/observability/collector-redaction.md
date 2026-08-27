# OpenTelemetry Collector Redaction

This document records the second, Collector-side layer of defense against
exporting sensitive telemetry: what it protects against, why the
`redaction` processor was chosen over the alternatives, the exact attribute
list and its reasoning, and how it was verified.

## Background: two layers, not one

Application instrumentation is already the first line of defense and is
covered by its own tests:

- `apps/slack-gateway/src/slack_gateway/telemetry.py` /
  `apps/slack-gateway/tests/test_telemetry.py`
- `mcp/google-calendar/src/google_calendar_mcp/telemetry.py` /
  `mcp/google-calendar/tests/test_telemetry.py` (see
  `docs/observability/google-calendar-mcp-telemetry.md`)

Neither service places Slack message bodies, request/response bodies,
credentials, `Authorization` headers, OAuth tokens, calendar event/attendee
data, raw exception messages, or Slack/conversation identifiers into span
attributes. Google Calendar MCP tool failures are reported as
`error.type = "tool_error"` rather than the underlying exception. This
remains the primary, authoritative boundary — this change does not weaken
or replace it, and does not modify any application code.

This task adds a **second** layer inside the shared OpenTelemetry Collector
(`infra/observability/otel-collector.yaml`): a `redaction` processor that
masks a fixed list of known-sensitive span attribute keys before *any*
exporter — `debug` included, not just Phoenix and MLflow — sees them. Its
purpose is narrow: catch the case where application code regresses, or
where some other instrumentation exporting into this same shared Collector
(for example Hermes Agent's own OpenTelemetry auto-instrumentation, see
`docs/observability/hermes-trace-context.md`) attaches one of these keys by
mistake. It is not a general-purpose PII detector and does not scan
arbitrary free-text attribute values for embedded secrets — see
"Known limitations" below.

## Processor choice

Investigated first: the Collector image already pinned in
`docker-compose.yml` (`otel/opentelemetry-collector-contrib:latest`) is at
version `0.159.0` at the time of this change
(`docker run --rm otel/opentelemetry-collector-contrib:latest --version`),
the `contrib` distribution, which is what both candidate processors below
ship in — no image or dependency change was needed either way.

Two candidates were considered:

- **`attributes` processor** with one `action: delete` entry per key.
  Simple and stable, but key matching is exact-string only — no regex — so
  covering a key *family* (e.g. any key containing `token`) requires
  enumerating every current spelling and would silently miss a future
  variant (a new semantic-convention field, a different casing) unless the
  list is manually kept in sync.
- **`redaction` processor**
  ([contrib source](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/redactionprocessor)).
  Supports regex key patterns (`blocked_key_patterns`), and — with
  `allow_all_keys: true` — fails **open** for any attribute not explicitly
  matched, rather than requiring an exhaustive allowlist of every
  legitimate key. OpenTelemetry's own security guidance names this
  processor specifically for this use case: "You can use the OpenTelemetry
  Collector's `redaction` processor to obfuscate or scrub sensitive data
  before exporting it to a backend"
  ([opentelemetry.io/docs/security/config-best-practices](https://opentelemetry.io/docs/security/config-best-practices/)).

The `redaction` processor was chosen. The fail-open behavior mattered most:
this repository's telemetry is still evolving (Milestone 9 plans more
instrumentation), and an allowlist-shaped defense would risk silently
dropping legitimate future attributes whenever someone forgot to update it
— a worse outcome for observability than the narrow, explicit blocklist
this uses instead. It is not deprecated; contrib status is beta for traces,
alpha for logs/metrics (this repository only has a traces pipeline).

## Processor ordering

```text
receivers (otlp)
        |
        v
processors: [redaction, batch]
        |
        v
exporters: [debug, otlphttp/phoenix, otlphttp/mlflow]
```

The Collector applies the processors of a pipeline in the order listed, and
one pipeline definition is shared by every exporter attached to it
("The order of the processors in a pipeline determines the order of the
processing operations that the Collector applies to the signal" —
[opentelemetry.io/docs/collector/configuration](https://opentelemetry.io/docs/collector/configuration/)).
Because `infra/observability/otel-collector.yaml` has always used a single
`traces` pipeline fanning out to all three exporters, placing `redaction`
first in that one shared `processors` list is sufficient to guarantee it
runs before `debug`, `otlphttp/phoenix`, and `otlphttp/mlflow` alike —
there is no separate path any of them could see unredacted data through.
`redaction` runs before `batch` (rather than after) following the same
"filter/transform before batch" convention the batch processor's own docs
recommend for sampling-type processors — batching should not do extra work
packaging data that redaction would still need to touch.

## What is redacted, and why

`processors.redaction.blocked_key_patterns` in
`infra/observability/otel-collector.yaml`, grouped by rationale:

| Pattern | Protects against | Source |
|---|---|---|
| `.*token.*`, `.*api[_-]?key.*` | OAuth access/refresh tokens, API keys | App boundary (roadmap.md); mirrors the redaction processor's own documented example |
| `.*secret.*`, `.*password.*` | Client secrets, credential material | Generic HTTP/OpenTelemetry auto-instrumentation credential-key convention (not observed in this repo's own instrumentation) |
| `.*credential.*` | Credentials (general) | App boundary: "credentials" |
| `(?i)^authorization$`, `http\.request\.header\.authorization`, `http\.response\.header\.authorization` | `Authorization` header, including semantic-convention captured-header keys | App boundary: "Authorization header"; the `http.request.header.*` form is the generic auto-instrumentation convention, added defensively |
| `.*email.*` | Attendee/user email addresses | App boundary: "attendee / user email" |
| `slack\.(user\|channel\|workspace\|team)\.(id\|name\|email)` | Slack user/channel/workspace identifiers | App boundary: "Slack user / channel / workspace identifiers" |
| `^conversation\.id$` | Conversation identifier | App boundary: "conversation identifier" |
| `slack\.message\.(text\|body\|content)` | Slack message body | App boundary: "Slack message body" (deliberately does not match the real, safe `slack.message.threaded` boolean) |
| `calendar\.event\.(id\|title\|summary)`, `^calendar\.id$` | Calendar event ID/title/summary, calendar ID | App boundary: "Calendar event title / summary", "Calendar event ID", "calendar ID" |
| `(request\|response)\.body` | Raw request/response bodies | App boundary: "request / response body" |
| `exception\.(message\|stacktrace)`, `^error\.message$` | Raw exception text | App boundary: "raw exception message"; `exception.message`/`exception.stacktrace` are the standard OpenTelemetry semantic-convention exception-event attribute names, in case `record_exception` is ever used upstream of this Collector |

None of these were invented speculatively beyond what is listed above: the
"App boundary" rows are the sensitive-data categories this repository's own
application instrumentation and documentation already name (see
`docs/roadmap.md` Milestone 5 and `docs/architecture.md` Security
Boundaries); the remaining rows are the specific, narrow allowance for
"credential-shaped keys generic HTTP/OpenTelemetry auto-instrumentation is
known to attach" rather than an imagined, broad key list.

**Deliberately excluded from `blocked_key_patterns`:** a broad `.*id.*` or
`.*\.id$` pattern. `jsonrpc.request.id` (Google Calendar MCP, a per-request
counter, not sensitive) would match either and be masked unnecessarily —
this is exactly the kind of over-broad pattern the safe-attribute
preservation check below guards against.

**Deliberately not implemented:** `blocked_values` (regex over attribute
*values*, applied to every allowed attribute across every span). The
upstream processor supports it and the official example uses it for credit
card numbers, but scanning arbitrary attribute values for sensitive
*content* — as opposed to removing known-sensitive attribute *keys* — is
the general-purpose free-text PII detection this task's scope explicitly
excludes. If a secret value ever reaches a span under a key not in the
table above, this layer will not catch it; the application-side boundary
is what prevents that from happening in the first place.

## What is preserved

`processors.redaction.allow_all_keys: true` means every attribute is kept
by default; only the patterns above remove anything. This is deliberately
fail-open: an attribute this Collector doesn't already know to be sensitive
is left alone rather than dropped, so instrumentation added later (e.g.
Milestone 9) is not silently stripped until someone remembers to update an
allowlist.

`processors.redaction.ignored_keys` additionally guarantees the following
currently-real, low-cardinality attributes are never touched, regardless of
the blocked patterns (ignored keys are checked first and always win, per
the processor's documented precedence):

```text
error.type
mcp.method.name
mcp.protocol.version
gen_ai.operation.name
gen_ai.tool.name
jsonrpc.request.id
concierge.request.source
concierge.downstream.service
concierge.operation
slack.event.type
slack.message.threaded
http.scheme
http.host
http.route
http.method
http.status_code
```

The first nine come from Slack Gateway and Google Calendar MCP's own
instrumentation; the `http.*` ones are Hermes Agent's auto-instrumented
`/v1/responses` server span (`docs/observability/hermes-trace-context.md`).

**Resource attributes are structurally out of scope for this processor.**
`service.name` and `service.namespace` are resource-level attributes (set
once via `Resource.create(...)` in each service's `telemetry.py`), and the
redaction processor only operates on "span, log, and metric datapoint"
attributes — a different level of the telemetry data than resource
attributes. This was confirmed empirically (see Verification below): a
probe span's `service.name` resource attribute reached the `debug` exporter
unchanged.

## Verification

### Automated (primary)

`infra/observability/tests/test_redaction.py` (`python -m pytest
infra/observability/tests`, requires a Docker daemon — same pattern as
`apps/hermes-agent/tests/test_trace_context_propagation.py`). It runs the
real `otel/opentelemetry-collector-contrib` image against this repository's
actual `infra/observability/otel-collector.yaml` and:

1. Validates the configuration (`otelcol-contrib validate --config=...`)
   exits `0`.
2. Confirms the Collector starts and its health check reports `200`.
3. Sends one synthetic OTLP/HTTP span carrying every `blocked_key_patterns`
   category alongside several safe attributes, and confirms via the
   `debug` exporter's logged output that:
   - none of the synthetic secret/PII *values* reach the export path,
   - each sensitive *key* is present but masked (`Str(****)`), proving the
     processor actively matched and rewrote them rather than, say, failing
     to parse the attribute at all,
   - `redaction.masked.count` equals the exact number of sensitive
     attributes sent,
   - every safe span attribute and both safe resource attributes
     (`service.name`, `service.namespace`) are unchanged.

Only clearly-synthetic placeholder values are used, matching this task's
constraints — never a real secret, token, or personal identifier:

```text
fake-secret-value-for-redaction-test
person@example.invalid
fake-calendar-event-id
fake-calendar-id
synthetic-message-body
U-SYNTHETIC0001 / C-SYNTHETIC0001 / T-SYNTHETIC0001
synthetic-conversation-id
synthetic sensitive exception detail
```

### Docker Compose (regression)

`docker compose up -d --force-recreate otel-collector` against the full
local stack (Phoenix and MLflow already running), followed by sending the
same kind of synthetic probe span over the real OTLP/HTTP port. Confirmed:

- The Collector health check still returns `200`.
- The `debug` exporter's output shows the same masking behavior as the
  automated test, against the real Compose-networked container.
- `docker compose logs otel-collector` shows no export errors for either
  `otlphttp/phoenix` or `otlphttp/mlflow` after the probe — both backends
  accepted the (now-redacted) batch.
- The already-running `hermes-agent`, `slack-gateway`, and
  `google-calendar-mcp` containers remained healthy with no new errors
  after the Collector was recreated.

Directly querying Phoenix's and MLflow's own APIs for the redacted span's
content was not automated — the automated `debug`-exporter check above
already gives a machine-verifiable pass/fail without depending on either
backend's specific query interface (keeping this Collector-only, per
scope). Confirming the synthetic span in the Phoenix and MLflow UIs remains
a manual, optional follow-up, the same way the existing "Verify Phoenix" /
"Verify MLflow" sections in `docs/setup/observability.md` already are for
the pre-existing fan-out smoke test.

### Application regression

`docker compose --profile test run --rm google-calendar-mcp-test` and
`... slack-gateway-test` both still pass unchanged — this change does not
modify application code or application telemetry behavior, only the shared
Collector configuration.

## Known limitations

- **Key-based only.** This layer removes attributes by *key*. It cannot
  detect a secret or personal data value embedded inside an unrelated,
  otherwise-safe attribute's *value* (for example, a raw exception string
  containing a token, attached under a generic key like `message`). That
  is exactly what the application-side boundary is responsible for
  preventing, and is out of scope for this Collector layer by design (see
  "Deliberately not implemented" above).
- **Static, manually-curated list.** A newly introduced sensitive attribute
  family requires a matching update to `blocked_key_patterns` in
  `infra/observability/otel-collector.yaml`; nothing infers new sensitive
  keys automatically.
- **Traces only.** This repository has only a `traces` pipeline; the
  `redaction` processor's log/metric support (alpha) is unused and
  untested here.
- **Masks rather than removes.** A blocked key's value becomes the literal
  string `****`, not an absent attribute — the key name itself is still
  visible in exported telemetry. This was an intentional, off-the-shelf
  choice within scope (matching the processor's documented default and the
  official credit-card-number example), not a preference for masking over
  removal specifically.
- **Defense-in-depth, not a substitute.** If application instrumentation
  were changed to intentionally rely on this layer instead of its own
  sanitization, that would defeat the point. The application-side tests
  referenced in "Background" remain the primary, required boundary.

## Ownership boundary

Everything above is implemented entirely within `local-agent-concierge`
(`infra/observability/otel-collector.yaml` and
`infra/observability/tests/**`). No change was made to application code in
`apps/slack-gateway` or `mcp/google-calendar`, to Hermes Agent, or to
Phoenix or MLflow. The existing `otel/opentelemetry-collector-contrib`
image already in `docker-compose.yml` was reused as-is.
