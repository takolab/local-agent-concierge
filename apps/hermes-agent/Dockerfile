# Derived Hermes Agent image that adds OpenTelemetry auto-instrumentation
# for the aiohttp-based API server, so incoming W3C `traceparent` headers on
# POST /v1/responses (and the other API server routes) are extracted and
# turned into a child span of the caller's span instead of being ignored.
#
# This does not fork or patch NousResearch/hermes-agent source. It only
# layers standard OpenTelemetry auto-instrumentation packages into the
# existing virtualenv.
#
# Base image pinned to a specific upstream release tag (not `latest`) for
# reproducibility — see docs/roadmap.md Milestone 5 for the investigation
# that led to this approach.
FROM nousresearch/hermes-agent:v2026.8.19

# The base image already ships opentelemetry-api/sdk 1.39.1 and
# opentelemetry-exporter-otlp-proto-http 1.39.1 (used internally by Hermes'
# own optional gateway-health exporter). Pin the added instrumentation
# packages to the matching 0.60b1 contrib release train so this install
# does not upgrade or otherwise disturb those existing packages.
RUN uv pip install --python /opt/hermes/.venv/bin/python3 \
    "opentelemetry-distro==0.60b1" \
    "opentelemetry-instrumentation==0.60b1" \
    "opentelemetry-instrumentation-aiohttp-server==0.60b1"

# Activate auto-instrumentation via PYTHONPATH + sitecustomize.py — the same
# mechanism the `opentelemetry-instrument` launcher itself uses internally —
# instead of wrapping the container's CMD with that launcher.
#
# This distinction matters specifically for this image: on a profile with
# any prior run (i.e. any real deployment), Hermes' own cont-init
# (`02-reconcile-profiles`) restores a dynamically s6-supervised
# `gateway-<profile>` service on every container start, independently of
# whatever command Docker's CMD passes. A CMD-level `opentelemetry-instrument`
# wrapper only instruments that one foreground CMD invocation; Hermes'
# supervised restart path (and the reconcile-created service) starts a
# completely separate, uninstrumented process, which is what previously
# produced a "gateway already running" conflict when supervision was
# disabled to work around it.
#
# A container-wide PYTHONPATH has neither problem: s6 (via with-contenv)
# re-injects the full container environment into every supervised process it
# starts, so `sitecustomize.py` — and therefore instrumentation — activates
# on whichever process ends up actually serving requests, without needing to
# control Hermes' own supervision behavior at all.
RUN test -f /opt/hermes/.venv/lib/python3.13/site-packages/opentelemetry/instrumentation/auto_instrumentation/sitecustomize.py

ENV PYTHONPATH="/opt/hermes/.venv/lib/python3.13/site-packages/opentelemetry/instrumentation/auto_instrumentation"
