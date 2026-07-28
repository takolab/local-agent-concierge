# Architecture

This document describes the planned architecture of Local Agent Concierge.

The project is designed as a privacy-focused personal AI system that can be accessed from Slack, use locally hosted language models through Ollama, delegate work to specialized agents, and share selected memories through a dedicated memory service.

## Architecture Principles

The system follows these principles:

1. **Local-first inference**  
   Language model inference should run locally through Ollama whenever practical.

2. **One conversational entry point**  
   The user interacts with a single concierge through Slack, even when multiple specialized agents are involved internally.

3. **Replaceable agents**  
   Hermes Agent, custom Python agents, and agents implemented with other frameworks should be replaceable behind a common contract.

4. **Explicit tool access**  
   Agents do not access external services directly unless they have been granted the required tool permission.

5. **Human approval for sensitive actions**  
   Actions such as creating, updating, or deleting calendar events require user approval before execution.

6. **Scoped memory**  
   Agents share only the memories they are allowed to access. Conversation history, long-term memory, task state, and external service data are treated separately.

7. **Observable agent activity**  
   Agent routing, model calls, tool calls, latency, and failures should be traceable through OpenTelemetry and Phoenix.

## Initial Milestone

The first milestone establishes a minimal end-to-end communication flow.

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
Ollama
  |
  v
Slack response
```

This milestone proves that:

- Slack messages can reach the local environment.
- Hermes Agent can receive and process a user request.
- Hermes Agent can use an Ollama-hosted model.
- The final response can be returned to Slack.

Google Calendar access, shared memory, multi-agent routing, and approval workflows will be added after this path is stable.

## Target Architecture

```text
+---------------------------+
| Slack                     |
|                           |
| Direct messages           |
| Threads                   |
| Approval buttons          |
+-------------+-------------+
              |
              v
+---------------------------+
| Slack Gateway             |
|                           |
| Event ingestion           |
| Slack authentication      |
| Message normalization     |
| Response delivery         |
+-------------+-------------+
              |
              v
+---------------------------+
| Concierge Orchestrator    |
|                           |
| Request classification    |
| Agent selection           |
| Task delegation           |
| Permission checks         |
| Result aggregation        |
| Approval coordination     |
+------+------+-------------+
       |      |      |
       v      v      v
+---------+ +---------+ +----------------+
| Hermes  | |Calendar | | Custom Agent   |
| Agent   | | Agent   | | Python / other |
+----+----+ +----+----+ +-------+--------+
     |           |              |
     +-----------+--------------+
                 |
        +--------+---------+
        |                  |
        v                  v
+------------------+  +------------------+
| Shared Memory    |  | Tool Services    |
| Service          |  |                  |
|                  |  | Google Calendar  |
| PostgreSQL       |  | GitHub           |
| pgvector         |  | Other APIs       |
+------------------+  +------------------+
                 |
                 v
+----------------------------------------+
| Local Infrastructure                   |
|                                        |
| Ollama                                 |
| Phoenix                                |
| OpenTelemetry                          |
| Docker Compose                         |
+----------------------------------------+
```

## Component Responsibilities

### Slack Gateway

The Slack Gateway is the only component that communicates directly with Slack.

Responsibilities:

- Receive Slack events and direct messages.
- Validate Slack requests or maintain a Socket Mode connection.
- Convert Slack-specific events into an internal request format.
- Associate each request with a user, channel, thread, and conversation.
- Render agent responses back into Slack messages.
- Present approval buttons for sensitive actions.
- Prevent duplicate processing when Slack retries an event.

The Slack Gateway should not contain agent reasoning or business-specific tool logic.

### Concierge Orchestrator

The Concierge Orchestrator is the coordination layer between Slack and the agents.

Responsibilities:

- Interpret the user's requested outcome.
- Select the most appropriate specialized agent.
- Delegate subtasks to one or more agents.
- Provide agents with only the required memory scopes and permissions.
- Combine agent outputs into one user-facing response.
- Pause execution when human approval is required.
- Continue an approved task without repeating completed work.

The orchestrator should depend on common agent contracts rather than framework-specific implementations.

### Agents

Each agent has a focused responsibility.

Planned agents include:

- **Concierge Agent** — general conversation, request clarification, and task coordination.
- **Calendar Agent** — calendar queries, availability checks, and event proposals.
- **Research Agent** — information gathering, comparison, and summarization.
- **Coding Agent** — development, repository, infrastructure, and debugging tasks.
- **Travel Agent** — itinerary planning and travel-related recommendations.
- **Custom Agents** — user-defined agents added later.

An agent may be implemented using Hermes Agent, a custom service, or another agent framework. Every implementation should expose the same logical request and response contract.

## Agent Contract

A normalized agent request may contain:

```json
{
  "task_id": "task-123",
  "user_id": "user-123",
  "conversation_id": "slack-thread-456",
  "instruction": "Find a two-hour study slot next week",
  "memory_scopes": [
    "user:user-123",
    "project:ml-systems"
  ],
  "permissions": [
    "calendar.read"
  ],
  "trace_id": "trace-789"
}
```

A normalized agent response may contain:

```json
{
  "status": "needs_approval",
  "summary": "Tuesday from 19:00 to 21:00 is available.",
  "proposed_actions": [
    {
      "type": "calendar.create_event",
      "title": "Ollama study",
      "start": "2026-08-04T19:00:00+01:00",
      "end": "2026-08-04T21:00:00+01:00"
    }
  ],
  "memory_candidates": []
}
```

The exact schema will evolve, but framework-specific objects should not cross the agent boundary.

## Tool Architecture

Tools provide controlled access to external systems.

Examples:

- Google Calendar
- GitHub
- Email
- Task management systems
- Web search

Tool access should be separated from agent reasoning so that permissions, retries, auditing, and approval requirements can be enforced consistently.

Conceptually:

```text
Agent -> Tool interface -> External service
```

Tools may initially be exposed through ordinary HTTP APIs. MCP-compatible interfaces can be added when they provide a clear interoperability benefit.

## Google Calendar Flow

Read operations can run automatically when the agent has `calendar.read` permission.

```text
Slack request
  -> Concierge Orchestrator
  -> Calendar Agent
  -> Calendar read tool
  -> Google Calendar API
  -> Calendar Agent
  -> Slack response
```

Write operations require explicit approval.

```text
User asks to create an event
  -> Calendar Agent prepares an event proposal
  -> Slack Gateway displays the proposal
  -> User approves the proposal
  -> Approval Service validates the approval
  -> Calendar write tool creates the event
  -> Slack Gateway reports the result
```

The source of truth for current event data remains Google Calendar. Calendar events should not be treated as permanent long-term memories.

## Memory Architecture

Memory is divided into separate categories.

### Conversation Memory

Short-lived context associated with the current Slack thread or conversation.

Examples:

- Recent messages
- Current task state
- Pending clarification
- Pending approval

### Global User Memory

Stable user information that may be useful across multiple agents.

Examples:

- Time zone
- Communication preferences
- Approval preferences
- Long-term goals

### Project Memory

Information associated with a specific project or topic.

Examples:

- Architecture decisions
- Repository conventions
- Current milestones
- Previous technical findings

### Agent-Private Memory

Information used only by a specific agent.

Examples:

- Agent-specific operational notes
- Tool usage patterns
- Domain-specific working context

### Task Memory and Artifacts

Temporary shared state produced while multiple agents work on the same request.

Examples:

- Research findings
- Proposed calendar slots
- Draft plans
- Intermediate outputs

## Memory Access Model

A memory record should include metadata similar to:

```json
{
  "id": "memory-123",
  "user_id": "user-123",
  "scope": "project:local-agent-concierge",
  "content": "Calendar write actions require explicit approval.",
  "source_agent": "concierge",
  "source_type": "user_statement",
  "visibility": [
    "concierge",
    "calendar"
  ],
  "confidence": 1.0,
  "created_at": "2026-07-28T12:00:00+01:00",
  "expires_at": null
}
```

Important metadata includes:

- `scope`
- `source_agent`
- `source_type`
- `visibility`
- `confidence`
- `expires_at`

Agents should propose memory candidates rather than freely writing every inference into long-term memory. The Memory Service decides whether to create, merge, reject, expire, or request confirmation for a memory.

## Source of Truth Boundaries

The system must distinguish memory from authoritative external data.

```text
Google Calendar -> calendar events and availability
GitHub          -> repositories, issues, and pull requests
Slack           -> original messages and thread history
Memory Service  -> preferences, summaries, background, and learned context
```

When current external information is needed, the system should query the relevant source instead of relying on an old memory.

## Human Approval

Sensitive actions must be represented as proposals before execution.

Examples requiring approval:

- Creating a calendar event
- Updating a calendar event
- Deleting a calendar event
- Sending an invitation
- Sending an email
- Modifying a GitHub repository

An approval should be bound to:

- The requesting user
- The exact proposed action
- The action parameters
- An expiration time
- The originating task

Changing the proposed action after approval should invalidate that approval.

## Security Boundaries

Secrets and personal data must never be committed to the public repository.

Sensitive data includes:

- Slack tokens and signing secrets
- Google OAuth client secrets
- OAuth access and refresh tokens
- Calendar event data
- Slack message history
- Memory databases
- Phoenix traces containing personal information
- Private keys

Public configuration should use placeholders in `.env.example`. Actual values should be stored in an ignored local `.env` file or a dedicated secret manager.

Agents should receive the minimum permissions required for each task. For example, a research agent should not automatically receive calendar write permission.

## Observability

A single Slack request should be represented as one distributed trace.

Example trace structure:

```text
Slack event received
  -> Request normalized
  -> Agent selected
  -> Memory retrieved
  -> Ollama model call
  -> Tool call
  -> External API request
  -> Agent response generated
  -> Slack response delivered
```

Useful telemetry includes:

- Agent selected
- Model name
- Prompt and completion token counts when available
- Model latency
- Tool latency
- Number of agent iterations
- Approval wait time
- Error type
- Final task status

Personal message content and credentials must be redacted before traces are exported or shared.

## Deployment Model

The initial development environment will use Docker Compose on a local machine or WSL2 environment.

Planned services include:

```text
slack-gateway
orchestrator
hermes-agent
ollama
memory-service
postgres
phoenix
```

Not every service needs to exist in the first milestone. Components should be introduced incrementally.

## Repository Mapping

The planned repository structure maps to the architecture as follows:

```text
apps/slack-gateway/          Slack integration
services/orchestrator/       Agent routing and coordination
services/memory-service/     Shared memory API
services/approval-service/   Human approval validation
agents/                      Agent definitions and implementations
tools/                       External service integrations
packages/agent-contracts/    Shared request and response schemas
packages/observability/      OpenTelemetry helpers
infra/                       Docker and service configuration
evals/                       Agent and memory evaluations
docs/                        Architecture and design decisions
```

## Implementation Phases

### Phase 1: Local Conversation Path

```text
Slack -> Hermes Agent -> Ollama -> Slack
```

### Phase 2: Calendar Read Access

```text
Concierge -> Calendar tool -> Google Calendar
```

### Phase 3: Human Approval

```text
Event proposal -> Slack approval -> Calendar write
```

### Phase 4: Multi-Agent Routing

```text
Slack -> Orchestrator -> Specialized agent
```

### Phase 5: Shared Memory

```text
Agent -> Memory Service -> PostgreSQL and pgvector
```

### Phase 6: Observability and Evaluation

```text
OpenTelemetry -> Phoenix
```

## Non-Goals for the Initial Version

The initial version will not attempt to:

- Build a fully autonomous assistant with unrestricted permissions.
- Allow agents to perform sensitive writes without approval.
- Replace authoritative external services with remembered copies of their data.
- Support every agent framework from the beginning.
- Deploy a public multi-user SaaS platform.
- Store all conversations permanently.

## Open Architecture Decisions

The following decisions will be documented as the implementation progresses:

- Programming language and framework for the Slack Gateway
- Programming language and framework for the Orchestrator
- Initial Hermes deployment method
- Agent transport protocol
- Memory embedding model
- PostgreSQL and pgvector schema
- Approval token design
- OpenTelemetry redaction strategy
- Authentication between internal services
