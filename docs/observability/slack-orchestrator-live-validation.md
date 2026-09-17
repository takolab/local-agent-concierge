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
Diff that file and record what the difference actually is — the
distinction changes what the run's evidence is worth, and only the diff
can tell you.

Record it in the categories §12 uses, which are narrower than they look:
`#` comments are discarded by the tokenizer, while **docstrings are string
constants that survive into the compiled module** and are therefore *not*
comments. §12's carve-out covers the first and not the second, so
"docstring-only" is a stop condition rather than a difference to explain
past.

**Classify it with this procedure, in order.** It matters that these are
separate steps: an AST comparison alone cannot distinguish the two
categories §12 now treats differently, because both a `#` comment
difference and a docstring difference leave the docstring-stripped ASTs
identical. A single "ASTs match after stripping" check collapses a
continue and a stop into one answer.

```text
1. Token signature.  Tokenize both copies; drop COMMENT and NL tokens;
   compare the remaining (type, string) sequences.
     identical  ->  the difference is comments and layout only
                    -> §12's carve-out applies, the run may continue
     differs    ->  go to 2

2. Unmodified ASTs.  Parse both copies and compare `ast.dump`.
     identical  ->  a non-comment, non-executable source difference
                    (numeric literal spelling, redundant parentheses,
                    and the like).  NOT covered by §12's carve-out: STOP
     differs    ->  go to 3

3. Docstring-stripped ASTs.  Strip every module/class/function docstring
   from both parses, compare again.
     identical  ->  docstring-only.  STOP (§12)
     differs    ->  executable code differs.  STOP (§12)
```

Step 1 is what licenses the `comment-only` claim, and nothing weaker
does: identical ASTs are *not* sufficient, since `x = 0x1` and `x = 1`
produce the same tree while differing in source and in neither comments
nor whitespace. Steps 2 and 3 do not change the outcome — everything past
step 1 stops — but they name *which* difference was found, which is what
the record has to carry and what any later decision to broaden §12 would
be reasoned about.

Record the step that decided it, not just the verdict.

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
Reply with this token only and nothing else: SLACK_GATEWAY_OK
```

**The expected reply, after stripping leading and trailing whitespace:**

```text
SLACK_GATEWAY_OK
```

Exact, and case-sensitive. Nothing else is the expected reply — not a
different case, not a trailing period, not the token inside a sentence,
not the token in backticks, not the token plus an explanation. A run that
observes any of those records `test input conforms to §5: YES` — the
input *was* §5's — and §9's first criterion as `FAIL`.

Stripping surrounding whitespace is the only normalisation, because it is
the one the transport performs rather than a judgement about what the
model meant.

**Why the input reads the way it does.** It carries the whole burden of
making the reply unambiguous, so the acceptance rule does not have to.
Two properties are deliberate and should survive any rewording:

- **It ends with the token.** Its predecessor, `Reply with exactly
  SLACK_GATEWAY_OK.`, ended with a period that was simultaneously the
  sentence's terminator and — on one defensible reading of "exactly" —
  part of the token. The 2026-09-14 run discovered that while it was
  running, and no amount of care in the acceptance rule can repair an
  input that asks for two different things.
- **It says "and nothing else".** The failure this criterion most needs
  to catch is not a formatting variant; it is a reply that carries the
  token *plus* commentary, which would satisfy a loose reading while
  proving much less than PASS claims.

**It is a single line, and must stay one.** §8's optional content
extension appends the message and the reply to a line-based needles file,
so a multi-line input cannot be expressed there at all.

**Two earlier attempts at this section are recorded because both were
wrong in instructive ways.** The first enumerated the exact strings the
2026-09-14 run had made plausible (`SLACK_GATEWAY_OK` and
`SLACK_GATEWAY_OK.`) and called the set closed — overfitted to one
observation, since nothing made a trailing period more principled than
backticks or a case change. The second replaced it with a semantic
"conveys the token and carries nothing else substantive" judgement, which
removed the overfitting but grew a judgement surface in the part of the
runbook that is meant to be deterministic, and contradicted itself on
whether a case change was acceptable formatting or an altered token.
Neither problem exists once the *input* is unambiguous: the reply is then
a fixed string, and the criterion can be an exact comparison again.

**A strict rule means a run can fail on a formatting variant, and that is
the intended trade.** If the model answers `SLACK_GATEWAY_OK.` the
criterion is `FAIL`, not a near-miss to be waved through. A criterion that
stretches to fit whatever came back costs the meaning of every PASS
recorded against it, and re-running costs one message.

**But "re-run it" is not automatic, and two different questions are being
answered.** `status=completed` says the request produced a known result;
it says nothing about whether the turn was side-effect-free. Everything
this section already concedes still applies — Hermes decides tool use
itself, the path reaches tool-capable components, terminal-toolset
availability on `/v1/responses` is not determinable from this repository —
and there is no idempotency guarantee at this boundary (§11). A completed
turn that returned the wrong formatting may have acted before returning
it, and re-sending could repeat that action.

```text
formatting mismatch  ->  §5 FAIL

NOT `OUTCOME_UNKNOWN`   we know what result the request produced, so
                        §10's classification is unaffected

NOT automatically safe to repeat
                        complete §14's side-effect checks for the failed
                        run, then decide explicitly whether repeating is
                        safe -- given §14's bounded visibility and the
                        absence of an idempotency guarantee -- and record
                        that decision and its basis, as §11 requires.

                        If a consequential action cannot be ruled out
                        well enough to justify a repeat, do not re-run.
                        Reconcile or reset the environment instead.
```

**§14 is evidence toward that decision, and not the decision.** The only
claim it supports is "no unexpected side effect *observed*, by the checks
above". It is a time-window observation rather than request attribution:
Hermes does not propagate trace context into its outbound MCP calls, so a
tool call it made would appear as an unrelated trace with nothing linking
it back, and §14 can neither tie one to this request nor rule one out.

```text
§14 clean   !=   no consequential action occurred
            !=   safe to repeat
```

Making a clean §14 the gate would have it carry a conclusion it
explicitly disclaims — the same shape of error as reading §12's carve-out
generously to admit the answer already in hand.

The distinction is worth keeping sharp, because the two properties come
apart here and the convenient reading merges them. §11's retry
*prohibition* is not triggered — but the *reasoning* behind §11 is about
side effects and idempotency, and knowing the outcome resolves none of
it. So §5 follows §11's decision model rather than inventing a second
one: inspect, decide, and record the decision with its basis.

**`response_chars` corroborates without logging content.** The Gateway
logs a response *length*, never content (§8's data minimisation), and the
expected reply is 16 characters. `response_chars=16` is consistent with
the expected reply and nothing was added; anything else means the reply
was not the bare token, and the thread is what settles it. A length is
not the text, so this corroborates the operator's in-thread observation
rather than replacing it.

`docs/setup/slack.md` uses the same input, so both procedures continue to
produce comparable evidence. It carries no personal data and asks for a
pure text response.

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

**A known outcome is not an exemption from this.** The rule above names
`OUTCOME_UNKNOWN` because that is the case where even *delivery* is in
doubt, but nothing here turns on the outcome being unknown: the tool
capability and the missing idempotency guarantee are properties of the
boundary, not of the classification. A run that completed and failed §5
on formatting is the concrete case — the result is known, and whether the
turn acted before returning it is not. The same decision is required, and
`status=completed` answers none of it.

## 12. Stop conditions

Stop and do not proceed (or do not continue) if any of these hold:

- the working tree is dirty, or §3's source comparison reports `DIFFERS`
  for anything you cannot show to be comment-only — the run would be
  evidence about a build you cannot name. (The retired form of this
  condition was "a container predates the build at the recorded SHA";
  §3 explains why image and start timestamps are not evidence.)

  **"Comment-only" means `#` comments, and nothing else.** Those are
  discarded by the tokenizer and cannot reach the running program.
  **Docstrings are not comments**: they are string constants bound to
  `__doc__`, they survive into the compiled module, and code can read
  them. A difference confined to docstrings is therefore **not** covered
  by this carve-out and **is** a stop condition, however convincingly it
  is shown to leave behaviour unchanged.

  This is deliberately the strict reading. Broadening the carve-out to
  "any mechanically proven non-behavioural difference" is a defensible
  rule, but it is a different rule, and adopting it silently — by reading
  "comment-only" generously in the middle of a run — is how a gate stops
  meaning what it says. The 2026-09-14 run did exactly that and its
  result was reclassified; see its record. If the broader rule is wanted,
  it should be written here deliberately, before the run that relies on
  it.

  **Recovering from a docstring-only difference** means making the
  runtime inputs match: rebuild the affected service from the recorded
  SHA and recreate it. That replaces the container, so the run is no
  longer bound to the provenance already recorded — restart from §3;
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

### 2026-09-15 (11:34 UTC) — **the canonical PASS**

**Overall: PASS.** Every §9 criterion was exercised as written and met,
against criteria that were finalised and merged *before* this run started.
This is the run that closes the live validation gate opened by PR #43.

```text
Validation date:         2026-09-15 11:34:38 UTC (container clock; host UTC
                         offset 0s, verified at preflight)
Operator:                repository owner, interactive session

Repository SHA:          9a24abf7bb933c2ee645001cf422d6fd24cf3a96
                         (= merge commit of PR #46; == origin/master)
Working tree:            clean (before and after)
Containers (container ID prefix, image ID, started) -- all eight:
  slack-gateway:         dbab5033ac33  sha256:9c9be6031de05d6473d  2026-09-15T08:10:58
  orchestrator:          0690b9d00b39  sha256:17272b02c47d075db1d  2026-09-15T08:13:55
  hermes-agent:          9a669773e3ce  sha256:470aa3b68074d9d752f  2026-09-15T11:16:33
  google-calendar-mcp:   b4c605fd9fb4  sha256:72854aab401dc96750a  2026-09-15T11:22:41
  ollama:                7e7efecf4bef  sha256:dacbdaa86a43fb9ed58  2026-09-15T08:10:58
  otel-collector:        cea0f4340a25  sha256:e11c83206a71a0ac312  2026-09-15T08:13:37
  phoenix:               5db3fd4d83bd  sha256:e90c06c2bf2f22ef7d9  2026-09-15T08:10:58
  mlflow:                bac1f771b2de  sha256:ec446a27c197e760a63  2026-09-15T08:10:58
                         `provenance` was run before the request and again
                         after evidence collection: all eight prefixes
                         identical, so nothing was recreated in between.

Runtime Python inputs match recorded SHA (§3):       YES
                         orchestrator: all inputs match (14 comparisons)
                         slack-gateway: all inputs match (12 comparisons)
                         Zero DIFFERS -- §3's ordered classification was not
                         reached, and §12's carve-out was neither needed nor
                         relied on. The orchestrator was rebuilt from 9a24abf
                         and recreated at preflight, which cleared the
                         2026-09-14 docstring-only stop condition by rebuild
                         rather than by reinterpretation
                         (hermes_agent.py now 53bb5f9a4c53... in both).
Outside that boundary (§3's table):                 acknowledged
Bind-mount integrity:    hermes-agent and google-calendar-mcp mounts were
                         repaired earlier this session and verified by
                         positive control; otel-collector's config mount
                         verified behaviourally (health_check answers 200,
                         spans carry redaction.ignored.count). This is NOT a
                         runbook step: §4 documents only the otel-collector
                         recovery. The check was performed as extra preflight
                         because of the 11:09 FAIL; adding it to §4 is a
                         separate, not-yet-made procedural change.

Preconditions:
  all required services healthy:                    YES -- all eight
  gateway -> orchestrator GET /health:              200
  gateway env free of HERMES_API_*:                 YES -- exactly
                         ORCHESTRATOR_BASE_URL, OTEL_EXPORTER_OTLP_ENDPOINT,
                         SLACK_APP_TOKEN, SLACK_BOT_TOKEN (names only;
                         values never printed)
  thread under operator control:                    YES -- direct message;
                         the Gateway log's channel id is D-prefixed

Test input:              `Reply with this token only and nothing else:
                         SLACK_GATEWAY_OK` -- §5's canonical input, sent
                         once as a DM
Test input conforms to §5:                          YES
Slack reply observed (and matches §5's expected):
                         `SLACK_GATEWAY_OK` -- the bare token, reported
                         verbatim from the thread by the operator: no
                         punctuation, no formatting, no added content, and
                         the exact case §5 requires.
                         response_chars=16 corroborates: exactly the
                         expected reply's length, so nothing was added.
                         NOTE: a length cannot settle case. `slack_gateway_ok`
                         and `Slack_Gateway_OK` are also 16 characters, and
                         §5's comparison is case-sensitive, so the operator's
                         in-thread observation is what decides this field.
Gateway log (dispatch -> status):
                         POST http://orchestrator:8700/dispatch → 200 OK
                         agent=hermes  status=completed  response_chars=16
                         event_id=Ev0C1SA7E12P
                         delivery=posted_and_processing_status_deleted

Trace ID:                45136c14ec24e75613eda6dfb5419c3b
Observed spans:          6, one trace id:
                           concierge.request        d8aad386582e  (root)
                             orchestrator.dispatch  acba95ba0e17
                               POST /dispatch       1e4cf5f2cf42
                                 hermes.request     9bb6dad9886d
                                   /v1/responses    3c95dd4b9399
                             slack.response         a95e07919374
Expected parent-child relationships:                PASS -- all five,
                                                    `trace` exit 0
Gateway → Orchestrator confirmed:                   YES
Orchestrator → Hermes confirmed:                    YES
Present in MLflow (trace id, state):
                         tr-45136c14ec24e75613eda6dfb5419c3b  state=OK
                         execution_duration 4.992s. Every span carried
                         `redaction.ignored.count`, so the Collector's
                         redaction processor ran on all six.
Sensitive sentinel check -- required set (§8):      PASS -- all 7 required
                         labels `absent`, 0 leaked, `scan` exit 0, none
                         skipped
Content sentinels (message / response text):        checked -- both
                         `absent`, giving 9 sentinels in one scan. Not part
                         of PASS (§9). The needles file was kept outside the
                         repository and deleted after the run.
Credential read bound to the request's container:   YES -- --expect-container
                         0690b9d00b39, the prefix recorded before the request

Unexpected side effects observed (§14 -- window, not attribution):
                                                    NO
Side-effect window anchored at §5's marker:         YES
                         marker at 11:34:29 UTC, dispatch at 11:34:38 UTC,
                         so the window opened 9s before the request and was
                         pre-committed, not reconstructed after the fact.
Post-validation checks performed:
                         - §14.1: 8 spans in the window -- the 6 request
                           spans plus `ping` and `MCP send ping`, the routine
                           Calendar-MCP keepalive §14 explicitly expects.
                           Zero non-keepalive `tools/call` spans.
                         - §14.2: 11 files changed under data/hermes, every
                           one inside §14's documented expected table:
                             response_store.db-{wal,shm}  (conversation store;
                               the Gateway sends "store": true)
                             state.db-*, kanban.db-*      (Hermes' own state)
                             cron/ticker_*, cron/.tick.lock,
                               state/gateway.heartbeat    (background tickers)
                             logs/agent.log               (Hermes' logs)
                           Nothing outside that table, nothing under a
                           project directory.
                         - §14.3: `git status --porcelain` clean
                         - `find` here is bfs 4.1.1, not GNU findutils; the
                           `-newer` form was verified working by positive
                           control at preflight, so "no unexpected change"
                           means the check ran rather than silently failed.
                         - §14.4: marker removed after the run.
                         Recorded as "no non-keepalive `tools/call` span
                         observed in the inspected window" -- never as "no
                         tool call for this request". Hermes does not
                         propagate trace context into its outbound MCP
                         calls, so §14 can neither attribute a tool call to
                         this request nor rule one out.

Criterion lifecycle:     The criteria were fixed BEFORE this run. The
                         contract merged at 2026-09-15 06:38 UTC (GitHub's
                         merged_at for PR #46); this run
                         executed at 11:34 UTC. No runbook text was modified
                         at any point during the preparation or the run, and
                         `infra/observability/tests/test_live_validation.py`
                         passed 71/71 at preflight, so the runbook's
                         "Expected" claims still match the code at this SHA.

Per-criterion state (§9) -- one line each, none omitted:
  §5 fixed input           PASS
  gateway dispatch/status  PASS
  trace / relationships    PASS
  required-set sentinels   PASS
  provenance (§3)          PASS
  side effects (§14)       PASS
Overall result:          PASS
                         (§9 aggregation: the worst state present; every
                          criterion is PASS)
Notes / limitations of this run:
                         - Content sentinels were checked, which is stronger
                           evidence than PASS requires; PASS is defined
                           against the required set (§9).
                         - §14 is a time-window observation, not request
                           attribution; the supportable claim is "no
                           unexpected side effect observed, by the checks
                           above".
                         - Hermes' terminal-toolset availability on the
                           /v1/responses path remains undetermined from this
                           repository -- a residual risk §5 accepts rather
                           than closes.
```

### 2026-09-15 (11:26 UTC) — NOT A RUNBOOK PASS: §14's window was never anchored

**Overall: NOT A RUNBOOK PASS.** Five of the six §9 criteria were
exercised and met, including §5's reply on the finalised contract. The
sixth — §14 — was not exercised as specified: §5's marker was never
dropped, so the side-effect window had no pre-committed anchor. The
canonical gate stayed **open** after this run; the 11:34 run closed it.

```text
Validation date:         2026-09-15 11:26:15 UTC (container clock; host
                         UTC offset 0s, verified immediately before the run)
Operator:                repository owner, interactive session

Repository SHA:          9a24abf7bb933c2ee645001cf422d6fd24cf3a96
                         (= merge commit of PR #46; == origin/master)
Working tree:            clean (before and after)
Containers (container ID prefix, image ID, started) -- all eight:
  slack-gateway:         dbab5033ac33  sha256:9c9be6031de05d6473d  2026-09-15T08:10:58
  orchestrator:          0690b9d00b39  sha256:17272b02c47d075db1d  2026-09-15T08:13:55
  hermes-agent:          9a669773e3ce  sha256:470aa3b68074d9d752f  2026-09-15T11:16:33
  google-calendar-mcp:   b4c605fd9fb4  sha256:72854aab401dc96750a  2026-09-15T11:22:41
  ollama:                7e7efecf4bef  sha256:dacbdaa86a43fb9ed58  2026-09-15T08:10:58
  otel-collector:        cea0f4340a25  sha256:e11c83206a71a0ac312  2026-09-15T08:13:37
  phoenix:               5db3fd4d83bd  sha256:e90c06c2bf2f22ef7d9  2026-09-15T08:10:58
  mlflow:                bac1f771b2de  sha256:ec446a27c197e760a63  2026-09-15T08:10:58
                         `provenance` run before the request and again
                         after evidence collection: all eight prefixes
                         identical, so nothing was recreated in between.

Runtime Python inputs match recorded SHA (§3):       YES
                         orchestrator: all inputs match (14 comparisons)
                         slack-gateway: all inputs match (12 comparisons)
                         Zero DIFFERS -- §3's ordered classification was not
                         reached and §12's carve-out was not relied on.
                         The orchestrator was rebuilt from 9a24abf and
                         recreated at preflight, clearing the 2026-09-14
                         docstring-only stop condition by rebuild rather
                         than by reinterpretation.
Outside that boundary (§3's table):                 acknowledged
Bind-mount integrity (extra preflight after the 11:09 FAIL; not a runbook step):
                         hermes-agent and google-calendar-mcp mounts were
                         repaired and re-verified by positive control;
                         otel-collector's config mount verified
                         behaviourally (health_check answers 200; spans
                         carry redaction.ignored.count).

Preconditions:
  all required services healthy:                    YES -- all eight
  gateway -> orchestrator GET /health:              200
  gateway env free of HERMES_API_*:                 YES -- exactly
                         ORCHESTRATOR_BASE_URL, OTEL_EXPORTER_OTLP_ENDPOINT,
                         SLACK_APP_TOKEN, SLACK_BOT_TOKEN (names only)
  thread under operator control:                    YES -- direct message;
                         the Gateway log's channel id is D-prefixed

Test input:              `Reply with this token only and nothing else:
                         SLACK_GATEWAY_OK` -- §5's canonical input, sent
                         once as a DM
Test input conforms to §5:                          YES
Slack reply observed (and matches §5's expected):   YES
                         `SLACK_GATEWAY_OK` -- the bare token, reported
                         verbatim by the operator from the thread.
                         Corroborated by response_chars=16, exactly the
                         expected reply's length, so nothing was added.
                         A length is not the text; the operator's in-thread
                         observation is what settles it.
Gateway log (dispatch -> status):
                         POST http://orchestrator:8700/dispatch → 200 OK
                         agent=hermes  status=completed  response_chars=16
                         event_id=Ev0C225BQRLL
                         delivery=posted_and_processing_status_deleted

Trace ID:                2baf2687afeb06c9b868e0e6e4776600
Observed spans:          6, one trace id:
                           concierge.request        51297949cf39  (root)
                             orchestrator.dispatch  8d9dec6c14ca
                               POST /dispatch       7c8cfc6b0a08
                                 hermes.request     8cf85e0ea110
                                   /v1/responses    3b0b34fedef7
                             slack.response         b3fce9a4285c
Expected parent-child relationships:                PASS -- all five,
                                                    `trace` exit 0
Gateway → Orchestrator confirmed:                   YES
Orchestrator → Hermes confirmed:                    YES
Present in MLflow (trace id, state):
                         tr-2baf2687afeb06c9b868e0e6e4776600  state=OK
                         execution_duration 7.817s. Every span carried
                         `redaction.ignored.count`, so the Collector's
                         redaction processor ran on all six.
Sensitive sentinel check -- required set (§8):      PASS -- all 7 required
                         labels `absent`, 0 leaked, `scan` exit 0, none
                         skipped
Content sentinels (message / response text):        checked -- both
                         `absent`, giving 9 sentinels in one scan. Not part
                         of PASS (§9). Needles file was kept outside the
                         repository and deleted after the run.
Credential read bound to the request's container:   YES -- --expect-container
                         0690b9d00b39, the prefix recorded before the request

Unexpected side effects observed (§14 -- window, not attribution):
                                                    NO -- but see the
                                                    anchoring defect below
Side-effect window anchored at §5's marker:         **NO**
                         `/tmp/live-validation-start` was never created.
                         §5 requires it to be dropped immediately before
                         sending; that step was skipped.
Post-validation checks performed:
                         - §14.1: 6 spans in the window, all of them the
                           expected request spans. No `tools/call` span at
                           all, keepalive or otherwise.
                         - §14.2: run against a RECONSTRUCTED anchor -- a
                           reference file stamped 11:26:00 UTC, 15s before
                           the 11:26:15 dispatch -- NOT §5's marker. 14
                           files changed, every one of them inside §14's
                           documented expected table:
                             response_store.db-{wal,shm}  (conversation
                               store; the Gateway sends "store": true)
                             state.db-*, kanban.db-*      (Hermes' own state)
                             cron/ticker_*, cron/.tick.lock,
                               state/gateway.heartbeat    (background tickers)
                             logs/agent.log, logs/errors.log
                             models_dev_cache.*           (metadata refresh)
                           Nothing outside that table, nothing under a
                           project directory.
                           (This file activity also confirms the mount
                           repair: the 11:09 FAIL showed zero changes
                           because the container was writing to a detached
                           filesystem.)
                         - §14.3: `git status --porcelain` clean
                         - `find` here is bfs 4.1.1; the `-newer` form was
                           verified by positive control at preflight.
                         Recorded as "no non-keepalive `tools/call` span
                         observed in the inspected window" -- never as "no
                         tool call for this request".

WHY THIS IS NOT A PASS:
  The substantive answer §14 seeks is almost certainly the same either
  way: the reconstructed anchor sits 15s before the dispatch, so it is
  strictly at-or-wider than the marker would have been, and it cannot
  drift. But the anchor was **chosen after the result was known**, from
  the Gateway log. §5's marker is evidence created *before* the outcome
  exists; a reconstructed anchor is a judgement inserted after it.

  That difference is the exact lifecycle property this runbook was
  rewritten to protect -- "a canonical gate has to be fixed before the
  run that closes it". The 2026-09-14 run was reclassified for the same
  shape of error: reading a gate generously, mid-run, in the direction
  of the answer already in hand. Accepting a post-hoc anchor because the
  file list looks clean would repeat it.

  Broadening §14 to accept a reconstructed anchor whose timestamp
  provably precedes the dispatch is a **defensible rule** -- but it is a
  *different* rule, and per §12's own reasoning it should be written
  into the runbook deliberately, before the run that relies on it, not
  adopted in the middle of one.

Per-criterion state (§9) -- one line each, none omitted:
  §5 fixed input           PASS
  gateway dispatch/status  PASS
  trace / relationships    PASS
  required-set sentinels   PASS
  provenance (§3)          PASS
  side effects (§14)       NOT EXERCISED  (no §5 marker; window reconstructed)
Overall result:          NOT A RUNBOOK PASS
                         (§9 aggregation: the worst state present.
                          NOT A RUNBOOK PASS outranks INCONCLUSIVE because
                          the fix is simply to run the criterion.)
Verified subset:         Everything except §14's anchoring. On the
                         finalised PR #46 contract, against a runtime
                         rebuilt from the recorded SHA: §5's canonical
                         input produced exactly `SLACK_GATEWAY_OK`; the
                         Gateway dispatched through the Orchestrator to
                         the registered "hermes" Agent and replied
                         in-thread; W3C trace context was continuous
                         across all six spans with correct parenting,
                         present in both Phoenix and MLflow (state=OK);
                         all 7 required sentinels plus both content
                         sentinels were absent, read from the request's
                         own container; and §3's source comparison
                         matched the recorded SHA exactly.
Notes / limitations of this run:
                         - Remedy is cheap and needs no rebuild: drop the
                           marker, send one message. Provenance is stable
                           and nothing has been recreated since it was
                           recorded, so the next attempt does NOT need to
                           restart from §3 -- only to re-record the run.
                         - Retry safety, decided and recorded per §11:
                           repeating is judged SAFE. The turn completed
                           with a known result, and the reconstructed-
                           anchor checks above found no `tools/call` span
                           and only files inside §14's documented routine
                           set. A post-hoc anchor is not enough for a PASS,
                           but it is adequate evidence for a retry decision,
                           which is a judgement §11 asks for explicitly. This is a
                           decision with its basis, not an inference that
                           a known outcome is automatically repeatable.
```

### 2026-09-15 (11:09 UTC) — FAIL: Hermes had no inference provider

(stale bind mount, not a wiring defect)

**Overall: FAIL. The canonical gate remains OPEN.** The Slack →
Orchestrator → Hermes wiring behaved correctly end to end; the run failed
because the `hermes-agent` container was not running the repository's
intended configuration at all.

```text
Validation date:         2026-09-15 11:09:45 UTC (container clock; host
                         UTC offset 0s, verified at preflight)
Operator:                repository owner, interactive session

Repository SHA:          9a24abf7bb933c2ee645001cf422d6fd24cf3a96
                         (= merge commit of PR #46; == origin/master)
Working tree:            clean (before and after)
Containers (container ID prefix, image ID, started) -- all eight:
  slack-gateway:         dbab5033ac33  sha256:9c9be6031de05d6473d  2026-09-15T08:10:58
  orchestrator:          0690b9d00b39  sha256:17272b02c47d075db1d  2026-09-15T08:13:55
  hermes-agent:          9d53cbf88567  sha256:470aa3b68074d9d752f  2026-09-15T08:10:58
  google-calendar-mcp:   8b868cfc3258  sha256:72854aab401dc96750a  2026-09-15T08:10:58
  ollama:                7e7efecf4bef  sha256:dacbdaa86a43fb9ed58  2026-09-15T08:10:58
  otel-collector:        cea0f4340a25  sha256:e11c83206a71a0ac312  2026-09-15T08:13:37
  phoenix:               5db3fd4d83bd  sha256:e90c06c2bf2f22ef7d9  2026-09-15T08:10:58
  mlflow:                bac1f771b2de  sha256:ec446a27c197e760a63  2026-09-15T08:10:58
                         `provenance` was run before the request and again
                         after evidence collection: all eight prefixes
                         unchanged, so nothing was recreated in between.
                         The orchestrator was rebuilt from 9a24abf and
                         recreated during preflight (df6eb0bff364 ->
                         0690b9d00b39), which is what cleared the
                         2026-09-14 docstring-only stop condition.

Runtime Python inputs match recorded SHA (§3):       YES
                         orchestrator: all inputs match (14 comparisons)
                         slack-gateway: all inputs match (12 comparisons)
                         Zero DIFFERS, so §3's ordered classification was
                         not reached and §12's carve-out was not relied
                         on. hermes_agent.py now byte-identical to
                         9a24abf (53bb5f9a4c53...).
Outside that boundary (§3's table):                 acknowledged
                         **And this run shows why that boundary matters.**
                         `data/hermes/` is gitignored local state, outside
                         the source check entirely. §3 passed while the
                         Agent's actual runtime configuration was wrong.

Preconditions (as recorded at preflight):
  all required services healthy:                    YES (all eight up;
                         otel-collector had hit the documented post-engine-
                         restart Exited (127) and was recovered per §4:
                         eee977aa44f6 -> cea0f4340a25, health 200)
  gateway -> orchestrator GET /health:              200 (run after the
                         orchestrator recreate)
  gateway env free of HERMES_API_*:                 YES
  thread under operator control:                    YES -- direct message,
                         channel id is D-prefixed

Test input:              `Reply with this token only and nothing else:
                         SLACK_GATEWAY_OK` -- §5's canonical input, sent
                         once as a DM
Test input conforms to §5:                          YES
Slack reply observed (and matches §5's expected):   NO
                         Observed: `Provider authentication failed: No
                         inference provider configured. Run 'hermes model'
                         to choose a provider and model, or set an API key
                         (OPENROUTER_API_KEY, OPENAI_API_KEY, etc.) in
                         ~/.hermes/.env.`
                         Corroborated by response_chars=199 against the
                         expected reply's 16.
Gateway log (dispatch -> status):
                         POST http://orchestrator:8700/dispatch → 200 OK
                         agent=hermes  status=completed  response_chars=199
                         event_id=Ev0C1H177LUX
                         Orchestrator: dispatch succeeded status='completed'

Trace ID:                fb642c64fabdfb2e9df5c64dade277de
Observed spans:          6, one trace id:
                           concierge.request        682bd83839f2  (root)
                             orchestrator.dispatch  31ad8cb64765
                               POST /dispatch       66ef4944a34f
                                 hermes.request     2a24d13ceb0c
                                   /v1/responses    4da9ede21d78
                             slack.response         7c8d1cd90e48
                         `/v1/responses` is Hermes' INBOUND HTTP handler
                         span, so it appears even though no model call was
                         ever made.
Expected parent-child relationships:                PASS -- all five,
                                                    `trace` exit 0
Gateway → Orchestrator confirmed:                   YES
Orchestrator → Hermes confirmed:                    YES
                         Every span carried `redaction.ignored.count`, so
                         the Collector's redaction processor ran on all six.
Present in MLflow (trace id, state):                not collected
Sensitive sentinel check -- required set (§8):      NOT EXERCISED -- `scan`
                         was not run. The run was already FAIL on §5 and
                         the sentinel check would not have changed the
                         outcome; it is recorded as not exercised rather
                         than assumed clean.
Content sentinels (message / response text):        not performed
Credential read bound to the request's container:   N/A (scan not run)

Unexpected side effects observed (§14 -- window, not attribution):
                                                    UNKNOWN -- §14.1 was
                                                    not performed; §14.2 and
                                                    §14.3 found nothing
Side-effect window anchored at §5's marker:         YES -- marker at
                         11:09:24 UTC, dispatch at 11:09:45 UTC, so the
                         window opened 21s before the request.
Post-validation checks performed:
                         - §14.1 (span window, non-keepalive `tools/call`):
                           **NOT performed at the time.** Phoenix was
                           queried only to find this request's trace id,
                           which does not establish whether a `tools/call`
                           span appeared in the window.
                         - `find data/hermes -newer <marker>`: ZERO files
                           changed. Note a successful turn normally touches
                           ~a dozen files including response_store.db-wal;
                           zero is consistent with the turn failing before
                           any inference or persistence occurred.
                         - `git status --porcelain`: clean
                         - `find` here is bfs 4.1.1; the `-newer` form was
                           verified working by positive control at preflight,
                           so "no changes" means no changes rather than a
                           check that failed to run.
                         - Marker removed after the run.

SUPPLEMENTARY, AFTER THE FACT -- does not change any state above:
  §14.1's span query was run on 2026-09-17, two days later, against the
  window this run's marker had already fixed (11:09:24 UTC) up to the next
  attempt's start (11:26:00 UTC). Zero `tools/call` spans. In time order:
      11:09:44-46  the six spans of this request, and nothing else until
      11:16:42     MCP initialize / tools/list  <- hermes-agent recreated
                                                   at 11:16:33 (remediation)
      11:19:42     MCP ping                     <- routine keepalive
      11:22:42-45  MCP initialize / tools/list  <- google-calendar-mcp
                                                   recreated at 11:22:41
      11:25:45     MCP ping                     <- routine keepalive
  This is recorded because it exists, not to upgrade §14: it was performed
  after the retry decision below had already been made, so it was not part
  of that decision's basis, and a check performed two days later is not
  the post-validation check §14 describes. §14 stays NOT EXERCISED.

ROOT CAUSE (established, not inferred):
  The `hermes-agent` bind mount `./data/hermes -> /opt/data` was STALE.
  The mount was still DEFINED correctly in `docker inspect`, but the
  container was reading a detached filesystem:
      .env         host   166 bytes   container 24792 bytes
      config.yaml  host  7477 bytes   container 100551 bytes
      entries      host     57        container    41
  Positive control: a file created on the host under `data/hermes/` was
  NOT visible at `/opt/data/` inside the container.
  The detached copy carried no usable provider credential, so Hermes'
  primary provider failed to authenticate and no fallback was configured:
      "Primary provider auth failed: ... — trying fallback"
      "Provider authentication failed for session=19b0ebcb-..."
  The host's own `data/hermes/.env` does carry ANTHROPIC_API_KEY (names
  checked only; values never printed) and `config.yaml` names
  provider: anthropic / claude-opus-4-6.

  **This is the same failure class as §4's documented otel-collector
  `Exited (127)` after a Docker Desktop / WSL engine restart -- but it
  fails SILENTLY.** The collector exits loudly; a stale data mount leaves
  the service "running and healthy" while it reads the wrong filesystem.
  §4 names the collector case only.

REMEDIATION APPLIED (after the run):
  `docker compose up -d --no-deps --force-recreate hermes-agent`
  9d53cbf88567 -> 9a669773e3ce. Mount verified repaired: .env 166/166,
  config.yaml 7477/7477, entries 55/55, and the host-created probe file
  IS now readable inside the container.
  Whether the ANTHROPIC_API_KEY itself is valid is NOT established by
  this repair -- only the canonical run can determine that.

STILL OUTSTANDING:
  `google-calendar-mcp` has the SAME stale bind mount
  (`./data/google-calendar -> /data/google-calendar`): host 2 entries,
  container 0, probe invisible. It was still "running (healthy)", since
  its health check does not touch the mount. Not recreated at the time
  this record was written; it was recreated at 11:22 UTC, before the next
  attempt (see the 11:26 and 11:34 records).

Per-criterion state (§9) -- one line each, none omitted:
  §5 fixed input           FAIL   (exercised; expected reply not observed)
  gateway dispatch/status  PASS   (200, status=completed)
  trace / relationships    PASS   (6 spans, one trace id, `trace` exit 0)
  required-set sentinels   NOT EXERCISED
  provenance (§3)          PASS
  side effects (§14)       NOT EXERCISED  (§14.1 not performed; §14.2
                                           and §14.3 found nothing)
Overall result:          FAIL
                         (§9 aggregation: the worst state present)
Verified subset:         The Slack → Orchestrator → Hermes wiring itself
                         is exercised and correct: the Gateway dispatched,
                         the Orchestrator routed to the registered "hermes"
                         Agent, Hermes was reached and replied through the
                         Orchestrator, the Gateway posted in-thread, and
                         W3C trace context was continuous across all six
                         spans with correct parenting. What failed is
                         Hermes' model backend, downstream of that wiring.
Notes / limitations of this run:
                         - This FAIL is not evidence against PR #43's
                           rewiring; see Verified subset.
                         - §5's criterion is the expected reply, and it was
                           not observed. The run is FAIL, not a near-miss,
                           and was NOT re-sent automatically (§11).
                         - Retry safety, decided and recorded per §11:
                           repeating is judged SAFE. The basis, and only
                           this basis: Hermes' own log shows the turn
                           failing at provider authentication with no
                           fallback, so no model call succeeded and no
                           model-selected tool call is expected; §14.2
                           found zero file changes under data/hermes; §14.3
                           found a clean repository. §14.1 was NOT part of
                           the basis, because it was not performed, so a
                           tool call is ruled out by inference from the log
                           rather than by observation. This is a decision
                           recorded with its basis, not an assumption that
                           a known outcome is repeatable.
                         - Because the remediation recreated hermes-agent,
                           the next attempt is a FRESH run starting from §3.
```

### 2026-09-14 (15:09 UTC) — the run that exposed §5's and §12's gaps

**Not a canonical PASS — and originally recorded as one.** This run was
written up here as `PASS`. Review found two defects in that
classification and it was reclassified before the record merged. The
correction is recorded rather than quietly applied, because the two
runbook defects it surfaced are what this run is actually useful for.

```text
1. §12's carve-out does not cover docstrings.  Its text excuses a
   `DIFFERS` shown to be *comment-only*. Python docstrings are string
   constants bound to `__doc__`, not comments -- so the §3 difference
   below was a stop condition, and continuing past it read the gate more
   generously than it is written. §12 now says so explicitly.

2. The criterion was finalised after the result was observed.  §5 named
   a fixed input but not an expected reply, the ambiguity was discovered
   mid-run, and the acceptance semantics were written afterwards -- in
   the same change that classified this run against them. The observed
   reply satisfies the finalised rule cleanly, so the result is not in
   doubt; the *lifecycle* is. A canonical gate has to be fixed before
   the run that closes it, or the run helps define the criterion that
   judges it.
```

Both defects are fixed in this same change, so the next run meets a
runbook settled in advance. The gate stayed **open** until that run —
which is the 2026-09-15 11:34 UTC record above. **The gate is now
closed.**

```text
Validation date:         2026-09-14 15:09:02 UTC (container clock; host
                         UTC differed by ~1s, so log and mtime comparisons
                         need no offset correction)
Operator:                repository owner, interactive session

Repository SHA:          83ed28ad9b9224e651d19115df7a9abc6f7f3551
                         (= merge commit of PR #45; == origin/master)
Working tree:            clean (before the request and after it)
Containers (container ID prefix, image ID, started) -- all eight:
  slack-gateway:         dbab5033ac33  sha256:9c9be6031de05d6473d  2026-09-10T20:50:41
  orchestrator:          df6eb0bff364  sha256:f021e28af4b6c2eda0a  2026-09-10T20:50:41
  hermes-agent:          9d53cbf88567  sha256:470aa3b68074d9d752f  2026-09-10T20:50:41
  google-calendar-mcp:   8b868cfc3258  sha256:72854aab401dc96750a  2026-09-10T20:50:41
  ollama:                7e7efecf4bef  sha256:dacbdaa86a43fb9ed58  2026-09-10T20:50:41
  otel-collector:        eee977aa44f6  sha256:e11c83206a71a0ac312  2026-09-11T07:18:43
  phoenix:               5db3fd4d83bd  sha256:e90c06c2bf2f22ef7d9  2026-09-10T20:50:41
  mlflow:                bac1f771b2de  sha256:ec446a27c197e760a63  2026-09-10T20:50:41
                         `provenance` was re-run after the scan: all eight
                         prefixes were unchanged, so nothing was recreated
                         between the record and the evidence.
Runtime Python inputs match recorded SHA (§3):
                         NO, in one respect -- and the same one the
                         2026-09-10 runs hit, still unrepaired because a
                         rebuild was deliberately not part of this run.
                         `slack-gateway`: all inputs match 83ed28ad.
                         `orchestrator`: every input matches except
                         `orchestrator/hermes_agent.py`, which is
                         byte-identical to that file at 0a03c56. The two
                         commits that have touched it since (094b984,
                         6aa80ce) changed only its module docstring and
                         `_extract_output_text`'s.
                         Established mechanically, not by reading the
                         diff: parsing both files and stripping every
                         docstring yields **identical** ASTs, while
                         keeping them yields differing ones -- so the
                         whole difference is string constants and no
                         executable code differs. That is §3's step 3,
                         and it classifies the difference as
                         docstring-only.
                         **At the time, the operator read §12's
                         `comment-only` carve-out as covering that and
                         continued the run.** Review later found the
                         interpretation incorrect: docstrings are string
                         constants bound to `__doc__`, not comments, so
                         the carve-out never applied and this was a stop
                         condition. §12 and §3 were both rewritten in the
                         change that carries this record. Also
                         compared and matching:
                         `packages/agent-contracts`'s Python files as
                         installed into site-packages, and
                         `packages/agent-contracts/pyproject.toml`.
Outside that boundary (§3's table):                 acknowledged

Preconditions:
  all required services healthy:          YES -- all eight up; otel-collector
                                          checked with `ps -a` and its health
                                          endpoint returned 200 (no Exited 127
                                          this time, so no recreation was needed)
  gateway → orchestrator GET /health:     200
  gateway env free of HERMES_API_*:       YES -- exactly ORCHESTRATOR_BASE_URL,
                                          OTEL_EXPORTER_OTLP_ENDPOINT,
                                          SLACK_APP_TOKEN, SLACK_BOT_TOKEN
                                          (names only; values never printed)
  thread under operator control:          YES -- a direct message; the Gateway
                                          log's channel id is D-prefixed

Test input:              `Reply with exactly SLACK_GATEWAY_OK.` -- §5's
                         fixed string *as §5 read at the time*, sent once
                         as a direct message
Test input conforms to §5:                          YES as §5 read then;
                         NO against the finalised §5, whose input this
                         same change replaces with
                         `Reply with this token only and nothing else:
                         SLACK_GATEWAY_OK` precisely because the old
                         one's trailing period was ambiguous
Slack reply observed (and matches §5's expected):
                         `SLACK_GATEWAY_OK` -- the bare token, with no
                         punctuation, formatting or added content,
                         reported verbatim by the operator from the
                         thread. Corroborated by the Gateway's
                         `response_chars=16`: exactly the token's length,
                         so the model added nothing. It would satisfy the
                         finalised §5 as well -- but §5 did not name an
                         expected reply when this run started, so what
                         this run compared against was decided after the
                         reply was in hand. That is the defect, not the
                         reply.
Gateway log (dispatch → status):
                         POST http://orchestrator:8700/dispatch → 200 OK
                         agent=hermes  status=completed  response_chars=16
                         delivery=posted_and_processing_status_deleted

Trace ID:                9056c40f4df2d6181ecc81e80f2bb83d
Observed spans:          6, one trace id:
                           concierge.request        c381cfb838bf  (root)
                             orchestrator.dispatch  dc55d9c47053
                               POST /dispatch       8d370940a7da
                                 hermes.request     74cc707b6402
                                   /v1/responses    1f0bc35efff2
                             slack.response         5bd1bd4a411a
Expected parent-child relationships:                PASS -- all five,
                                                    `trace` exit 0
Gateway → Orchestrator confirmed:                   YES
Orchestrator → Hermes confirmed:                    YES
Present in MLflow (trace id, state):
                         tr-9056c40f4df2d6181ecc81e80f2bb83d  state=OK
                         Every span also carried `redaction.ignored.count`,
                         so the Collector's redaction processor ran on all
                         six.
Sensitive sentinel check -- required set (§8):      PASS -- all 7 labels
                                                    `absent`, 0 leaked,
                                                    `scan` exit 0, none
                                                    skipped
Content sentinels (message / response text):        checked -- both
                                                    `absent`, giving 9
                                                    sentinels in one scan.
                                                    Practical because §5's
                                                    input and its expected
                                                    reply are both single
                                                    lines, expressible
                                                    verbatim in the
                                                    line-based needles file.
                                                    Not part of PASS (§9).
Credential read bound to the request's container:   YES -- `--expect-container
                                                    df6eb0bff364`, the prefix
                                                    recorded before the request

Unexpected side effects observed (§14 -- window, not attribution):
                                                    NO
Side-effect window anchored at §5's marker:         YES -- marker at
                                                    15:08:24 UTC, dispatch at
                                                    15:09:02 UTC, so the
                                                    window opened 38s before
                                                    the request and could not
                                                    have drifted past it
Post-validation checks performed:
                         1. Phoenix span scan over 14:09:57–15:12:57 UTC,
                            which contains the request: **zero** `tools/call`
                            spans. The only other traffic was 22 paired
                            `MCP send ping` / `ping` keepalives at ~3-minute
                            intervals -- the routine Calendar-MCP keepalive
                            §14 names. This is a time-window observation and
                            not request attribution (§14).
                         2. `find data/hermes -newer` the marker: 11 files,
                            all within §14's expected table --
                            `response_store.db-{shm,wal}` (the conversation
                            store, expected because the Gateway sends
                            `"store": true`), `state.db-*`, `kanban.db-*`,
                            `cron/ticker_heartbeat`,
                            `cron/ticker_last_success`,
                            `state/gateway.heartbeat`, `logs/agent.log`.
                            One file is recorded separately rather than
                            folded in: `cron/.tick.lock`, which §14's table
                            does not name by filename. It is the lock of the
                            `cron/ticker_*` mechanism the table does name, it
                            is under `data/hermes`, and it is neither a new
                            file outside that tree, nor under a project
                            directory, nor a repository change -- so it is
                            not one of the three things §14 defines as
                            unexpected.
                         3. `git status --porcelain` empty.
                         4. Marker removed; the needles file was written
                            outside the repository and deleted.

Per-criterion state (§9) -- one line each, none omitted:
  §5 fixed input           NOT EXERCISED -- §5's then-current input was
                           sent and a reply was observed, but §9's
                           criterion is the input *and its expected
                           reply*, and no expected reply existed to
                           exercise it against. One was written after
                           this reply was observed, which is not the same
                           as meeting a pre-existing criterion.
  gateway dispatch/status  PASS -- POST /dispatch 200, status=completed
  trace / relationships    PASS -- 6 spans, one trace id, all five
                           relationships, `trace` exit 0
  required-set sentinels   PASS -- 7/7 absent, `scan` exit 0
  provenance (§3)          FAIL -- recorded thoroughly (all eight before
                           the request, re-verified unchanged after it)
                           and compared against the recorded SHA, which
                           is more than either earlier run did. But the
                           comparison reported `DIFFERS`, and the
                           difference is docstring-only, which §12 does
                           not excuse: docstrings are string constants,
                           not comments. The criterion was exercised and
                           not met. What happened at the time: the
                           operator treated the docstring-only difference
                           as satisfying §12's carve-out and continued,
                           arguing that a strict reading would otherwise
                           permit a run that could never pass. Review
                           later found that interpretation incorrect,
                           because docstrings are not comments. §12 now
                           forecloses the argument rather than leaving it
                           available.
  side effects (§14)       PASS -- window correctly anchored, and no
                           unexpected side effect observed by any of the
                           three checks
Overall result:          FAIL
                         (§9's aggregation rule: the worst state present.
                         `FAIL` outranks the `NOT EXERCISED` on §5's
                         criterion, so the run label is `FAIL` even
                         though four criteria passed -- rule 1 is why
                         every state above is recorded individually
                         rather than collapsed into the headline.)
Verified subset:         dispatch through the Orchestrator, trace
                         continuity across all five relationships,
                         required-set *and* content sentinel absence, and
                         credential binding to the serving container.
                         Provenance and §5's criterion are NOT part of
                         this subset.
Notes / limitations of this run:                     see below
```

**What this run established anyway.** Its label is `FAIL`, and four of
its six criteria still passed on evidence neither 2026-09-10 run
produced. Provenance was recorded *before* the request from §3's helper
and re-verified unchanged *after* the scan, rather than inferred
retroactively from image ids — which is precisely why the `DIFFERS` was
caught at all. The side-effect window was anchored at §5's marker, so its
clean result is usable evidence instead of the `INCONCLUSIVE` the
superseded drifting window produced twice. The optional content extension
ran, so the message and response text are additionally known absent from
telemetry. And the procedure did what a gate is for: it surfaced two
defects in itself rather than producing a comfortable answer.

**What has to be true for the next run to close the gate.** Both items
are prerequisites, not preferences:

1. **The Orchestrator is rebuilt from the recorded SHA and recreated**, so
   §3's comparison reports no `DIFFERS` and §12's carve-out is never
   consulted. Recreation replaces the container, so provenance must be
   captured fresh afterwards — the run starts at §3, not around it.
   Broadening §12 instead would also resolve it, but that is a different
   rule and §12 now says it must be adopted deliberately and in advance,
   not read into the existing text mid-run.
2. **The runbook it is judged against is the one merged before it ran.**
   This change finalises §5's input and expected reply; the next run
   simply follows them.

**Limitations of this run.**

- The running Orchestrator was **not** built from the recorded SHA, and
  under §12 as now written that is a stop condition rather than an
  explained difference. Its executable code is identical to 83ed28ad —
  established by AST comparison, not by reading the diff — so nothing
  here suggests the *behaviour* observed was wrong. What it means is that
  this run is evidence about a build the record cannot name, which is
  exactly what §3's comparison exists to prevent.
- Success path only. The failure and `OUTCOME_UNKNOWN` paths (§10) remain
  test-covered and still unobserved live.
- Span *kind* was not verified: Phoenix's REST API reports `UNKNOWN` (§7).
- **No tool call is attributed, and none is ruled out.** §14's span check
  is a time-window observation; Hermes does not propagate trace context to
  its outbound MCP calls, so a tool call would appear as an unrelated
  trace with no link back to this request. The supportable claim is the
  one recorded: no non-keepalive `tools/call` span in the inspected
  window.
- No Calendar tool was invoked, so Hermes' outbound-MCP propagation gap
  was neither confirmed nor contradicted — as in both earlier runs.
- §5's residual risk stands: whether Hermes' terminal toolset is available
  on the `/v1/responses` path remains undeterminable from this repository,
  which is why this record says "no unexpected side effect *observed*".
- Everything in §3's "outside the boundary" table is untouched by this
  run: the Dockerfiles, build args, install-time resolution,
  `hermes-agent`'s instrumentation layer, `google-calendar-mcp`'s source,
  and the upstream `:latest` images' non-reproducible identities.

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
  exercised. A canonical runbook PASS still needs a run that sends §5's
  input and observes its expected reply. (This record originally quoted
  that input as `Reply with exactly SLACK_GATEWAY_OK.`; §5 has since
  replaced it, because that period was ambiguous — see §5 and the
  2026-09-14 record. The point stands, only the string has moved.)
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
