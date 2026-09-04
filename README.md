# Local Agent Concierge

A privacy-focused personal AI concierge powered by local LLMs, multiple specialized agents, shared memory, and Slack.

## Vision

Local Agent Concierge provides a single AI concierge accessible through Slack.

The concierge delegates tasks to specialized agents, such as:

* Calendar Agent
* Research Agent
* Coding Agent
* Travel Agent
* Custom Agents

Agents can use local LLMs through Ollama and share selected memories through a dedicated memory service.

## Planned Architecture

```text
Slack
  |
  v
Slack Gateway
  |
  v
Concierge Orchestrator
  |-- Hermes Agent
  |-- Calendar Agent
  |-- Research Agent
  |-- Custom Agents
  |
  v
Shared Memory Service
  |
  v
Ollama / Google Calendar / GitHub
```

## Goals

* Run LLMs locally through Ollama
* Access the concierge from Slack
* Delegate tasks to specialized agents
* Support Hermes Agent and custom agents
* Share selected memories between agents
* Integrate with services such as Google Calendar and GitHub
* Require human approval for sensitive actions
* Observe agent activity with OpenTelemetry and compare Phoenix and MLflow as observability backends

## Project Status

* [x] Set up the development environment
* [x] Run Ollama locally
* [x] Connect Hermes Agent to Ollama
* [x] Create a Slack application
* [x] Implement the Slack Gateway
* [x] Add Google Calendar read access
* [x] Add OpenTelemetry observability foundation
* [ ] Compare Phoenix and MLflow using shared application traces
* [ ] Add a human approval workflow
* [ ] Implement multi-agent routing
* [ ] Implement the shared memory service

## Documentation

* [Architecture](docs/architecture.md)
* [Roadmap](docs/roadmap.md)

Setup guides:

* [Ollama setup](docs/setup/ollama.md)
* [Hermes Agent setup](docs/setup/hermes-agent.md)
* [Slack Gateway setup](docs/setup/slack.md)
* [Google Calendar setup](docs/setup/google-calendar.md)
* [Observability setup](docs/setup/observability.md)

Domain models:

* [Agent contracts](docs/agent-contracts/domain-model.md)
* [Approvals](docs/approval/domain-model.md)
* [Orchestrator](docs/orchestrator/domain-model.md)

Observability notes:

* [Collector redaction](docs/observability/collector-redaction.md)
* [Google Calendar MCP telemetry](docs/observability/google-calendar-mcp-telemetry.md)
* [Hermes trace context](docs/observability/hermes-trace-context.md)

Development workflow:

* [Level 3 delegated development, Experiment #2](docs/delegated-development/level3-experiment-2.md)
* [Review loop runner](tools/review-loop/README.md)

## Initial Milestone

The first milestone is to establish the following communication flow:

```text
Slack
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

Google Calendar read access is now implemented through a dedicated MCP service. Shared memory and multi-agent routing will be added in later milestones.

## Security

The following information must not be committed to this repository:

* Slack tokens and signing secrets
* Google OAuth credentials and refresh tokens
* Personal calendar data
* Slack messages and conversation history
* Memory databases
* Phoenix traces containing personal information
* Ollama model files

Use `.env.example` to document required environment variables. Store actual credentials in a local `.env` file that is excluded by `.gitignore`.

## Repository Structure

```text
local-agent-concierge/
├── README.md
├── LICENSE
├── .gitignore
├── .dockerignore
├── .env.example
├── docker-compose.yml
├── .github/
│   └── workflows/
├── docs/
│   ├── architecture.md
│   ├── roadmap.md
│   ├── agent-contracts/
│   ├── approval/
│   ├── delegated-development/
│   ├── observability/
│   ├── orchestrator/
│   └── setup/
├── apps/
│   ├── hermes-agent/
│   └── slack-gateway/
├── mcp/
│   └── google-calendar/
├── packages/
│   ├── agent-contracts/
│   └── approvals/
├── services/
│   └── orchestrator/
├── infra/
│   └── observability/
└── tools/
    └── review-loop/
```

The containerized services built from this repository are spread across
three directories: `apps/` (Hermes Agent, Slack Gateway), `mcp/` (the Google
Calendar MCP server) and `services/` (the Orchestrator) — one `Dockerfile`
each. `packages/` holds importable domain packages those services share,
rather than a service of its own; `infra/` holds infrastructure
configuration such as the OpenTelemetry Collector's; and `tools/` holds
local development tooling that runs outside the containerized runtime.

The repository structure will expand as additional agents and services are implemented.

## License

This project is licensed under the MIT License.
