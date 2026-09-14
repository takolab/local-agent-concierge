# Slack → Orchestrator → Hermes: Live Validation Runbook

A Human-executable procedure for validating the runtime path in an actual
running stack, plus the evidence record for each run.

> **Three kinds of statement appear in this document, and the difference
> between the last two is what keeps the evidence honest.**
>
> - **Expected** — derived from the repository at the SHA named in each
>   section: span names, contracts, exit codes. Checked against the code by
>   `infra/observability/tests/test_live_validation.py`.
> - **Referenced observation** — something seen on a past run, cited in a
>   normative section to justify why a step exists or is shaped as it is
>   (§4's Collector exit, §5's tool-capability evidence, §7's
>   evidence-channel limits). Always attributed to a date or a document, and
>   never evidence about *your* run.
> - **Current-run evidence** — what a validation actually established, in
>   "[Run records](#run-records)" **only**.
>
> The invariant is not that history cannot be cited outside Run records —
> the procedure would be unjustifiable without it. It is that **no section
> may read as evidence for the run in progress.** If a statement would
> change a reader's conclusion about the current run, it belongs in a run
> record, with its own provenance.

## 1. Purpose

[PR #43](https://github.com/takolab/local-agent-concierge/pull/43) rewired
the Slack Gateway to dispatch through the Orchestrator instead of calling
Hermes Agent directly. It shipped with automated tests only.

```text
merged implementation  !=  live operational verification
```

This runbook is how that gap gets closed, repeatably: it validates that the
rewiring works against real containers, a real model, and the real
Collector → Phoenix/MLflow pipeline.

It is the Slack-caller counterpart to
`docs/observability/orchestrator-trace-context.md`'s "End-to-end
verification (manual)", which validated the same Orchestrator → Hermes hops
with a synthetic `curl` caller rather than a Slack message.

## 2. Validation boundary

### In scope

```text
a real Slack event enters the Slack Gateway
the Gateway builds an AgentRequest and calls POST /dispatch
the Orchestrator dispatches to the registered "hermes" Agent
Hermes Agent returns through the Orchestrator
the Gateway replies in the Slack thread
W3C Trace Context continues across every boundary
the expected safe telemetry is emitted
the defined sensitive values are absent from telemetry
```

### Out of scope

Not tested here, and not to be improvised into the procedure:

```text
idempotency guarantees          execution deadlines
failure provenance              automatic Agent selection
multi-agent routing             Approval Service
Calendar write authorization    destructive tool execution
incident simulation             network fault injection
timeout injection               Hermes source modification
```

The first three do not exist yet and are tracked in `docs/roadmap.md`
Milestone 6. Exercising the failure paths (stopping a container) is a
*separate* procedure documented in `docs/setup/slack.md`; this runbook is
the success path.

## 3. Exact runtime provenance

Record this **before** sending anything. `provenance` gathers it:

```bash
python3 infra/observability/live_validation.py provenance
```

It reports the repository SHA, whether the working tree is clean, and for
each of the eight services below: a **12-character container ID prefix**,
the local image ID, and the container's start time.

```text
slack-gateway   orchestrator   hermes-agent   google-calendar-mcp
ollama          otel-collector phoenix        mlflow
```

`google-calendar-mcp` is included because Hermes can reach it on this path;
`phoenix` and `mlflow` because they are where the evidence is read from. A
record that pins the trace producers but not those three cannot be
correlated against them later. §13's template has a row for each — record
all eight, not a summary `YES`. The prefix is what `--expect-container`
compares against — twelve characters is what `docker ps` shows and what the
tool requires as a minimum.

**Record the `orchestrator` one.** §8's credential scan takes it as
`--expect-container` so the credential is read from the instance that
handled the request. Without it, a service recreated between the request
and the scan resolves to a *new* container with a *new* credential: the
scan finds that one absent and reports clean while the credential the
request actually used is the one that leaked. `scan` will not run a
service-backed credential read unbound — the two options are a required
pair.

**What each kind of service can actually prove.** The four kinds are not
equivalent, and a record should not imply they are. Every service in the
set above appears here, so nothing has to be inferred by elimination:

| Service | Identity recorded | What it establishes |
|---|---|---|
| `slack-gateway`, `orchestrator` | local image ID | Nothing on its own — a local image has no registry digest and no mechanical link to a commit. Use the source check below, which covers both. |
| `google-calendar-mcp` | local image ID | Correlation identity only. It is locally built, but its source is **outside** the source check's boundary — nothing below compares it. |
| `hermes-agent` | local image ID | Derived from the pinned upstream image in `apps/hermes-agent/Dockerfile`; the pin is in the repository at the recorded SHA. It copies no repository file, so there is nothing to hash-compare. |
| `ollama`, `otel-collector`, `phoenix`, `mlflow` | local image ID | Upstream `:latest` — **not a reproducible identity**. The ID lets a later investigation correlate, nothing more. |

`phoenix` and `mlflow` are included because they are where the evidence is
*read from*: a record pinning the producers but not the backends cannot be
correlated against what those backends held.

**Do not use image timestamps.** `docker inspect <image> --format
'{{.Created}}'` reports when the image *config* was created, and a rebuild
whose layers all hit the cache keeps the original date. On this stack it
reported `2026-09-08` for an orchestrator image that in fact contains
2026-09-10 source. It is not evidence of when anything was built.

**The check: compare the running image's inputs against the recorded
commit.** Not against your working tree — that moves. Against the SHA this
run is pinned to:

```bash
SHA=<the repository SHA recorded above>

check() {                       # check <compose-service> <repo-dir>
  ok=1
  for f in $(git ls-tree -r --name-only "$SHA" -- "$2/src" "$2/pyproject.toml"); do
    case "$f" in
      */pyproject.toml) dst=/app/pyproject.toml ;;
      *)                dst="/app/src/${f#$2/src/}" ;;
    esac
    c=$(docker compose exec -T "$1" sha256sum "$dst" 2>/dev/null | cut -c1-12)
    r=$(git show "$SHA:$f" | sha256sum | cut -c1-12)
    [ "$c" = "$r" ] || { echo "DIFFERS $f"; ok=0; }
  done
  for f in $(git ls-tree -r --name-only "$SHA" -- packages/agent-contracts/src/agent_contracts); do
    c=$(docker compose exec -T "$1" sha256sum \
          "/usr/local/lib/python3.12/site-packages/agent_contracts/$(basename $f)" \
          2>/dev/null | cut -c1-12)
    r=$(git show "$SHA:$f" | sha256sum | cut -c1-12)
    [ "$c" = "$r" ] || { echo "DIFFERS $f"; ok=0; }
  done
  # agent-contracts' own build definition: it selects the build backend and
  # the packages discovered, so it can change install semantics without any
  # file under src/agent_contracts changing. The Dockerfiles COPY the whole
  # directory, so it survives into the image and can be compared.
  c=$(docker compose exec -T "$1" sha256sum \
        /packages/agent-contracts/pyproject.toml 2>/dev/null | cut -c1-12)
  r=$(git show "$SHA:packages/agent-contracts/pyproject.toml" | sha256sum | cut -c1-12)
  [ "$c" = "$r" ] || { echo "DIFFERS packages/agent-contracts/pyproject.toml"; ok=0; }
  [ $ok -eq 1 ] && echo "$1: all inputs match $SHA"
}

check orchestrator   services/orchestrator
check slack-gateway  apps/slack-gateway
```

It covers every repository-controlled Python install input those
Dockerfiles copy in: the service's Python source, the service's
`pyproject.toml`, `packages/agent-contracts`'s Python files as installed
into site-packages, **and `packages/agent-contracts/pyproject.toml`**.

The last one is easy to leave out and matters: it selects the build backend
and which packages are discovered, so it can change what `pip install`
produces without a single file under `src/agent_contracts` differing. A
`YES` that skipped it would assert more than it checked.

Any `DIFFERS` means the running image was not built from the recorded SHA.
Diff that file and record whether the difference is executable code or only
comments — the distinction changes what the run's evidence is worth, and
only the diff can tell you.

**What this does not cover, and must not be implied by a `YES`:**

| Outside the boundary | Why |
|---|---|
| The `Dockerfile`s themselves | Not present in the image; a changed build definition cannot be detected from a running container |
| Build args, base-image pulls, install-time resolution | Not reconstructible after the fact |
| `hermes-agent`'s repository-controlled layer | Its Dockerfile pins an upstream base and adds instrumentation via `uv pip install` + `PYTHONPATH`; it copies no repository file, so there is nothing to hash-compare. Its identity here is the pin *in the repository at the recorded SHA*, not a verified image property |
| `google-calendar-mcp` | Built from its own directory and reachable from this path via Hermes' tools; its image id is recorded, its source is not compared |
| Upstream `:latest` images | Not reproducible identities at all (see the table above) |

Record the outcome as:

```text
Runtime Python inputs match recorded SHA:  YES / NO (detail)
Outside that boundary:                     Dockerfiles, build args,
                                           hermes-agent's instrumentation
                                           layer, upstream :latest images
```

rather than a bare "provenance consistent", which asserts a conclusion
without naming what was compared or what was not.

The Collector's config identity is the repository file
`infra/observability/otel-collector.yaml` at the recorded SHA; it is
bind-mounted read-only, so no separate identity is needed.

## 4. Preconditions

```bash
docker compose ps
```

| Service | Required state | Why |
|---|---|---|
| `slack-gateway` | running | receives the Slack event |
| `orchestrator` | running **and healthy** | the dispatch boundary under test |
| `hermes-agent` | running | the Agent |
| `ollama` | running and healthy | Hermes' model backend |
| `google-calendar-mcp` | running and healthy | `hermes-agent` depends on it to start |
| `otel-collector` | running | telemetry; **not** required for dispatch — see below |
| `phoenix`, `mlflow` | running and healthy | needed only to *read* the evidence |

**Check `otel-collector` with `docker compose ps -a`, not `ps`.** It is the
one service whose absence is invisible where it matters: dispatch keeps
working, Slack keeps replying, and no trace is ever exported — which reads
as "the rewiring is broken" rather than "the Collector is down".

It has a known failure mode after a Docker Desktop / WSL engine restart.
The single-file bind mount (`otel-collector.yaml` → `/etc/otelcol-contrib/config.yaml`)
does not survive it, and the container's own automatic restart fails on the
stale mount path:

```text
Exited (127)
  error mounting ".../docker-desktop-bind-mounts/..." to rootfs at
  "/etc/otelcol-contrib/config.yaml": not a directory
```

Recover by **replacing** the container, so the mount is resolved afresh,
and confirm the replacement actually happened:

```bash
docker compose ps -q otel-collector                       # container id before
docker compose up -d --no-deps --force-recreate otel-collector
docker compose ps -q otel-collector                       # must differ
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:13133/   # expect 200
```

`--force-recreate` is specified rather than a plain `up -d` because only
the explicit form *guarantees* a replacement: Compose may otherwise decide
the existing container's configuration is unchanged and simply start it
again, leaving whatever state the stale mount is in. Given that this
service's failure is invisible from Slack's point of view, a recovery step
that might quietly not recover is the wrong shape.

**What was actually observed, as opposed to inferred.** On 2026-09-10 the
exit above occurred after an engine restart, and a plain
`docker compose up -d --no-deps otel-collector` restored it — Compose
reported `Starting`/`Started`, i.e. it *started the existing container*
rather than replacing it, and the Collector came back healthy. So a
restart-shaped recovery is not known to be insufficient; this runbook does
not claim it is. `--force-recreate` is prescribed because it removes the
question, not because the weaker form was seen to fail. It was verified to
replace the container (id changed, health `200`).

Then confirm the Gateway can actually reach the Orchestrator — this is the
new hop, and the only precondition specific to it:

```bash
docker compose exec -T slack-gateway python -c \
  "import os,urllib.request; \
   print(urllib.request.urlopen(os.environ['ORCHESTRATOR_BASE_URL']+'/health',timeout=5).status)"
```

Expected: `200`. `GET /health` is liveness-only — it runs no business
logic and calls no Agent
(`services/orchestrator/src/orchestrator/http_server.py`). This is the one
step that starts a process inside a running container; it is a short-lived
read-only HTTP GET and changes nothing, but it is the reason this check is
a deliberate step rather than part of the read-only helper.

Confirm the Gateway's configuration is the post-#43 one (**names only —
never print values**):

```bash
docker inspect "$(docker compose ps -q slack-gateway)" \
  --format '{{range .Config.Env}}{{println .}}{{end}}' | cut -d= -f1 | sort \
  | grep -E 'SLACK|HERMES|ORCHESTRATOR|OTEL'
```

`cut -d= -f1` is load-bearing: it prints variable **names** only. Never run
this without it — the container's environment holds the Slack tokens. The
container id is resolved through `docker compose ps` rather than hardcoded,
because the container's name depends on the Compose project name.

Expected — exactly these four, and **no `HERMES_API_*`**:

```text
ORCHESTRATOR_BASE_URL
OTEL_EXPORTER_OTLP_ENDPOINT
SLACK_APP_TOKEN
SLACK_BOT_TOKEN
```

(The `grep` filters out the base image's own variables — `PATH`, `LANG`,
`PYTHON_VERSION` and friends — which are present and irrelevant.) A
`HERMES_API_*` here means the container predates #43 and is still on the
direct path; that is a [stop condition](#12-stop-conditions).

The `hermes` Agent registration is hardcoded in
`services/orchestrator/src/orchestrator/__main__.py` and cannot be absent
if the Orchestrator started at all: it raises on a missing
`HERMES_API_BASE_URL` / `HERMES_API_SERVER_KEY` rather than starting
without the Agent.

## 5. Test input

**Immediately before sending**, drop a marker that anchors §14's
side-effect window:

```bash
touch /tmp/live-validation-start
```

§14 searches for files newer than this marker rather than for files newer
than "ten minutes ago". The difference matters: a model run can take
minutes, and the Human then works through §7's trace check and §8's scan
before reaching §14. A window measured backwards from inspection time
therefore moves its own start forward while the validation proceeds, and a
request-side write can fall out of it — another way to get a clean result
from a check that did not look where it should have.

Then send **one** direct message to the Slack app, in a thread you control:

```text
Reply with exactly SLACK_GATEWAY_OK.
```

This is the input `docs/setup/slack.md` already uses for Slack Gateway
verification — reused rather than invented so both procedures produce
comparable evidence. It is deterministic enough to identify, carries no
personal data, and asks for a pure text response.

**It does not guarantee that no tool runs, and this runbook does not claim
it does.** What the repository actually supports:

- Hermes decides tool use itself; nothing in this repository can force a
  tool-free turn.
- The Calendar MCP server *is* reachable from this path — a real Slack
  message has been observed driving `tools/call list_events`
  (`docs/observability/google-calendar-mcp-telemetry.md`) — but
  `data/hermes/config.yaml` restricts it to a read-only `include:` list
  (`get_current_datetime`, `list_upcoming_events`, `list_events`,
  `list_busy_periods`, `list_free_periods`). No write tool is exposed.
- Whether Hermes' **terminal** toolset is available on the `/v1/responses`
  path is **not determinable from this repository**. `data/hermes/config.yaml`
  carries a `terminal:` settings block, and its `platform_toolsets:` map has
  no entry for the API-server path. Settings are not enablement, and this
  runbook does not resolve it.

That last point is a **residual risk, accepted rather than closed**: it is
mitigated by the input's phrasing, bounded by
[§14 post-validation state](#14-post-validation-state), and it is the
reason this runbook says "no unexpected side effect *observed*" rather than
"no side effect possible".

Do not use real personal data, real credentials, or a shared/production
Slack channel.

## 6. Expected request flow

```text
Slack event (Socket Mode)
  ↓
Slack Gateway            validates, dedupes, builds an AgentRequest
  ↓  POST /dispatch      {"agent_name": "hermes", "request": {...}}
Orchestrator             http_server.py → Orchestrator.dispatch()
  ↓
HermesAgent              the registered Agent adapter
  ↓  POST /v1/responses
Hermes Agent
  ↓
Ollama
  ↓
AgentResponse            {"status": "completed", "summary": ...}
  ↓
Slack Gateway            posts `summary`, deletes the processing status
  ↓
Slack thread reply
```

**Agent selection has not moved.** The Slack Gateway selects the fixed
name `"hermes"` (`HERMES_AGENT_NAME` in
`apps/slack-gateway/src/slack_gateway/orchestrator_client.py`) and sends it
as `agent_name`. The Orchestrator dispatches through its registered Agent
boundary and performs **no request classification and no Agent selection**.
Do not describe an observed run as the Orchestrator choosing an Agent.

## 7. Expected trace shape

Span names below are the ones the code emits — `concierge.request`,
`orchestrator.dispatch` from
`apps/slack-gateway/src/slack_gateway/telemetry.py`; `POST /dispatch`,
`hermes.request` from `services/orchestrator/src/orchestrator/telemetry.py`;
`/v1/responses` from Hermes Agent's aiohttp auto-instrumentation. A test
asserts this list against those files so the runbook cannot drift.

```text
concierge.request              Slack Gateway   CONSUMER   (trace root)
  ├─ orchestrator.dispatch     Slack Gateway   CLIENT
  │    └─ POST /dispatch       Orchestrator    SERVER
  │         └─ hermes.request  Orchestrator    CLIENT
  │              └─ /v1/responses   Hermes Agent   SERVER
  └─ slack.response            Slack Gateway   CLIENT
```

All six spans share one trace ID. Context crosses each service boundary by
`inject()` on the client side and `extract()` on the server side, through
the configured propagator — no `traceparent` is built or parsed by hand
anywhere:

| Boundary | Injected by | Extracted by |
|---|---|---|
| Gateway → Orchestrator | `orchestrator_client.dispatch` (`opentelemetry.propagate.inject`) | `orchestrator.telemetry.trace_dispatch_request` (`extract`) |
| Orchestrator → Hermes | `orchestrator.telemetry.trace_context_headers` | Hermes' auto-instrumentation |

**`AgentRequest.trace_id` is not this.** It travels in the JSON body, the
Gateway leaves it `null`, and nothing reads it for propagation. Neither it
nor `traceparent` is authentication or authorization evidence: `POST
/dispatch` has no authentication at all. An observed run must not describe
`trace_id` as having carried the trace.

Retrieve and check the shape:

```bash
python3 infra/observability/live_validation.py trace <TRACE_ID>
```

This prints the span tree, then checks each expected parent → child
relationship, distinguishing *missing span* (a hop did not emit) from
*wrong parent* (the hop emitted but trace context did not continue). Exit
status is non-zero if any relationship fails.

The expectation is a set of relationships, not one linear chain, because
the trace is not linear: `slack.response` is a **second child of
`concierge.request`**, not a descendant of the dispatch. Every one of the
six expected spans appears in at least one relationship, so a non-zero exit
also covers §9's "all six spans" criterion — including the case where the
dispatch chain is perfect but the Slack reply never emitted.

**Evidence-channel limitations, both observed:**

- Phoenix's `/v1/projects/{p}/spans` REST API reports `span_kind` as
  `UNKNOWN` for these spans. **Do not verify span kind through it** — the
  kinds in the table above are what the code sets, not what that API can
  confirm. Use the Phoenix UI if kind matters for a given run.
- MLflow exposes trace-level info only; no working span-attribute endpoint
  was found (`memory: reference_phoenix_mlflow_trace_query_api`). Use it to
  confirm the trace arrived and its `state`, and use Phoenix for
  attributes. Both receive the same processed pipeline from one Collector
  (`docs/observability/collector-redaction.md`), so Phoenix evidence
  applies to what MLflow received.

MLflow trace lookup. This is a `POST`, but it is MLflow's read/search
endpoint — it queries and writes nothing. The v3 request shape is easy to
get wrong: a flat `locations` list is accepted and silently matches
nothing, which reads like "no traces exist".

```bash
curl -s -X POST http://127.0.0.1:5000/api/3.0/mlflow/traces/search \
  -H "Content-Type: application/json" \
  -d '{"locations":[{"type":"MLFLOW_EXPERIMENT","mlflow_experiment":{"experiment_id":"0"}}],"max_results":20}'
```

## 8. Telemetry evidence

### Expected safe attributes

These are the attributes the code intentionally emits. Nothing else should
be treated as expected.

| Span | Attributes |
|---|---|
| `concierge.request` | `concierge.request.source`, `slack.event.type`, `slack.message.threaded` |
| `orchestrator.dispatch` | `concierge.downstream.service`, `concierge.operation` |
| `POST /dispatch` | `concierge.operation`, `http.method`, `http.route`, `http.status_code` |
| `hermes.request` | `concierge.downstream.service`, `concierge.operation` |
| `slack.response` | `concierge.downstream.service`, `concierge.operation` |
| `/v1/responses` | the auto-instrumentation's `http.*` set |

Plus `redaction.ignored.count` on each, added by the Collector's redaction
processor — its presence is what shows redaction ran.

On failure only, one bounded `error.type` from a fixed vocabulary:
`orchestrator.request_error` / `orchestrator.outcome_unknown` (Gateway),
`dispatch.server_error` / `hermes.http_status_error` /
`hermes.connection_error` / `hermes.invalid_response` (Orchestrator).

**Two Slack attributes are intentionally present**: `slack.event.type`
(always the constant `message`) and `slack.message.threaded` (a boolean).
Neither is an identifier or content. An evidence record must not claim "no
Slack attribute reaches telemetry" — the accurate claim is that no Slack
*identifier* and no message *content* does.

`agent_name` is deliberately **not** a span attribute anywhere
(`services/orchestrator/src/orchestrator/telemetry.py` explains why). Do
not look for it, and do not record its absence as a defect.

### Sensitive values that must be absent

Build a needles file — one `label=value` line each, **every label
distinct** — from the values the run actually used. A repeated label, or a
malformed line, makes the file unusable (`INCOMPLETE`, exit `2`) rather
than dropping an entry: a sentinel silently excluded from the scan is the
same false PASS as one that was never supplied. Parse errors name the line
*number* only and never quote its content, since a malformed entry is
exactly where a pasted secret tends to be.

The Gateway's own log line supplies the identifiers:

```bash
docker compose logs --tail=50 slack-gateway | grep "Dispatching Slack message"
```

Check them:

```bash
python3 infra/observability/live_validation.py scan <TRACE_ID> \
  --needles-file <path> \
  --env HERMES_API_SERVER_KEY \
  --env-from-service orchestrator \
  --expect-container <the orchestrator container prefix from §3>
```

**`scan` never re-echoes a sentinel value** — it prints only `absent` /
`LEAKED` per label — so its output can go into a pasted evidence record as
it stands.

That is a property of `scan`, not of the whole procedure: **the
`docker compose logs` command above prints the Slack identifiers to your
terminal**, which is how you obtain them, and `--env-from-service` reads
the credential into the process (without printing it). The narrow claim is
the true one: nothing you paste from `scan` contains a sentinel. Keep the
needles file outside the repository, and delete it when the run is over.

`--env-from-service orchestrator` is how the credential is supplied, and
it matters *which* source is used. **Docker Compose reads `.env` itself,
but a host-side `python3` process does not**, so `--env
HERMES_API_SERVER_KEY` alone finds nothing on most machines.

`--env-from-service` reads the value out of the **running container's own
environment** — what Compose actually injected, after any interpolation it
performed. That is the ground truth for "the credential this stack is
using", and it needs no dotenv interpretation at all.

**It is exclusive.** When `--env-from-service` is given, no other source is
consulted for those names: an unreadable container, or one that does not
carry the variable, fails the run rather than quietly substituting whatever
the host environment happens to hold. A stale host value would scan clean
against telemetry that leaked the *current* credential — a lower-authority
answer is worse than no answer, because only one of them is visibly
incomplete.

**And it is bound to one instance.** "Authoritative" and "the same
instance" are different properties: provenance is recorded *before* the
request and the scan runs *after* it, so a service recreated in between is
still an authoritative source — of the wrong credential.
`--expect-container` closes that window, and it is **not optional**:
`--env-from-service` without it is `INCOMPLETE`, as is `--expect-container`
without `--env-from-service` (which would bind nothing). If `orchestrator`
no longer resolves to the recorded prefix, the scan is `INCOMPLETE` (exit
`2`, Phoenix not queried) and names the mismatch rather than reading the
replacement.

If the Orchestrator *was* legitimately restarted mid-validation, the run is
over: its credential may differ from the one the request used, so nothing
this scan reports would be evidence about that request. Start again from
§3.

The Orchestrator is the right service to read it from because it is the
only **caller-side** holder: since #43 the Slack Gateway no longer has the
credential, and the Orchestrator owns the client side of the Hermes hop.
The `hermes-agent` container holds the *same value* as its own
`API_SERVER_KEY` — the server side it validates against — so either
container would yield the right string to scan for, but the Orchestrator is
the one whose possession of it this path depends on.

A `--env-file` fallback exists, but it is deliberately **fail-closed**: any
value whose Compose semantics this tool cannot reproduce — `${...}`
interpolation, `$NAME`, backslash escapes, inline comments, an `export`
prefix — is *rejected*, not parsed. Parsing `KEY=${BASE}` literally would
scan the string `"${BASE}"` while the container holds the expansion, so a
leak of the real credential would report `absent`. A tool that can pass
while checking the wrong value is worse than one that refuses.

Either way, a requested sentinel that cannot be resolved is a hard failure
(`INCOMPLETE`, exit `2`, Phoenix not even queried) — an unchecked sentinel
must never be able to look like a clean run. Do not `export` the value into
your shell as a workaround; that puts it in shell history and every child
process.

#### The required set

These are the sentinels a PASS depends on. All are obtainable from the
Gateway log and the running Orchestrator, so a run has no excuse for
skipping one:

| Label | Source |
|---|---|
| Slack event id (`task_id`) | Gateway log |
| Slack user id | Gateway log |
| Slack channel id | Gateway log |
| Slack workspace id | Gateway log |
| `conversation_id` string | Gateway log |
| message timestamp | Gateway log |
| `HERMES_API_SERVER_KEY` | `--env-from-service orchestrator` |

#### The optional content extension

Checking whether the **message text** or the **model's response text**
leaked is a stronger check, and it is deliberately *not* part of the
required set. Neither value exists anywhere this procedure can read: the
Gateway logs identifiers and a response *length*, never content — and that
data minimisation is a property worth keeping, not a gap to close by
logging more.

The operator, however, knows both: they sent one and read the other. To
include them, append two lines to the needles file by hand:

```text
message text=<exactly what you sent>
response text=<exactly what Slack replied>
```

`scan` reports only `absent` / `LEAKED`, so neither value is echoed back.
Three practical notes:

- The file is line-based, so a **multi-line** message cannot be expressed.
  §5's fixed input and its expected reply are both single lines, which is
  one more reason to use them.
- Use the text verbatim, punctuation included. A near-miss scans clean and
  proves nothing.
- Delete the file when the run is over. It is the only artifact of a
  validation that contains message content, and it must never be inside
  this repository.

A run that does this records `content sentinels: checked`. A run that does
not records `content sentinels: not performed` — **not** a failure, and not
a caveat on PASS, because PASS is defined against the required set above.

## 9. Success criteria

```text
PASS =
    §5's fixed input was sent, and its expected reply is observed in-thread
  + the Gateway log shows POST /dispatch → 200 and status=completed
  + one trace contains all six expected spans with one trace ID
  + every expected parent → child relationship is correct
    (`trace` exits 0 — it checks both of the above together)
  + runtime provenance is recorded and internally consistent (§3)
  + every sentinel in §8's REQUIRED SET reports `absent`, and none was
    skipped (`scan` exits 0; an unresolved one exits 2 as `INCOMPLETE`)
  + no unexpected side effect is observed (§14)
```

**PASS is defined against the required set, and means exactly that.** §8's
optional content extension — message and response text — is not part of it,
because neither value exists anywhere this procedure can read, and the
alternative (logging message content) is a worse trade than the check is
worth. A run that performed the extension records so and is stronger
evidence; a run that did not is still a PASS, and its record says
`content sentinels: not performed` so no reader can mistake the scope.

**The input is part of the criterion, not a suggestion.** §5 specifies a
deterministic input *and* the reply it should produce, and the first line
above depends on both: with an operator-chosen message there is no expected
reply to compare against, so the check degrades to "some substantive reply
came back" — weaker, and not what PASS claims. A run that deviates records
`test input conforms to §5: NO` and says which criterion was therefore not
exercised.

The invariant being protected: **a PASS must never imply a check the run
did not actually perform.** Adding a sentinel to §8's required set means
committing to it being obtainable every time; likewise, calling a run a
PASS means every criterion above was exercised as written.

### Run result vocabulary

Because PASS is conjunctive, a run that skipped a criterion is **not** a
PASS with a footnote — it is a different result. Record one of:

| Result | Meaning |
|---|---|
| `PASS` | every criterion above was exercised, and met |
| `NOT A RUNBOOK PASS` | one or more criteria were not exercised. Name which, and record `Verified subset:` — what *was* established |
| `INCONCLUSIVE` | a criterion was exercised, but its evidence is unusable or ambiguous — the trace never arrived, the backends disagree, a sentinel could not be resolved. Distinct from `NOT A RUNBOOK PASS` (not attempted) and from `FAIL` (attempted, answered, wrong) |
| `FAIL` | a criterion was exercised and not met |

**Aggregating a mixed run.** A run can have several non-passing criteria at
once — one not exercised, another inconclusive. Two rules, both required:

1. **Record every criterion's own state.** The overall label never stands
   alone; a record that names only the worst outcome hides the rest.
2. **The overall result is the worst state present**, in the order
   `FAIL` > `NOT A RUNBOOK PASS` > `INCONCLUSIVE` > `PASS`.

`NOT A RUNBOOK PASS` outranks `INCONCLUSIVE` because the two call for
different work: a criterion that was never exercised is fixed by running
it, while an inconclusive one needs the evidence channel investigated
first. Putting the cheaper action in the headline is the more useful
default, and rule 1 means nothing is lost either way.

Deliberately **not** `INCOMPLETE` at run level: this document already uses
that word for the specific thing `scan` and `trace` print when the evidence
they were given is unusable (exit `2`). A run can be `NOT A RUNBOOK PASS`
while every tool invocation in it exited `0`, which is exactly the 21:05
case below — reusing `INCOMPLETE` for both would blur a distinction the
tooling works hard to keep. `INCONCLUSIVE` is the run-level term for the
case where a tool *did* report exit `2`, or where two backends disagree.

Do not require what this stack cannot provide: span kind via the Phoenix
REST API, span attributes via MLflow, or a registry digest for a locally
built image.

## 10. Failure / unknown outcome semantics

The implementation classifies dispatch outcomes; this runbook only reports
what it classified. **Do not redefine the classification here** — the
authority is
`apps/slack-gateway/src/slack_gateway/orchestrator_client.py`.

| Slack message | Gateway log `outcome=` | Meaning |
|---|---|---|
| `Please try again` | `orchestrator.request_error` | `DEFINITE_FAILURE` — provably nothing ran |
| `The result is unknown…` | `orchestrator.outcome_unknown` | `OUTCOME_UNKNOWN` — may have run |

Only two things are definite failures: httpx errors that provably precede
delivery, and HTTP `400`/`404`, which the Orchestrator emits strictly
before `Orchestrator.dispatch()` is called.

**Everything else is `OUTCOME_UNKNOWN`**, including cases that look like
plain failures:

- a read/write **timeout** — the request may have been delivered and may
  still be executing. The Gateway's 330s and the Orchestrator's 300s
  timeouts are per-operation *inactivity* timeouts, not end-to-end
  deadlines; their ordering is best-effort and guarantees nothing;
- **HTTP 500** — the Orchestrator returns one generic `internal_error`
  both when Hermes was never reached and when the Agent raised *after*
  running, because `HermesAgent.handle()` extracts output text only once
  the Hermes call has returned;
- a transport failure after the request was written;
- a `2xx` whose body is not a valid `AgentResponse` — there the Agent
  demonstrably ran and only the result was lost;
- any unexpected HTTP status.

A visible error therefore never proves non-execution. Classify as
`DEFINITE_FAILURE` only when non-execution is provable; otherwise
`OUTCOME_UNKNOWN`.

## 11. Retry rule

```text
OUTCOME_UNKNOWN  →  do NOT immediately retry
```

Hermes is tool-capable — this repository has verified a real Terminal Tool
file-writing side effect (`docs/roadmap.md` Milestone 2), and live Calendar
MCP `tools/call` requests on this path — so a request whose outcome is
unknown may already have acted. There is **no idempotency guarantee** at
this boundary; nothing deduplicates a repeated dispatch.

Before deciding whether a retry is safe, inspect, in order:

1. `docker compose logs --tail=100 orchestrator` — did `POST /dispatch`
   reach it, and what did it log?
2. `docker compose logs --tail=100 hermes-agent` — did Hermes start a turn?
3. the trace, if one was exported — how far along the path did spans get?
4. §14's side-effect checks.

Record the decision and its basis in the run record.

## 12. Stop conditions

Stop and do not proceed (or do not continue) if any of these hold:

- the working tree is dirty, or §3's source comparison reports `DIFFERS`
  for anything you cannot show to be comment-only — the run would be
  evidence about a build you cannot name. (The retired form of this
  condition was "a container predates the build at the recorded SHA";
  §3 explains why image and start timestamps are not evidence.);
- the Slack Gateway container still has `HERMES_API_*` in its environment —
  it is on the pre-#43 direct path, so a run would validate the wrong
  thing;
- `orchestrator` is not healthy, or the Gateway cannot reach
  `GET /health`;
- any required service is unhealthy (§4);
- a secret value appears in a terminal, a log, or an evidence record;
- an unexpected tool call, filesystem change, or external side effect is
  observed (§14);
- the trace cannot be interpreted — spans missing with no explanation, or
  more than one trace ID for one request;
- any service was recreated between recording provenance and collecting
  evidence — the run's provenance no longer describes what handled the
  request;
- the Slack workspace, channel, or thread is not one you control.

## 13. Evidence capture template

Copy this into the [Run records](#run-records) section and complete it.

```text
Validation date:
Operator:

Repository SHA:
Working tree:            clean / DIRTY
Containers (container ID prefix, image ID, started) -- all eight:
  slack-gateway:
  orchestrator:            <- record the prefix; §8 requires it
  hermes-agent:
  google-calendar-mcp:
  ollama:
  otel-collector:
  phoenix:
  mlflow:
Runtime Python inputs match recorded SHA (§3):       YES / NO (detail)
Outside that boundary (§3's table):                 acknowledged

Preconditions:
  all required services healthy:                    YES / NO
  gateway → orchestrator GET /health:               200 / other
  gateway env free of HERMES_API_*:                 YES / NO

Test input:
Test input conforms to §5:                          YES / NO
Slack reply observed (and matches §5's expected):
Gateway log (dispatch → status):

Trace ID:
Observed spans:
Expected parent-child relationships:                PASS / FAIL / UNKNOWN
Gateway → Orchestrator confirmed:                   YES / NO / UNKNOWN
Orchestrator → Hermes confirmed:                    YES / NO / UNKNOWN
Present in MLflow (trace id, state):
Sensitive sentinel check -- required set (§8):      PASS / FAIL / UNKNOWN
Content sentinels (message / response text):        checked / not performed
Credential read bound to the request's container:   YES / NO

Unexpected side effects observed (§14 -- window, not attribution):
                                                    YES / NO / UNKNOWN
Side-effect window anchored at §5's marker:         YES / NO
Post-validation checks performed:

Per-criterion state (§9) -- one line each, none omitted:
  §5 fixed input           PASS / NOT EXERCISED / INCONCLUSIVE / FAIL
  gateway dispatch/status  PASS / NOT EXERCISED / INCONCLUSIVE / FAIL
  trace / relationships    PASS / NOT EXERCISED / INCONCLUSIVE / FAIL
  required-set sentinels   PASS / NOT EXERCISED / INCONCLUSIVE / FAIL
  provenance (§3)          PASS / NOT EXERCISED / INCONCLUSIVE / FAIL
  side effects (§14)       PASS / NOT EXERCISED / INCONCLUSIVE / FAIL
Overall result:          PASS / NOT A RUNBOOK PASS / INCONCLUSIVE / FAIL
                         (§9's aggregation rule: the worst state present)
Verified subset (if not a PASS):
Notes / limitations of this run:
```

## 14. Post-validation state

The input in §5 asks for a text reply, but nothing *enforces* a tool-free
turn (§5). After the run, check what actually happened rather than assuming:

```bash
# 1. Did any tool call span appear? A tool call starts its own trace --
#    Hermes does not propagate context to its outbound MCP calls (a known
#    upstream gap, docs/observability/hermes-trace-context.md), so it will
#    NOT be inside the request's trace.
curl -s "http://127.0.0.1:6006/v1/projects/local-agent-concierge-infra-smoke-test/spans?limit=50"

# 2. Did anything change under Hermes' persistent state since §5's
#    marker? `-newer <file>` is used rather than `-newermt <time>` for two
#    reasons: it anchors the window at validation start instead of at
#    inspection time, and it avoids the relative-timestamp form
#    (`-newermt '-10 minutes'`) that GNU findutils accepts but `bfs` --
#    which some systems install as `find` -- rejects as invalid, printing
#    to stderr and matching nothing. A side-effect check that reports "no
#    changes" because it failed to run, or because its window drifted past
#    the write, is the worst possible outcome for this step.
find data/hermes -type f -newer /tmp/live-validation-start \
  -not -path '*/cache/*' | sort

# 3. Is the repository still clean?
git status --porcelain

# 4. Done -- remove the marker so a later run cannot inherit this one's
#    window.
rm -f /tmp/live-validation-start
```

Expected for a text-only turn: no `tools/call …` span in the inspected
window other than the routine `MCP send ping` keepalive (Hermes pings the
Calendar MCP every ~3 minutes, unrelated to any request), and no repository
change.

**This is a time-window observation, not request attribution.** Hermes does
not propagate the request's trace context into its outbound MCP calls
(`docs/observability/hermes-trace-context.md`), so a tool call it made
*would* appear as an unrelated trace with no link back. Nothing here can
tie an outbound tool trace to this request, or rule one out. Record it as
"no non-keepalive `tools/call` span observed in the inspected window" —
never as "no tool call for this request".

**Reading step 2's output.** A successful turn touches roughly a dozen
files, and almost none of them are about your request:

| Path | What it means |
|---|---|
| `response_store.db-wal`, `response_store.db-shm` | **The conversation store** — the Gateway sends `"store": true`, so the turn is persisted. Expected. |
| `state.db-*`, `kanban.db-*`, `channel_directory.json` | Hermes' own state, written continuously |
| `cron/ticker_*`, `state/gateway.heartbeat` | Background tickers and heartbeats, unrelated to any request |
| `logs/agent.log`, `logs/errors.log` | Hermes' logs |
| `models_dev_cache.*` | Model metadata cache refresh |

What would *not* be expected: a new file outside these, anything under a
project directory, or a change to this repository.

**Clock offset.** The containers log in UTC; your host may not. Compare the
gateway log's timestamps against container time, not host time:

```bash
date -u '+%Y-%m-%d %H:%M:%S UTC'                 # host, in UTC
docker compose exec -T hermes-agent date -u      # container
```

An hour's offset between a log line and a file mtime is a timezone
difference, not evidence of anything.

Do not record "no side effects possible". The supportable statement is
"no unexpected side effect observed, by the checks above".

---

## Run records

Each entry is **observed evidence** from one actual execution.

### 2026-09-10 (21:05 UTC) — live run with a §5 test-input deviation

Executed with the procedure above rather than ad hoc, so the container
binding is pinned rather than inferred — but **not** with §5's fixed input,
which means one PASS criterion was not exercised as written. See the result
line and limitations below. This is not yet the canonical runbook PASS.

```text
Validation date:         2026-09-10 21:05:14 UTC (container clock)
Operator:                repository owner, interactive session

Repository SHA:          548fed6a32ccd9cad2261e9d921e2819799260c1
                         (= merge commit of PR #44)
Working tree:            clean
Containers (container ID prefix, image ID, started):
  slack-gateway:         dbab5033ac33  sha256:9c9be6031de05d6473d  20:50:41
  orchestrator:          df6eb0bff364  sha256:f021e28af4b6c2eda0a  20:50:41
  hermes-agent:          9d53cbf88567  sha256:470aa3b68074d9d752f  20:50:41
  ollama:                7e7efecf4bef  sha256:dacbdaa86a43fb9ed58  20:50:41
  otel-collector:        2f55fb34043e  sha256:e11c83206a71a0ac312  20:53:50
Runtime Python inputs match recorded SHA (§3):
                         NO, in one respect. Established afterwards by §3's
                         source comparison, not at the time: every
                         slack-gateway file and every orchestrator file
                         except one matches 548fed6 byte-for-byte.
                         `orchestrator/hermes_agent.py` differs in two
                         docstrings (the module docstring and
                         `_extract_output_text`'s) -- the running image
                         predates 094b984 and 6aa80ce, which changed only
                         those comments. ASTs differ only in those string
                         constants; no executable code differs.
                         The original record said "Provenance consistent:
                         YES" on the basis that the containers started
                         after the images were built. That was not
                         evidence: a restart is not a rebuild, and image
                         `Created` timestamps are unreliable (§3).
                         phoenix / mlflow image ids were not recorded at
                         the time; `provenance` did not yet report them.

Preconditions:
  all required services healthy:          YES (after recreating otel-collector)
  gateway → orchestrator GET /health:     200
  gateway env free of HERMES_API_*:       YES

Test input:              one Slack direct message, operator-chosen wording
Test input conforms to §5:  NO -- §5's fixed string was not used
Slack reply observed:    a substantive 63-character reply was posted
                         in-thread and the processing status was deleted.
                         Because the input was not §5's, there was no
                         expected reply to compare it against: this
                         establishes that the path returned a real agent
                         response rather than the error message, not that
                         a deterministic expected output was produced.
Gateway log:             POST http://orchestrator:8700/dispatch → 200 OK
                         agent=hermes  status=completed  response_chars=63
                         delivery=posted_and_processing_status_deleted

Trace ID:                bb3d8ce5fcb43e4e8cb8ad895e077950
Observed spans:          6, one trace id:
                           concierge.request        (root)
                             orchestrator.dispatch
                               POST /dispatch
                                 hermes.request
                                   /v1/responses
                             slack.response
Expected relationships:  PASS -- all five, `trace` exit 0
Gateway → Orchestrator:  YES
Orchestrator → Hermes:   YES (`http.status_code = 200` on POST /dispatch)
Present in MLflow:       tr-bb3d8ce5fcb43e4e8cb8ad895e077950
                         service=slack-gateway  state=OK
Sensitive sentinel check -- required set (§8):
                         PASS -- all 7 required labels, 0 leaked, exit 0
Content sentinels (message / response text):
                         not performed -- see §8's optional extension
Credential read bound to the request's container:  YES (df6eb0bff364)

Unexpected side effects: none observed, but see the INCONCLUSIVE state
                         above -- the window this was observed in may not
                         have covered the request. No non-keepalive
                         `tools/call` span in the inspected window (which
                         cannot attribute tool calls to a request either
                         way, see §14); the
                         only persistent writes were the expected
                         conversation store (`response_store.db-*`) plus
                         Hermes' own background state, logs and heartbeats;
                         repository clean.

Per-criterion state (§9):
  §5 fixed input           NOT EXERCISED -- operator-chosen wording
  gateway dispatch/status  PASS -- POST /dispatch 200, status=completed
  trace / relationships    PASS
  required-set sentinels   PASS
  provenance (§3)          NOT EXERCISED at the time; checked retroactively
                           and answered NO-in-one-respect (above)
  side effects (§14)       INCONCLUSIVE -- performed with the superseded
                           "last 10 minutes" window, whose start drifts
                           forward while the Human works through §7 and §8.
                           Nothing establishes that this run's window still
                           covered the request, so the clean result cannot
                           be relied on.
Overall result:          NOT A RUNBOOK PASS (worst state present; see §9's
                         aggregation rule)
Verified subset:         routing through the Orchestrator, trace continuity
                         across all five relationships, required-set
                         sentinel absence, and credential binding to the
                         serving container. Provenance and side-effect
                         coverage are NOT part of this subset.
```

**What this run additionally established.** (Current-run evidence; §4 and
§7 cite the same observations as *referenced* history.) The helper's
container-backed paths ran against a real Docker daemon for the first time — they had been
exercised only against a stubbed `_run` when PR #44 was written, which that
PR flagged as an open gap. All four failure paths were confirmed to fail
closed with distinguishable reasons: a mismatched `--expect-container`
(`"replaced between the request and this scan"`), a too-short prefix, a
service without the variable, and a service that is not running. The
2026-09-10 17:27 trace also survived the engine restart and still passes,
so Phoenix's storage is durable across one.

**Limitations of this run.**

- The optional content extension was not performed, so this run says
  nothing about whether the message or response text leaked. That is a
  scope statement, not a caveat on the PASS: §9 defines PASS against §8's
  required set, all seven of which were checked.
- **§5's fixed input was not used**, so §9's first criterion was not
  exercised. A canonical runbook PASS still needs a run that sends
  `Reply with exactly SLACK_GATEWAY_OK.` and observes that exact reply.
  The same deviation is why the content extension was impractical here:
  §5's single-line, deterministic input/reply pair is what makes those two
  sentinels expressible verbatim, and an operator-chosen message is not.
- Only the success path ran; the failure and unknown-outcome paths remain
  test-covered only.
- No Calendar tool was invoked, so Hermes' outbound-MCP propagation gap was
  neither confirmed nor contradicted.
- `otel-collector` had to be recreated first (§4). Had that gone unnoticed,
  the request would have succeeded in Slack while producing no trace at
  all.
- The side-effect check used the superseded drifting window, so its clean
  result is `INCONCLUSIVE` rather than a pass. Re-establishing it for this
  run is not possible after the fact; §5's marker exists so later runs do
  not inherit the problem.

### 2026-09-10 (17:27 UTC) — first live run of the Slack → Orchestrator path

```text
Validation date:         2026-09-10 17:27 (host local time)
Operator:                repository owner, interactive session

Repository SHA:          0bcceb95820ed91a765ccee4e3beedff20237e0f
                         (= merge commit of PR #43)
Working tree:            clean
Containers (image ID, started):
  slack-gateway:         sha256:9c9be6031de05d6473d…  2026-09-10T17:21:28
  orchestrator:          sha256:f021e28af4b6c2eda0a…  2026-09-10T11:24:42
  hermes-agent:          sha256:470aa3b68074d9d752f…  2026-09-10T11:25:30
  ollama:                sha256:dacbdaa86a43fb9ed58…  2026-09-10T11:14:52
  otel-collector:        sha256:e11c83206a71a0ac312…  2026-09-10T11:14:52
Source matches recorded SHA:
                         NOT CHECKED at the time. Inferred afterwards: the
                         image ids above are the ones §3's source comparison
                         was later run against, and against 0bcceb95 the
                         only difference is `orchestrator/hermes_agent.py`'s
                         module docstring (094b984, part of this very PR's
                         merge, which the running image predates). No
                         executable code differs. This is retroactive
                         inference from unchanged image ids, not evidence
                         gathered by the run.
                         The original record said "Provenance consistent:
                         YES — slack-gateway was rebuilt and recreated at
                         this SHA before the run". A rebuild before a run
                         is not a comparison; see §3.
                         phoenix / mlflow image ids were not recorded.

Preconditions:
  all required services healthy:          YES
  gateway → orchestrator GET /health:     200 {"status": "ok"}
  gateway env free of HERMES_API_*:       YES (ORCHESTRATOR_BASE_URL,
                                          OTEL_EXPORTER_OTLP_ENDPOINT,
                                          SLACK_APP_TOKEN, SLACK_BOT_TOKEN)

Test input:              one Slack direct message (text request; the exact
                         wording was operator-chosen, not §5's string)
Test input conforms to §5:  NO
Slack reply observed:    YES — 452-character text reply posted in-thread,
                         processing status deleted
Gateway log:             POST http://orchestrator:8700/dispatch → 200 OK
                         agent=hermes  status=completed  response_chars=452
                         delivery=posted_and_processing_status_deleted

Trace ID:                ff731430ed03161076ae1857d8dea219
Observed spans:          6, all sharing that one trace ID:
                           concierge.request        13.65s  (root)
                             orchestrator.dispatch  12.93s
                               POST /dispatch       12.92s
                                 hermes.request     12.92s
                                   /v1/responses    12.90s
                             slack.response          0.49s
Expected parent-child relationships:      PASS — all five expected
                                          relationships correct, including
                                          concierge.request → slack.response
Gateway → Orchestrator confirmed:         YES — `orchestrator.dispatch`
                                          exists (0 occurrences before this
                                          run) and `POST /dispatch` is its
                                          child
Orchestrator → Hermes confirmed:          YES — `hermes.request` →
                                          `/v1/responses`, `http.status_code
                                          = 200` on `POST /dispatch`
Present in MLflow:                        tr-ff731430ed03161076ae1857d8dea219
                                          service=slack-gateway  state=OK
Sensitive sentinel check — required set (§8):
                                          PASS — all 7 required labels,
                                          0 leaked
Content sentinels (message / response text):
                                          not performed

Unexpected side effects:                  none observed, but see the
                                          INCONCLUSIVE state above — no
                                          non-keepalive `tools/call` span in
                                          the inspected window (not request
                                          attribution; see §14)
Post-validation checks performed:         span scan of the surrounding
                                          window; repository clean

Overall result:                           NOT A RUNBOOK PASS -- §5's input
                                          was not used, so §9's first
                                          criterion was not exercised
Per-criterion state (§9):
  §5 fixed input                          NOT EXERCISED
  gateway dispatch/status                 PASS
  trace / relationships                   PASS
  required-set sentinels                  PASS
  provenance (§3)                         NOT EXERCISED -- credential read
                                          unbound, no source comparison
  side effects (§14)                      INCONCLUSIVE -- superseded window
                                          method, as for the 21:05 run
Verified subset:                          routing, trace continuity and
                                          required-set sentinel absence.
                                          Nothing about which build served
                                          it, and nothing reliable about
                                          side effects.
```

**What this run additionally settled.** MLflow reported the trace as
`state=OK`, not `IN_PROGRESS`.
`docs/observability/orchestrator-trace-context.md` records `IN_PROGRESS`
from the earlier synthetic-caller run and names a missing root span as one
known sufficient cause; with the Slack Gateway supplying a real root span,
that state did not occur. One observation, not a proof of the general case.

**Limitations of this run.**

- The operator's message wording was not §5's fixed string, so a later run
  following this runbook will be more reproducible than this one.
- The credential scan was **not bound** to a recorded container prefix —
  §3's `--expect-container` step did not exist yet. The binding did hold in
  fact: `orchestrator` started at 11:24:42 and was never recreated, while
  the only recreation that session (`slack-gateway`, 17:21:28) preceded the
  17:27 request. But that is inferred from start times rather than pinned,
  so it is weaker evidence than a later run following §3 will produce.
- The optional content extension (§8) was not performed; neither value was
  captured at the time and neither is recoverable now. This is a scope
  statement rather than a caveat on the PASS, which §9 defines against §8's
  required set.
- Span *kind* was not verified: Phoenix's REST API reports `UNKNOWN` (§7).
- Only the success path ran. The failure and unknown-outcome paths were not
  exercised and remain test-covered only.
- No Calendar tool was invoked, so Hermes' known outbound-MCP propagation
  gap did not appear in this trace and was neither confirmed nor
  contradicted by it.
