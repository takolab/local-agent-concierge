# Roadmap

This roadmap describes the planned development phases for Local Agent Concierge.

The project will be developed incrementally. Each milestone should produce a small, testable improvement before additional services and agents are introduced.

## Current Status

* [x] Create the public GitHub repository
* [x] Add the initial README
* [x] Add the MIT License
* [x] Define the initial system architecture
* [x] Define the containerized development environment
* [x] Run Ollama through Docker Compose
* [x] Connect Hermes Agent to Ollama
* [x] Connect the system to Slack
* [x] Add Google Calendar integration
* [x] Add observability foundation
* [ ] Compare Phoenix and MLflow using shared OpenTelemetry traces
* [ ] Add human approval for sensitive actions
* [ ] Add multi-agent orchestration
* [ ] Add shared memory
* [ ] Add extended observability and evaluations

## Containerization Strategy

All application services should run as separate Docker containers and be managed through Docker Compose.

Planned and implemented containerized services include:

* Ollama
* Hermes Agent
* Slack Gateway
* Google Calendar MCP
* OpenTelemetry Collector
* Phoenix
* MLflow
* Concierge Orchestrator
* Memory Service
* Approval Service
* PostgreSQL

Each container should have a focused responsibility.

Services should communicate through the internal Docker Compose network using service names as hostnames.

For example, Hermes Agent connects to Ollama using the OpenAI-compatible base URL:

```text
http://ollama:11434/v1
```

It should not use:

```text
http://localhost:11434
```

Inside a container, `localhost` refers to that container itself rather than another Compose service.

Services are introduced incrementally. Milestone 1 established the containerized Ollama foundation. Milestone 2 added Hermes Agent to the same Docker Compose application and verified model connectivity and tool calling. Later milestones will add the Slack Gateway, the Orchestrator, shared memory, approval workflows, and observability.

Persistent data should be stored in Docker volumes or ignored local directories instead of disposable container filesystems.

The host environment should require as few dependencies as practical. Ideally, a developer should only need:

- Git
- Docker Engine
- Docker Compose
- NVIDIA Container Toolkit when GPU acceleration is used

## Planned Container Architecture

```text
Docker Compose
├── ollama
├── hermes-agent
├── slack-gateway
├── google-calendar-mcp
├── otel-collector
├── phoenix
├── mlflow
├── orchestrator
├── approval-service
├── memory-service
└── postgres
```

The initial implementation will not start all of these containers at once. Each service will be added after its dependencies have been tested.

## Milestone 1: Containerized Local Foundation

### Goal

Create the initial Docker Compose environment and run Ollama as the first containerized service.

This milestone establishes the shared containerization conventions that will later be used by Hermes Agent, the Slack Gateway, the Orchestrator, the Memory Service, PostgreSQL, and Phoenix.

### Tasks

* [x] Define the Ollama service in `docker-compose.yml`
* [x] Define a shared Docker network
* [x] Create a persistent Docker volume for Ollama models
* [x] Expose the Ollama API to the local host
* [x] Add an Ollama health check
* [x] Validate the Docker Compose configuration
* [x] Start the Ollama container
* [x] Confirm that the container is healthy
* [x] Download the initial Gemma 4 12B model
* [x] Send a test prompt to the model
* [x] Confirm that GPU acceleration is available
* [x] Document the local Docker Compose commands

### Planned Flow

```text
Host terminal
  |
  v
Docker Compose
  |
  v
Ollama container
  |
  v
Local LLM response
```

### Completion Criteria

This milestone is complete because:

1. `docker compose up -d ollama` starts Ollama successfully.
2. `docker compose ps` reports the service as healthy.
3. Ollama retains the downloaded model after the container is recreated.
4. The Ollama API returns a model-generated response.
5. The model can use NVIDIA GPU acceleration.
6. The effective context length can be inspected through `/api/ps`.

## Milestone 2: Containerized Hermes Agent Integration

### Goal

Run Hermes Agent as a Docker container and connect it to the Ollama container through the internal Docker network.

### Tasks

* [x] Select the official Hermes Agent container image
* [x] Add the Hermes Agent service to `docker-compose.yml`
* [x] Create an ignored persistent bind mount for Hermes configuration and state
* [x] Map the container user to the host UID and GID
* [x] Attach Hermes Agent to the shared Docker network
* [x] Configure Hermes Agent to wait for Ollama health
* [x] Configure the Ollama base URL as `http://ollama:11434/v1`
* [x] Configure Gemma 4 12B as a custom model provider
* [x] Configure the Chat Completions API compatibility mode
* [x] Verify the Ollama `/v1/models` endpoint from the Hermes container
* [x] Send a terminal-based chat request through Hermes Agent
* [x] Confirm that Hermes requests reach `/v1/chat/completions`
* [x] Verify Terminal Tool Calling
* [x] Verify that a tool side effect occurs only once
* [x] Confirm that Hermes configuration survives container recreation
* [x] Evaluate Hermes diagnostics and container supervision
* [x] Document the Hermes container setup

### Verified Flow

```text
Host terminal
  |
  v
Hermes Agent container
  |
  | POST /v1/chat/completions
  v
Ollama container
  |
  v
gemma4:12b
  |
  v
Hermes response
```

### Verified Tool Calling

```text
User instruction
  |
  v
Gemma 4 selects the Terminal Tool
  |
  v
Hermes executes the command
  |
  v
Tool result returns to Gemma 4
  |
  v
Final response
```

A controlled file-writing test confirmed that the Terminal Tool side effect occurred exactly once.

The final response formatting was inconsistent during one test, so sensitive or destructive tool operations will require validation, logging, and human approval in later milestones.


### Completion Criteria

This milestone is complete because:

1. Hermes Agent runs through the official Docker image.
2. Hermes resolves Ollama using the Compose service hostname.
3. Hermes lists `gemma4:12b` through the OpenAI-compatible API.
4. A request sent through Hermes returns an Ollama-generated response.
5. Ollama logs confirm requests to `/v1/chat/completions`.
6. Hermes successfully invokes its Terminal Tool.
7. A controlled file test confirms that the external side effect occurs once.
8. Hermes configuration persists independently of the disposable container.
9. No Hermes-specific software needs to be installed directly on the host.
10. The setup and observed limitations are documented.

## Milestone 3: Containerized Slack Gateway

### Goal

Run the Slack Gateway as a Docker container and allow Slack messages to reach Hermes Agent and Ollama.

### Tasks

* [x] Create a Slack application
* [x] Enable Slack Socket Mode
* [x] Configure the required Slack bot scopes
* [x] Configure the required Slack event subscriptions
* [x] Create the `apps/slack-gateway` application
* [x] Add a Dockerfile for the Slack Gateway
* [x] Add the Slack Gateway service to `docker-compose.yml`
* [x] Store Slack credentials in the local `.env` file
* [x] Read Slack credentials through environment variables
* [x] Establish the Socket Mode connection
* [x] Receive a Slack direct message
* [x] Normalize the Slack message into an internal request
* [x] Forward the request to Hermes Agent
* [x] Return the generated response to Slack
* [x] Preserve Slack thread information
* [x] Prevent duplicate Slack event processing
* [x] Add structured application logging
* [x] Document the Slack setup process

### Verified Flow

```text
Slack direct message
  |
  | Socket Mode
  v
Slack Gateway container
  |
  | POST /v1/responses
  v
Hermes Agent container
  |
  | POST /v1/chat/completions
  v
Ollama container
  |
  v
Slack thread response
```

### Completion Criteria

This milestone is complete because:

1. A direct message can be sent to the Slack application.
2. The Slack Gateway receives messages through Socket Mode.
3. Hermes Agent processes each normalized request.
4. Ollama generates the model response.
5. The response appears in the correct Slack thread.
6. Follow-up messages in the same thread reuse the same Hermes conversation.
7. A temporary processing status is displayed and removed after completion.
8. Gateway-level failures are reported to the Slack user.
9. Duplicate Slack event IDs are ignored by the Gateway process.
10. The Slack Gateway does not expose a public HTTP endpoint.
11. The Slack setup and known limitations are documented.

## Milestone 4: Google Calendar Read Access

### Goal

Allow the concierge to answer questions about upcoming calendar events and availability.

The Google Calendar integration should run as a containerized tool service or as a clearly separated module inside an existing application container.

### Tasks

* [x] Create a Google Cloud project
* [x] Enable the Google Calendar API
* [x] Configure the OAuth consent screen
* [x] Create OAuth client credentials
* [x] Define the initial read-only OAuth scopes
* [x] Implement the OAuth authorization flow
* [x] Store OAuth credentials outside the Git repository
* [x] Store access and refresh tokens in an ignored or encrypted location
* [x] Implement event-listing functionality
* [x] Implement upcoming-event queries
* [x] Implement free-time and availability checks
* [x] Define Calendar tool permissions
* [x] Add the Calendar tool to the containerized environment
* [x] Add tests using synthetic calendar data
* [x] Document the Google Calendar setup

### Verified Flow

```text
Slack
  |
  v
Slack Gateway
  |
  v
Hermes Agent
  |
  | MCP
  v
Google Calendar MCP
  |
  v
Google Calendar API
```

### Completion Criteria

The end-to-end Calendar read path has been verified with questions such as:

```text
What is my next meeting?

What does my schedule look like tomorrow?

Do I have a two-hour free slot this week?
```

Calendar data must be retrieved from Google Calendar rather than from long-term agent memory.

## Milestone 5: Observability Foundation and Backend Comparison

### Goal

Establish a vendor-neutral observability foundation using OpenTelemetry and compare Phoenix and MLflow against the same agent workloads.

Application services should export traces to a shared OpenTelemetry Collector rather than integrating directly with a specific observability backend.

The Collector will fan out the same trace stream to both Phoenix and MLflow so that the two platforms can be evaluated under equivalent workloads.

### Planned Architecture

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

The initial experiment follows Pattern A:

```text
App
 |
 | OpenTelemetry
 v
OpenTelemetry Collector
 |                 |
 v                 v
Phoenix          MLflow
```

### Principles

* Application instrumentation should remain backend-neutral.
* OpenTelemetry is the common tracing interface.
* Application services should export traces to the OpenTelemetry Collector.
* Phoenix and MLflow should receive equivalent traces whenever practical.
* Backend-specific instrumentation should not be introduced during the initial comparison.
* Sensitive Slack messages, credentials, OAuth tokens, and personal calendar data should not be exported by default.

### Tasks

* [x] Add an OpenTelemetry Collector service
* [x] Add a Phoenix service
* [x] Add an MLflow Tracking Server service
* [x] Add persistent storage where required
* [x] Add health checks for the observability services
* [x] Configure the Collector to receive OTLP traces
* [x] Configure the Collector to export traces to Phoenix
* [x] Configure the Collector to export traces to MLflow
* [x] Document the observability infrastructure setup
* [x] Add OpenTelemetry instrumentation to the Slack Gateway
* [x] Add OpenTelemetry instrumentation to the Google Calendar MCP service
* [x] Trace requests from the Slack Gateway to Hermes Agent
* [x] Inject W3C Trace Context into the Slack Gateway's outgoing Hermes request
* [x] Extract incoming trace context in Hermes Agent and join the same distributed trace
* [x] Record latency and error information
* [x] Redact secrets and personal information
* [x] Verify that the same synthetic workload can be inspected in Phoenix and MLflow
* [ ] Compare trace visualization and debugging workflows
* [ ] Compare search and filtering capabilities
* [ ] Compare latency and error analysis
* [ ] Compare tool-call representation
* [ ] Compare local deployment and resource usage
* [ ] Document the comparison results

### Verified Slack Gateway Trace

The Slack Gateway now exports backend-neutral OpenTelemetry traces to the
shared OpenTelemetry Collector.

The verified trace structure is:

```text
concierge.request
|
+-- hermes.request
|
+-- slack.response
```

The same representative Slack request traces have been verified in both
Phoenix and MLflow.

The current instrumentation records request latency, downstream latency,
span status, and low-cardinality error types.

Slack message bodies, credentials, raw exception messages, conversation
identifiers, channel identifiers, user identifiers, and workspace identifiers
are not added to trace attributes.

Failure-path verification confirmed that sanitized application errors are
reported as `ERROR` spans without exporting raw exception messages.

The Slack Gateway's outgoing HTTP request to Hermes Agent now carries a
standard OpenTelemetry `traceparent` header, injected using the OpenTelemetry
API's global propagator (`opentelemetry.propagate.inject`) while the
`hermes.request` span is active. The injected header's trace ID and parent
span ID match the `hermes.request` span, and existing `Authorization` and
`Content-Type` headers are preserved.

Hermes Agent now extracts this incoming trace context and creates a matching
`SERVER` span for each API server request, so the distributed trace continues
past the HTTP boundary into Hermes Agent:

```text
concierge.request
|
+-- hermes.request
|   |
|   +-- /v1/responses (Hermes Agent)
|
+-- slack.response
```

This is implemented as an OpenTelemetry auto-instrumentation layer added on
top of the unmodified official Hermes Agent image
(`apps/hermes-agent/Dockerfile`), rather than a change to Hermes Agent's own
source — see `docs/observability/hermes-trace-context.md` for the
investigation, the extraction approach, and its verification. Spans for
Hermes-internal processing (LLM calls, tool calls) are not added yet; that is
tracked under Milestone 9. Instrumentation of the Google Calendar MCP service
also remains follow-up work.

### Verified Google Calendar MCP Trace

The Google Calendar MCP service now exports backend-neutral OpenTelemetry
traces to the shared OpenTelemetry Collector, reusing the same
`service.namespace` convention, the same OTLP/gRPC exporter, and the same
Collector endpoint configuration as the Slack Gateway
(`service.name = google-calendar-mcp`).

Unlike the Slack Gateway and Hermes Agent, no application span-creation code
was added. The `mcp` Python SDK the service already depends on ships its own
`OpenTelemetryMiddleware`, enabled by default, which wraps every MCP
`tools/call` request in a span:

```text
tools/call <tool_name>
```

with a bounded, low-cardinality attribute set (`mcp.method.name`,
`gen_ai.tool.name`, `gen_ai.operation.name`, `jsonrpc.request.id`), a
sanitized `error.type = "tool_error"` on failure, and no calendar content,
credentials, or raw exception text — verified both by automated tests and by
a real Docker Compose run against the shared Collector. See
`docs/observability/google-calendar-mcp-telemetry.md` for the full design
notes, the sensitive-data verification, and what remains follow-up work.

Because the same SDK middleware extracts an incoming W3C `traceparent` from
the MCP request using the standard OpenTelemetry API, a Google Calendar MCP
tool span joins an incoming distributed trace as a child span whenever the
caller supplies one — with no Google Calendar MCP-side code dedicated to
this beyond the existing SDK configuration, and no change to Hermes Agent.
This was confirmed live with the real, already-running Hermes Agent
container and a real Slack message: Hermes Agent's own `tools/call
list_events` / `tools/call list_upcoming_events` MCP client spans and Google
Calendar MCP's corresponding server spans shared a trace ID and were
correctly parented, for both a successful and a (separately, credential-
related) failing tool call.

That same live check also showed this Hermes Agent ↔ Google Calendar MCP
trace is not currently joined to the Slack-originated
`concierge.request` → `hermes.request` trace — Hermes Agent's own internal
code does not carry its `/v1/responses` server span's context into its
outgoing MCP client calls. That is a gap inside Hermes Agent itself (the
unmodified vendor image), out of scope here; see
`docs/observability/google-calendar-mcp-telemetry.md` for the full
evidence and what a follow-up would require.

This matches a confirmed, tracked upstream issue
([NousResearch/hermes-agent#60177](https://github.com/NousResearch/hermes-agent/issues/60177))
with an open, unmerged fix
([NousResearch/hermes-agent#78965](https://github.com/NousResearch/hermes-agent/pull/78965)).
The planned approach for this repository — recorded in
`docs/observability/hermes-trace-context.md` — is to wait for that (or a
successor) to merge upstream, then bump the pinned tag in
`apps/hermes-agent/Dockerfile` and enable its `mcp.trace_propagation: true`
config, rather than patching Hermes Agent source directly.

### Verified Collector-Side Redaction

Application instrumentation (Slack Gateway, Google Calendar MCP) already
avoids placing sensitive data in span attributes by design, verified by the
tests referenced above. The shared OpenTelemetry Collector now adds a second,
defense-in-depth layer: a `redaction` processor
(`infra/observability/otel-collector.yaml`) that masks a fixed list of
known-sensitive span attribute keys — credentials, tokens, Authorization
headers, Slack/Calendar identifiers and content, raw exception text — before
*any* exporter (`debug` included, not just Phoenix and MLflow) sees them.
Every other attribute, including ones not explicitly known about in advance,
passes through unchanged (`allow_all_keys: true`), so this cannot silently
drop future legitimate telemetry the way an exhaustive allowlist would.

This protects against an application-side regression or an auto-instrumented
dependency (e.g. Hermes Agent's own OpenTelemetry auto-instrumentation, which
exports to this same Collector) attaching one of these keys by mistake. It
does not attempt general-purpose PII detection over arbitrary free-text
attribute values — see `docs/observability/collector-redaction.md` for the
full design rationale, the processor evaluated and rejected, the complete
attribute list and its reasoning, and the verification evidence (an
automated test suite at `infra/observability/tests/test_redaction.py` using
only synthetic placeholder values, plus a real Docker Compose run confirming
Phoenix and MLflow both still receive the (now-redacted) trace without
export errors).

### Initial Trace Scope

```text
concierge.request
|
+-- slack.receive
|
+-- hermes.request
|
+-- calendar.mcp
|   |
|   +-- google-calendar.api
|
+-- slack.response
```

This was the originally planned span hierarchy. The Google Calendar MCP
span actually implemented is named `tools/call <tool_name>` (from the `mcp`
SDK's own instrumentation, see "Verified Google Calendar MCP Trace" above)
rather than `calendar.mcp`, and the Google Calendar API HTTP request itself
does not yet have its own child span (`google-calendar.api`) — that remains
follow-up work.

The exact span hierarchy may evolve as Hermes Agent instrumentation and distributed trace propagation are explored.

### Comparison Criteria

Phoenix and MLflow should initially be compared on:

* Trace visualization
* Distributed trace navigation
* Search and filtering
* Error investigation
* Latency analysis
* Tool-call representation
* Agent-oriented metadata
* Local deployment complexity
* Resource usage
* Evaluation capabilities
* Experiment management
* OpenTelemetry interoperability

### Completion Criteria

This milestone is complete when:

1. The OpenTelemetry Collector runs as the common trace ingestion point for instrumented application services.
2. Phoenix receives traces from the Collector.
3. MLflow receives traces from the Collector.
4. Representative Slack and Calendar workloads can be inspected in both observability backends.
5. Calendar-related requests expose enough spans to identify latency and failures.
6. Sensitive credentials and personal data are not exported unintentionally.
7. Phoenix and MLflow have been compared using equivalent representative workloads.
8. The comparison results and the next observability decision are documented.

## Milestone 6: Human Approval for Calendar Writes

### Goal

Allow the concierge to propose calendar changes without executing them until the user explicitly approves.

### Tasks

* [x] Define a proposed-action schema
* [x] Define approval states
* [ ] Create the Approval Service
* [ ] Add a Dockerfile for the Approval Service
* [ ] Add the Approval Service to `docker-compose.yml`
* [ ] Implement calendar event proposals
* [ ] Display exact proposed event details in Slack
* [ ] Add Slack approval and rejection controls
* [ ] Bind each approval to the requesting Slack user
* [ ] Bind each approval to the exact action parameters
* [ ] Add approval expiration
* [ ] Prevent modified actions from reusing an old approval
* [ ] Implement calendar event creation
* [ ] Implement calendar event updates
* [ ] Implement calendar event deletion
* [ ] Record the result of approved actions
* [ ] Add approval workflow tests
* [ ] Document the approval security model

### Proposed-Action Schema and Approval States

The domain foundation for this milestone is now defined in
`packages/approvals`: a `ProposedAction` schema covering
`calendar.create_event`, `calendar.update_event`, and
`calendar.delete_event`, and an `ApprovalState` lifecycle
(`pending` → `approved` / `rejected` / `expired`, with `approved`,
`rejected`, and `expired` all terminal).

`ProposedAction` is an immutable, validated record of exactly what a
calendar write would do (action type, target event ID where applicable,
and a flat string parameter set); because it is a frozen dataclass, exact
structural equality is a future Approval Service's basis for detecting
whether an action changed after approval. `Approval` binds a
`ProposedAction` to the requesting actor and originating conversation and
enforces its state transitions through an exhaustive, tested transition
table — an approval can never move out of a terminal state, so a rejected
or expired proposal cannot be turned into an approved one.

This is schema and state-machine only: no Approval Service, no
persistence, no Slack controls, no approval expiration scheduling, and no
calendar write execution exist yet. See
`docs/approval/domain-model.md` for the full design, the security
boundary this establishes (and what it deliberately does not cover yet),
and `packages/approvals/tests` for the automated verification (128 tests,
100% line coverage).

### Planned Flow

```text
User requests a calendar change
  |
  v
Calendar Agent prepares a proposal
  |
  v
Slack displays the exact proposal
  |
  v
User approves or rejects
  |
  v
Approval Service validates the action
  |
  v
Calendar tool executes the approved action
```

### Completion Criteria

This milestone is complete when the system can:

1. Prepare a calendar event proposal.
2. Display the exact event details in Slack.
3. Wait for explicit user approval.
4. Reject expired or modified approvals.
5. Create the event only after valid approval.
6. Report the final result back to Slack.

## Milestone 7: Containerized Concierge Orchestrator

### Goal

Introduce a dedicated Orchestrator container between the Slack Gateway and specialized agents.

The Orchestrator will allow new agents and frameworks to be added without coupling the Slack Gateway directly to Hermes Agent.

### Tasks

* [x] Define the common agent request schema
* [x] Define the common agent response schema
* [ ] Create the `services/orchestrator` application
* [x] Add a Dockerfile for the Orchestrator
* [x] Add the Orchestrator service to `docker-compose.yml`
* [ ] Move agent-selection responsibility out of the Slack Gateway
* [ ] Implement agent registration
* [ ] Implement request classification
* [ ] Implement agent selection
* [x] Forward requests to Hermes Agent
* [ ] Support custom Agent implementations
* [ ] Support task delegation
* [ ] Support multiple-agent workflows
* [ ] Combine results from multiple agents
* [x] Preserve trace and conversation identifiers
* [ ] Add routing tests
* [ ] Document the Agent contract

### Planned Flow

```text
Slack
  |
  v
Slack Gateway
  |
  v
Concierge Orchestrator
  |
  +--> Hermes Agent
  |
  +--> Calendar Agent
  |
  +--> Research Agent
  |
  +--> Custom Agent
```

### Initial Agents

* Concierge Agent
* Calendar Agent
* Research Agent
* Coding Agent

### Completion Criteria

This milestone is complete when:

1. The Slack Gateway sends all normalized requests to the Orchestrator.
2. Different requests are delegated to the appropriate specialized agent.
3. The user does not need to select an agent manually.
4. A custom Agent can be added without changing the Slack integration.
5. Hermes Agent is treated as one Agent implementation rather than the entire system.

## Milestone 8: Containerized Shared Memory

### Goal

Allow agents to share selected user, project, and task memories while preserving access boundaries.

### Planned Services

```text
memory-service
postgres
```

`pgvector` may be enabled inside PostgreSQL when semantic retrieval becomes necessary.

### Tasks

* [ ] Define the memory record schema
* [ ] Define memory source types
* [ ] Define memory scopes
* [ ] Define memory visibility rules
* [ ] Add the PostgreSQL service to `docker-compose.yml`
* [ ] Create a persistent PostgreSQL Docker volume
* [ ] Add database health checks
* [ ] Add pgvector if semantic retrieval is required
* [ ] Create the `services/memory-service` application
* [ ] Add a Dockerfile for the Memory Service
* [ ] Add the Memory Service to `docker-compose.yml`
* [ ] Implement the Memory Service API
* [ ] Implement global user memory
* [ ] Implement project memory
* [ ] Implement task memory
* [ ] Implement Agent-private memory
* [ ] Support memory candidates
* [ ] Prevent unrestricted Agent memory writes
* [ ] Add confidence metadata
* [ ] Add expiration support
* [ ] Add memory deletion support
* [ ] Add memory retrieval evaluations
* [ ] Document the memory access model

### Planned Flow

```text
Agent
  |
  v
Memory Service container
  |
  v
PostgreSQL container
  |
  v
Optional pgvector search
```

### Completion Criteria

This milestone is complete when:

* Agents can retrieve memories within their allowed scopes.
* Agent-private memories remain private.
* Project memories can be shared with approved Agents.
* Agent inferences are distinguishable from user-provided facts.
* Memories can expire or be deleted.
* Current external data is still retrieved from its authoritative source.

## Milestone 9: Extended Observability

### Goal

Extend the observability foundation introduced in Milestone 5 across the Orchestrator, Agents, Memory Service, Approval Service, and future tool integrations.

The backend strategy for this milestone should follow the results of the Phoenix and MLflow comparison performed in Milestone 5.

### Tasks

* [ ] Add OpenTelemetry instrumentation to the Orchestrator
* [ ] Add OpenTelemetry instrumentation to Agent calls
* [ ] Add OpenTelemetry instrumentation to the Memory Service
* [ ] Add OpenTelemetry instrumentation to the Approval Service
* [ ] Add OpenTelemetry instrumentation to additional tool calls
* [ ] Create one distributed trace for each Slack request
* [ ] Add spans for model calls
* [ ] Add spans for agent routing
* [ ] Add spans for memory retrieval
* [ ] Add spans for approval waits
* [ ] Add spans for external API requests
* [ ] Record latency and error information
* [ ] Propagate trace identifiers between containers
* [ ] Enforce telemetry redaction rules
* [ ] Decide whether to retain both Phoenix and MLflow or standardize on one backend
* [ ] Document the tracing model

### Planned Flow

```text
Slack request
  |
  v
Application containers
  |
  | OpenTelemetry
  v
OpenTelemetry Collector
  |
  v
Selected observability backend or backends
```

### Example Trace

```text
Slack event received
  -> Request normalized
  -> Agent selected
  -> Memory retrieved
  -> Ollama model call
  -> Tool called
  -> External API requested
  -> Approval requested if required
  -> Agent response generated
  -> Slack response delivered
```

### Completion Criteria

This milestone is complete when a Slack request can be followed from receipt to final response through the observability stack selected after Milestone 5.

The trace should show:

* Which Agent was selected
* Which model was used
* Which memories were retrieved
* Which tools were called
* How long each step took
* Whether an approval was required
* Where an error occurred

Sensitive message content and credentials must not be exposed in exported traces.

## Milestone 10: Evaluation and Reliability

### Goal

Measure whether the system selects the correct Agents, retrieves useful memories, and performs safe tool actions.

### Evaluation Areas

* Agent-routing accuracy
* Tool-selection accuracy
* Memory relevance
* Response quality
* Approval enforcement
* Hallucination detection
* Tool failure recovery
* Prompt injection resistance
* Container failure recovery
* External API timeout handling

### Tasks

* [ ] Create synthetic evaluation datasets
* [ ] Add routing evaluations
* [ ] Add memory retrieval evaluations
* [ ] Add tool-selection evaluations
* [ ] Add approval safety tests
* [ ] Add prompt injection tests
* [ ] Add external API failure tests
* [ ] Add container restart tests
* [ ] Add regression tests
* [ ] Add Docker image build checks
* [ ] Run tests and evaluations in GitHub Actions
* [ ] Document known limitations

### Completion Criteria

This milestone is complete when automated evaluations can detect regressions in:

* Agent selection
* Memory retrieval
* Tool use
* Approval enforcement
* Response quality
* Service reliability

## Milestone 11: Developer Experience

### Goal

Make the complete local environment easy for another developer to understand and run.

### Tasks

* [ ] Add a setup script
* [ ] Add common commands to a `Makefile`
* [ ] Document CPU-only setup
* [ ] Document NVIDIA GPU setup
* [ ] Document environment variables
* [ ] Document Docker volumes
* [ ] Document service ports
* [ ] Document health checks
* [ ] Add example configuration files
* [ ] Add troubleshooting documentation
* [ ] Add architecture decision records
* [ ] Add contribution guidelines
* [ ] Add a security policy

### Target Commands

The complete environment should eventually support commands similar to:

```bash
docker compose up -d
```

```bash
docker compose ps
```

```bash
docker compose logs -f
```

```bash
docker compose down
```

Optional services may later be started through Compose profiles:

```bash
docker compose --profile observability up -d
```

## Future Ideas

The following features are outside the initial implementation scope but may be explored later:

* Gmail integration
* GitHub issue and pull request management
* Personal task management
* Scheduled daily briefings
* Meeting preparation summaries
* Travel planning
* Voice input and output
* Additional local model providers
* Additional Agent frameworks
* A2A-compatible Agent communication
* MCP-compatible tools
* Remote access through a secure private network
* Mobile notifications
* Local document search
* Multiple user profiles

## Development Guidelines

Each milestone should:

1. Be implemented on a dedicated Git branch.
2. Produce an independently testable result.
3. Add one service or capability at a time.
4. Include documentation.
5. Include automated tests where practical.
6. Avoid committing credentials or personal data.
7. Use Docker volumes for persistent service data.
8. Define health checks where practical.
9. Keep service responsibilities separate.
10. Update this roadmap when the implementation status changes.

Some of this work is produced by delegating implementation to autonomous
Coding Agents, including running independent tasks in parallel. Those
delegation experiments test the delegation workflow itself (agent
capability, human review burden) rather than tracking product milestones,
so they are logged separately under `docs/delegated-development/` — see
[`docs/delegated-development/level3-experiment-2.md`](delegated-development/level3-experiment-2.md)
for the latest.

## Branch Naming

Suggested branch names include:

```text
infra/ollama-compose
infra/hermes-container
feat/slack-gateway
feat/calendar-read-tool
infra/observability-lab
feat/calendar-approval
feat/agent-orchestrator
feat/shared-memory
observability/extended-tracing
test/agent-evaluations
docs/developer-setup
```

## Commit Message Examples

```text
infra: add containerized Ollama service
infra: add Hermes Agent container
feat: implement Slack Socket Mode gateway
feat: add Google Calendar read tool
feat: add calendar approval workflow
feat: implement agent routing
feat: add shared memory service
observability: add OpenTelemetry trace fan-out
test: add agent routing evaluations
docs: document local container setup
```
