# Roadmap

This roadmap describes the planned development phases for Local Agent Concierge.

The project will be developed incrementally. Each milestone should produce a small, testable improvement before additional services and agents are introduced.

## Current Status

* [x] Create the public GitHub repository
* [x] Add the initial README
* [x] Add the MIT License
* [x] Define the initial system architecture
* [ ] Define the containerized development environment
* [ ] Run Ollama through Docker Compose
* [ ] Connect Hermes Agent to Ollama
* [ ] Connect the system to Slack
* [ ] Add Google Calendar integration
* [ ] Add multi-agent orchestration
* [ ] Add shared memory
* [ ] Add observability and evaluations

## Containerization Strategy

All application services should run as separate Docker containers and be managed through Docker Compose.

Planned containerized services include:

* Ollama
* Hermes Agent
* Slack Gateway
* Concierge Orchestrator
* Memory Service
* Approval Service
* PostgreSQL
* Phoenix

Each container should have a focused responsibility.

Services should communicate through the internal Docker Compose network using service names as hostnames.

For example, Hermes Agent should connect to Ollama using:

```text
http://ollama:11434
```

It should not use:

```text
http://localhost:11434
```

Inside a container, `localhost` refers to that container itself rather than another Compose service.

Services will be introduced incrementally. The first milestone starts with Ollama, and later milestones add Hermes Agent, the Slack Gateway, the Orchestrator, shared memory, and observability to the same Docker Compose application.

Persistent data should be stored in Docker volumes or ignored local directories instead of disposable container filesystems.

The host environment should require as few dependencies as practical. Ideally, a developer should only need:

* Git
* Docker Engine
* Docker Compose
* NVIDIA Container Toolkit when GPU acceleration is used

## Planned Container Architecture

```text
Docker Compose
├── ollama
├── hermes-agent
├── slack-gateway
├── orchestrator
├── approval-service
├── memory-service
├── postgres
└── phoenix
```

The initial implementation will not start all of these containers at once. Each service will be added after its dependencies have been tested.

## Milestone 1: Containerized Local Foundation

### Goal

Create the initial Docker Compose environment and run Ollama as the first containerized service.

This milestone establishes the shared containerization conventions that will later be used by Hermes Agent, the Slack Gateway, the Orchestrator, the Memory Service, PostgreSQL, and Phoenix.

### Tasks

* [ ] Define the Ollama service in `docker-compose.yml`
* [ ] Define a shared Docker network
* [ ] Create a persistent Docker volume for Ollama models
* [ ] Expose the Ollama API to the local host
* [ ] Add an Ollama health check
* [ ] Validate the Docker Compose configuration
* [ ] Start the Ollama container
* [ ] Confirm that the container is healthy
* [ ] Download a small test model
* [ ] Send a test prompt to the model
* [ ] Confirm whether GPU acceleration is available
* [ ] Document the local Docker Compose commands

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

This milestone is complete when:

1. `docker compose up -d` starts Ollama successfully.
2. `docker compose ps` reports the service as healthy.
3. Ollama retains downloaded models after the container is recreated.
4. The Ollama API returns a model-generated response.

Example request:

```bash
curl http://localhost:11434/api/generate \
  -d '{
    "model": "<model-name>",
    "prompt": "Explain what a local LLM is in one sentence.",
    "stream": false
  }'
```

## Milestone 2: Containerized Hermes Agent Integration

### Goal

Run Hermes Agent as a Docker container and connect it to the Ollama container through the internal Docker network.

### Tasks

* [ ] Decide which Hermes Agent Docker image or build method to use
* [ ] Add the Hermes Agent service to `docker-compose.yml`
* [ ] Create a persistent volume for Hermes configuration and state
* [ ] Configure the Ollama endpoint as `http://ollama:11434`
* [ ] Configure the initial Ollama model
* [ ] Add Hermes environment variables
* [ ] Add a Hermes health check if supported
* [ ] Configure Hermes to wait for Ollama readiness
* [ ] Send a terminal-based request through Hermes Agent
* [ ] Confirm that Hermes receives a response from Ollama
* [ ] Confirm that Hermes state survives container recreation
* [ ] Document the Hermes container setup

### Planned Flow

```text
Host terminal
  |
  v
Hermes Agent container
  |
  v
Ollama container
  |
  v
Local LLM response
```

### Completion Criteria

This milestone is complete when:

1. Hermes Agent runs as a Docker container.
2. Hermes connects to Ollama using the Compose service name.
3. A request sent through Hermes returns an Ollama-generated response.
4. No Hermes-specific software needs to be installed directly on the host.

## Milestone 3: Containerized Slack Gateway

### Goal

Run the Slack Gateway as a Docker container and allow Slack messages to reach Hermes Agent and Ollama.

### Tasks

* [ ] Create a Slack application
* [ ] Enable Slack Socket Mode
* [ ] Configure the required Slack bot scopes
* [ ] Configure the required Slack event subscriptions
* [ ] Create the `apps/slack-gateway` application
* [ ] Add a Dockerfile for the Slack Gateway
* [ ] Add the Slack Gateway service to `docker-compose.yml`
* [ ] Store Slack credentials in the local `.env` file
* [ ] Read Slack credentials through environment variables
* [ ] Establish the Socket Mode connection
* [ ] Receive a Slack direct message
* [ ] Normalize the Slack message into an internal request
* [ ] Forward the request to Hermes Agent
* [ ] Return the generated response to Slack
* [ ] Preserve Slack thread information
* [ ] Prevent duplicate Slack event processing
* [ ] Add structured application logging
* [ ] Document the Slack setup process

### Planned Flow

```text
Slack
  |
  | Socket Mode
  v
Slack Gateway container
  |
  v
Hermes Agent container
  |
  v
Ollama container
  |
  v
Slack response
```

### Completion Criteria

This milestone is complete when:

1. A direct message can be sent to the Slack application.
2. The Slack Gateway receives the message.
3. Hermes Agent processes the request.
4. Ollama generates the response.
5. The response appears in the correct Slack conversation or thread.
6. The Slack Gateway does not need to expose a public HTTP endpoint.

## Milestone 4: Google Calendar Read Access

### Goal

Allow the concierge to answer questions about upcoming calendar events and availability.

The Google Calendar integration should run as a containerized tool service or as a clearly separated module inside an existing application container.

### Tasks

* [ ] Create a Google Cloud project
* [ ] Enable the Google Calendar API
* [ ] Configure the OAuth consent screen
* [ ] Create OAuth client credentials
* [ ] Define the initial read-only OAuth scopes
* [ ] Implement the OAuth authorization flow
* [ ] Store OAuth credentials outside the Git repository
* [ ] Store access and refresh tokens in an ignored or encrypted location
* [ ] Implement event-listing functionality
* [ ] Implement upcoming-event queries
* [ ] Implement free-time and availability checks
* [ ] Define Calendar tool permissions
* [ ] Add the Calendar tool to the containerized environment
* [ ] Add tests using synthetic calendar data
* [ ] Document the Google Calendar setup

### Planned Flow

```text
Slack
  |
  v
Slack Gateway
  |
  v
Hermes Agent
  |
  v
Google Calendar tool
  |
  v
Google Calendar API
```

### Completion Criteria

This milestone is complete when the concierge can answer questions such as:

```text
What is my next meeting?

What does my schedule look like tomorrow?

Do I have a two-hour free slot this week?
```

Calendar data must be retrieved from Google Calendar rather than from long-term agent memory.

## Milestone 5: Human Approval for Calendar Writes

### Goal

Allow the concierge to propose calendar changes without executing them until the user explicitly approves.

### Tasks

* [ ] Define a proposed-action schema
* [ ] Define approval states
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

## Milestone 6: Containerized Concierge Orchestrator

### Goal

Introduce a dedicated Orchestrator container between the Slack Gateway and specialized agents.

The Orchestrator will allow new agents and frameworks to be added without coupling the Slack Gateway directly to Hermes Agent.

### Tasks

* [ ] Define the common agent request schema
* [ ] Define the common agent response schema
* [ ] Create the `services/orchestrator` application
* [ ] Add a Dockerfile for the Orchestrator
* [ ] Add the Orchestrator service to `docker-compose.yml`
* [ ] Move agent-selection responsibility out of the Slack Gateway
* [ ] Implement agent registration
* [ ] Implement request classification
* [ ] Implement agent selection
* [ ] Forward requests to Hermes Agent
* [ ] Support custom Agent implementations
* [ ] Support task delegation
* [ ] Support multiple-agent workflows
* [ ] Combine results from multiple agents
* [ ] Preserve trace and conversation identifiers
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

## Milestone 7: Containerized Shared Memory

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

## Milestone 8: Containerized Observability

### Goal

Trace model calls, agent decisions, memory retrieval, tool usage, and external API requests.

Phoenix should run as a Docker container and receive OpenTelemetry data from the other services.

### Tasks

* [ ] Add the Phoenix service to `docker-compose.yml`
* [ ] Create persistent Phoenix storage if required
* [ ] Add Phoenix health checks
* [ ] Add OpenTelemetry instrumentation to the Slack Gateway
* [ ] Add OpenTelemetry instrumentation to the Orchestrator
* [ ] Add OpenTelemetry instrumentation to Agent calls
* [ ] Add OpenTelemetry instrumentation to the Memory Service
* [ ] Add OpenTelemetry instrumentation to tool calls
* [ ] Create one trace for each Slack request
* [ ] Add spans for model calls
* [ ] Add spans for agent routing
* [ ] Add spans for memory retrieval
* [ ] Add spans for approval waits
* [ ] Add spans for external API requests
* [ ] Record latency and error information
* [ ] Propagate trace identifiers between containers
* [ ] Redact secrets and personal information
* [ ] Consider using a Docker Compose observability profile
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
Phoenix container
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
  -> Agent response generated
  -> Slack response delivered
```

### Completion Criteria

This milestone is complete when a Slack request can be followed from receipt to final response in Phoenix.

The trace should show:

* Which Agent was selected
* Which model was used
* Which memories were retrieved
* Which tools were called
* How long each step took
* Whether an approval was required
* Where an error occurred

Sensitive message content and credentials must not be exposed in exported traces.

## Milestone 9: Evaluation and Reliability

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

## Milestone 10: Developer Experience

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

## Branch Naming

Suggested branch names include:

```text
infra/ollama-compose
infra/hermes-container
feat/slack-gateway
feat/calendar-read-tool
feat/calendar-approval
feat/agent-orchestrator
feat/shared-memory
feat/phoenix-observability
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
observability: add Phoenix tracing
test: add agent routing evaluations
docs: document local container setup
```
