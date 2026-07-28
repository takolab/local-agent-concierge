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
* Observe agent activity with OpenTelemetry and Phoenix

## Project Status

* [x] Set up the development environment
* [x] Run Ollama locally
* [ ] Connect Hermes Agent to Ollama
* [ ] Create a Slack application
* [ ] Implement the Slack Gateway
* [ ] Add Google Calendar read access
* [ ] Add a human approval workflow
* [ ] Implement multi-agent routing
* [ ] Implement the shared memory service
* [ ] Add Phoenix observability

## Documentation

- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- [Ollama setup](docs/setup/ollama.md)

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

Google Calendar integration, shared memory, and multi-agent routing will be added after the initial communication flow is working.

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
├── .env.example
├── docker-compose.yml
├── docs/
│   ├── architecture.md
│   └── roadmap.md
└── apps/
    └── slack-gateway/
```

The repository structure will expand as additional agents and services are implemented.

## License

This project is licensed under the MIT License.
