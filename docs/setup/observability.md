# Observability Setup

This document describes the local observability infrastructure for Local Agent Concierge.

## Architecture

The initial observability architecture follows a backend-neutral OpenTelemetry design.

```text
Application Services
        |
        | OTLP
        v
OpenTelemetry Collector
        |
        +------> Phoenix
        |
        +------> MLflow
```

Application services should send telemetry only to the OpenTelemetry Collector.

Phoenix and MLflow are backend implementations behind the Collector. Application instrumentation should not depend directly on either backend during the initial comparison.

## Current Scope

The current observability infrastructure includes:

- OpenTelemetry Collector
- Phoenix
- MLflow Tracking Server
- Persistent storage for Phoenix
- Persistent storage for MLflow
- OTLP trace ingestion through the Collector
- Trace fan-out from the Collector to Phoenix and MLflow
- Health checks
- Manual synthetic trace verification
- Collector-side redaction of known-sensitive span attributes (defense in
  depth behind application-side sanitization), see
  `docs/observability/collector-redaction.md`

The Slack Gateway's outgoing request to Hermes Agent injects a W3C Trace
Context `traceparent` header using the OpenTelemetry API's global propagator,
so the request carries the `hermes.request` span's trace ID and span ID.
Hermes Agent extracts this header (via OpenTelemetry auto-instrumentation
layered on the unmodified official image, see
`docs/observability/hermes-trace-context.md`) and creates a matching
`SERVER` span, so the trace continues past that HTTP boundary.

The Google Calendar MCP service exports its own OpenTelemetry traces to the
Collector (`service.name = google-calendar-mcp`), using the `mcp` SDK's
built-in per-tool-call instrumentation — see
`docs/observability/google-calendar-mcp-telemetry.md`.

The following are not implemented yet:

- Spans for Hermes Agent's own internal processing (LLM calls, tool calls)
- A dedicated span for the Google Calendar API HTTP request itself
- A confirmed, real, Slack-triggered end-to-end trace continuing from Hermes
  Agent into a Google Calendar MCP tool span
- Phoenix-specific application instrumentation
- MLflow-specific application instrumentation
- Evaluation datasets

## Container Images

The observability lab uses the latest container images:

```text
arizephoenix/phoenix:latest
ghcr.io/mlflow/mlflow:latest
otel/opentelemetry-collector-contrib:latest
```

This is intentional so that the backend comparison tracks current releases.

Pull the latest images with:

```bash
docker compose pull phoenix mlflow otel-collector
```

Check the OpenTelemetry Collector version:

```bash
docker compose run --rm otel-collector --version
```

Check the MLflow version:

```bash
curl -fsS http://127.0.0.1:5000/version && echo
```

If an upstream update introduces a regression, investigate the incompatibility before deciding whether a temporary version pin is necessary.

## Services and Ports

### OpenTelemetry Collector

Host endpoints:

```text
OTLP/gRPC   127.0.0.1:4317
OTLP/HTTP   127.0.0.1:4318
Health      127.0.0.1:13133
```

Application containers on the Compose network should use:

```text
otel-collector:4317
otel-collector:4318
```

Collector configuration:

```text
infra/observability/otel-collector.yaml
```

### Phoenix

Phoenix UI:

```text
http://localhost:6006
```

Health endpoint:

```text
http://127.0.0.1:6006/healthz
```

The Collector exports traces to Phoenix through:

```text
http://phoenix:6006
```

Infrastructure smoke-test traces are assigned to:

```text
local-agent-concierge-infra-smoke-test
```

### MLflow

MLflow UI:

```text
http://localhost:5000
```

Health endpoint:

```text
http://127.0.0.1:5000/health
```

The Collector exports traces to MLflow through:

```text
http://mlflow:5000
```

MLflow requires an experiment ID for OTLP trace ingestion.

The default local configuration is:

```text
MLFLOW_EXPERIMENT_ID=0
```

This can be overridden in the local `.env` file later.

## Persistent Storage

Phoenix and MLflow use separate Docker named volumes:

```text
phoenix-data
mlflow-data
```

Conceptually:

```text
Phoenix container
    |
    v
phoenix-data
    |
    v
Phoenix persistent data

MLflow container
    |
    v
mlflow-data
    |
    +-- mlflow.db
    |
    +-- artifacts/
```

Hermes Agent data remains separate under:

```text
./data/hermes
```

Google Calendar credentials and tokens remain separate under:

```text
./data/google-calendar
```

Do not run the following command unless deleting persistent observability data is intentional:

```bash
docker compose down -v
```

The `-v` option removes Docker named volumes.

## Starting the Observability Infrastructure

Start the three observability services:

```bash
docker compose up -d phoenix mlflow otel-collector
```

Check their status:

```bash
docker compose ps phoenix mlflow otel-collector
```

Expected state:

```text
Phoenix         Up (healthy)
MLflow          Up (healthy)
otel-collector  Up
```

## Stopping the Observability Infrastructure

Stop the services while preserving persistent data:

```bash
docker compose stop otel-collector phoenix mlflow
```

Start them again with:

```bash
docker compose up -d phoenix mlflow otel-collector
```

## Configuration Validation

Validate Docker Compose:

```bash
docker compose config --quiet
```

Validate the Collector configuration:

```bash
docker compose run --rm otel-collector \
  validate \
  --config=/etc/otelcol-contrib/config.yaml
```

A successful validation exits without an error.

## Health Checks

### Phoenix

```bash
curl -o /dev/null -sS -w '%{http_code}\n' \
  http://127.0.0.1:6006/healthz
```

Expected:

```text
200
```

### MLflow

```bash
curl -o /dev/null -sS -w '%{http_code}\n' \
  http://127.0.0.1:5000/health
```

Expected:

```text
200
```

### OpenTelemetry Collector

```bash
curl -o /dev/null -sS -w '%{http_code}\n' \
  http://127.0.0.1:13133/
```

Expected:

```text
200
```

## Collector Trace Pipeline

The current Collector trace pipeline is:

```text
OTLP receiver
    |
    v
redaction processor
    |
    v
batch processor
    |
    +------> debug exporter
    |
    +------> Phoenix
    |
    +------> MLflow
```

The debug exporter currently uses `verbosity: detailed` so that smoke-test spans can be inspected through Docker logs.

It is intended for local development and validation and may be removed or disabled later.

The `redaction` processor runs before `batch`, so it applies uniformly to
every exporter below it, the debug exporter included. It is a
defense-in-depth layer behind application-side sanitization (Slack Gateway,
Google Calendar MCP already avoid placing sensitive data in span
attributes by design) — it masks a fixed list of known-sensitive attribute
keys in case application code regresses or some other instrumentation
exporting into this Collector attaches one by mistake. See
`docs/observability/collector-redaction.md` for the full design rationale,
attribute list, and verification, and the "Collector Redaction Smoke Test"
section below for how to check it locally.

## Synthetic Fan-Out Smoke Test

Application instrumentation is not required to validate the infrastructure.

A synthetic OTLP/HTTP trace can be sent directly to the Collector:

```bash
python3 - <<'PY'
import json
import secrets
import time
import urllib.request

trace_id = secrets.token_hex(16)
span_id = secrets.token_hex(8)

start_ns = time.time_ns()
end_ns = start_ns + 10_000_000

payload = {
    "resourceSpans": [
        {
            "resource": {
                "attributes": [
                    {
                        "key": "service.name",
                        "value": {
                            "stringValue": "observability-infra-smoke-test"
                        },
                    },
                    {
                        "key": "deployment.environment.name",
                        "value": {
                            "stringValue": "local"
                        },
                    },
                ]
            },
            "scopeSpans": [
                {
                    "scope": {
                        "name": "manual-otlp-smoke-test"
                    },
                    "spans": [
                        {
                            "traceId": trace_id,
                            "spanId": span_id,
                            "name": "collector-fanout-smoke-test",
                            "kind": 1,
                            "startTimeUnixNano": str(start_ns),
                            "endTimeUnixNano": str(end_ns),
                            "attributes": [
                                {
                                    "key": "test.type",
                                    "value": {
                                        "stringValue": "infrastructure-fanout-smoke-test"
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ]
}

request = urllib.request.Request(
    "http://127.0.0.1:4318/v1/traces",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
    },
    method="POST",
)

with urllib.request.urlopen(request, timeout=5) as response:
    print("HTTP status:", response.status)

print("trace_id:", trace_id)
print("span_id:", span_id)
PY
```

Expected output begins with:

```text
HTTP status: 200
```

## Verify the Collector

Check that the span reached the Collector:

```bash
docker compose logs --since=2m otel-collector \
  | grep -C 10 "collector-fanout-smoke-test"
```

Check for exporter failures:

```bash
docker compose logs --since=2m otel-collector \
  | grep -Ei "error|failed|refused|retry|400|401|403|404|500"
```

No exporter error should be reported for the smoke-test trace.

## Collector Redaction Smoke Test

The automated version of this check is
`infra/observability/tests/test_redaction.py`:

```bash
pip install -r infra/observability/tests/requirements.txt
python -m pytest infra/observability/tests
```

This requires a Docker daemon (it runs its own throwaway Collector
container on ports 24318/24133 so it does not conflict with an
already-running `docker compose up`) and uses only synthetic placeholder
values — see `docs/observability/collector-redaction.md`.

To check it manually against the real running stack instead, send a
synthetic span carrying both a known-sensitive attribute and a safe one:

```bash
python3 - <<'PY'
import json
import secrets
import time
import urllib.request

payload = {
    "resourceSpans": [
        {
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "synthetic-redaction-test"}},
                ]
            },
            "scopeSpans": [
                {
                    "scope": {"name": "manual-redaction-smoke-test"},
                    "spans": [
                        {
                            "traceId": secrets.token_hex(16),
                            "spanId": secrets.token_hex(8),
                            "name": "collector-redaction-smoke-test",
                            "kind": 1,
                            "startTimeUnixNano": str(time.time_ns()),
                            "endTimeUnixNano": str(time.time_ns() + 10_000_000),
                            "attributes": [
                                {"key": "operation.name", "value": {"stringValue": "synthetic-operation"}},
                                {"key": "authorization", "value": {"stringValue": "Bearer fake-secret-value-for-redaction-test"}},
                            ],
                        }
                    ],
                }
            ],
        }
    ]
}

request = urllib.request.Request(
    "http://127.0.0.1:4318/v1/traces",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=5) as response:
    print("HTTP status:", response.status)
PY
```

Then confirm both halves of the redaction contract in the same log output:

```bash
docker compose logs --since=2m otel-collector \
  | grep -A 15 "collector-redaction-smoke-test"
```

Expected: `operation.name: Str(synthetic-operation)` is unchanged, and
`authorization: Str(****)` — the masked placeholder, not
`fake-secret-value-for-redaction-test` — appears instead of the raw value.

## Verify Phoenix

Open:

```text
http://localhost:6006
```

Open the project:

```text
local-agent-concierge-infra-smoke-test
```

Verify that this trace or span exists:

```text
collector-fanout-smoke-test
```

## Verify MLflow

Open:

```text
http://localhost:5000
```

When using:

```text
MLFLOW_EXPERIMENT_ID=0
```

open the Default experiment and verify that this trace exists:

```text
collector-fanout-smoke-test
```

The same synthetic workload should therefore be visible in both Phoenix and MLflow.

## Persistent Storage Verification

MLflow persistence was verified by:

1. Creating an experiment.
2. Removing the MLflow container.
3. Recreating the MLflow container.
4. Confirming that the experiment remained visible in the MLflow UI.

The MLflow SQLite database is stored at:

```text
/mlflow/mlflow.db
```

Its existence can be checked with:

```bash
docker compose exec mlflow python -c '
from pathlib import Path

p = Path("/mlflow/mlflow.db")

print("exists:", p.exists())
print("size:", p.stat().st_size if p.exists() else 0)
'
```

## Security

Infrastructure smoke tests must use synthetic data only.

Do not place the following information in trace attributes:

- Slack message contents
- Slack tokens
- OAuth access tokens
- OAuth refresh tokens
- Google Calendar event details
- API credentials
- Passwords
- Personal information

Application instrumentation (Slack Gateway, Google Calendar MCP) is the
primary defense against exporting the data above, verified by their own
tests. The shared Collector's `redaction` processor adds a second,
defense-in-depth layer that masks a fixed list of known-sensitive attribute
keys before any exporter sees them — see
`docs/observability/collector-redaction.md` for the design rationale and
`docs/roadmap.md` Milestone 5 for the checklist status. Neither layer is a
general-purpose PII detector; both target the specific categories listed
above.

## Next Step

The Slack Gateway, Hermes Agent's HTTP boundary, and the Google Calendar MCP
service are now instrumented, and the shared Collector applies a
defense-in-depth redaction layer to known-sensitive attributes (see
`docs/observability/collector-redaction.md`). The next observability work is
comparing Phoenix and MLflow using the resulting traces (see Milestone 5 in
`docs/roadmap.md`), and later extending instrumentation to the Orchestrator,
other Agents, and remaining tool integrations (Milestone 9).

Each instrumented service depends only on OpenTelemetry and the Collector
endpoint, and does not use Phoenix-specific or MLflow-specific tracing SDKs.
