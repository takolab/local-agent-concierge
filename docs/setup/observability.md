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

The Slack Gateway's outgoing request to Hermes Agent now injects a W3C Trace
Context `traceparent` header using the OpenTelemetry API's global propagator,
so the request carries the `hermes.request` span's trace ID and span ID.
Hermes Agent does not yet extract this header or create spans of its own, so
the trace does not yet continue past that HTTP boundary.

The following are not implemented yet:

- Google Calendar MCP instrumentation
- Hermes Agent instrumentation
- Hermes Agent extracting incoming trace context to join the same distributed trace
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

Application telemetry schemas and redaction rules will be designed together with the first application instrumentation.

## Next Step

The next observability implementation phase will instrument the Slack Gateway using OpenTelemetry.

The intended flow is:

```text
Slack Gateway
      |
      | OpenTelemetry
      v
OpenTelemetry Collector
      |
      +------> Phoenix
      |
      +------> MLflow
```

The Slack Gateway should depend only on OpenTelemetry and the Collector endpoint.

It should not use Phoenix-specific or MLflow-specific tracing SDKs during the initial backend comparison.
