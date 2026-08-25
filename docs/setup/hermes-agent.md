# Hermes Agent Setup

This document explains how to run Hermes Agent with Docker Compose, connect it to the containerized Ollama service, configure Gemma 4 12B as a custom model provider, and verify basic chat and tool-calling behavior.

## Overview

Hermes Agent provides the agent runtime for Local Agent Concierge.

In the current development environment:

* Ollama provides local LLM inference.
* Gemma 4 12B is the initial model.
* Hermes Agent connects to Ollama through the internal Docker Compose network.
* Hermes configuration, sessions, skills, memory, and local credentials are stored outside the disposable container.
* Slack and other messaging gateways are not configured yet.

The tested communication path is:

```text
User command
    |
    v
Hermes Agent container
    |
    | OpenAI-compatible Chat Completions API
    | http://ollama:11434/v1/chat/completions
    v
Ollama container
    |
    v
gemma4:12b
```

The tested tool-calling path is:

```text
User instruction
    |
    v
Hermes Agent
    |
    v
Gemma 4 selects a tool
    |
    v
Hermes Terminal Tool
    |
    v
Tool result returned to Gemma 4
    |
    v
Final response
```

## Tested Environment

This setup was initially verified with:

```text
Hermes Agent: 0.19.0
Python: 3.13.5
OpenAI SDK: 2.24.0
Operating environment: WSL2
Ollama model: gemma4:12b
Ollama context length: 65536
API mode: Chat Completions
```

The `latest` container image may install a newer Hermes Agent version in the future. Command output and interactive prompts may change between versions.

## Prerequisites

Complete the Ollama setup before configuring Hermes Agent.

The following items must already work:

* Docker Engine
* Docker Compose
* Ollama running through Docker Compose
* `gemma4:12b` downloaded in Ollama
* Ollama reporting a context length of `65536`
* The Ollama service attached to `concierge-network`

Verify the Ollama service:

```bash
docker compose up -d ollama
docker compose ps
```

Verify that the model is installed:

```bash
docker compose exec ollama ollama list
```

Expected model:

```text
gemma4:12b
```

Verify the effective context length after loading the model:

```bash
curl -s http://localhost:11434/api/ps \
  | python3 -m json.tool
```

The loaded model should report:

```json
{
  "context_length": 65536
}
```

## Persistent Hermes Data

Hermes stores its runtime data in `/opt/data` inside the container.

Create a host directory for the bind mount:

```bash
mkdir -p data/hermes
```

The project mounts this directory as:

```text
Host:      ./data/hermes
Container: /opt/data
```

This directory may contain:

```text
data/hermes/
├── .env
├── config.yaml
├── SOUL.md
├── cron/
├── logs/
├── memories/
├── sessions/
├── skills/
└── state.db
```

The exact contents are created incrementally as Hermes features are used.

The complete `data/` directory is excluded by `.gitignore`. Do not commit Hermes configuration, credentials, sessions, prompts, memories, or local state to the public repository.

Verify that the directory is ignored:

```bash
git check-ignore -v data/hermes
```

After Hermes creates files, verify a specific file:

```bash
git check-ignore -v data/hermes/config.yaml
```

## Host UID and GID

The Hermes container can remap its internal user to the host user's UID and GID. This keeps files created under `data/hermes` readable and writable from the host.

Check the host UID:

```bash
id -u
```

Check the host GID:

```bash
id -g
```

Add the actual values to the local `.env` file:

```dotenv
HERMES_UID=1000
HERMES_GID=1000
```

Use the values returned by `id -u` and `id -g`; do not assume they are always `1000`.

Verify the local configuration:

```bash
grep -E '^HERMES_(UID|GID)=' .env
```

The public `.env.example` documents the variables without storing private credentials:

```dotenv
# Hermes Agent
HERMES_UID=1000
HERMES_GID=1000
```

The local `.env` file is excluded from Git.

## Docker Compose Configuration

The Hermes Agent service is defined in the repository root `docker-compose.yml`.

The service configuration is:

```yaml
hermes-agent:
  build:
    context: ./apps/hermes-agent
  restart: unless-stopped

  command:
    - gateway
    - run

  depends_on:
    ollama:
      condition: service_healthy

  environment:
    HERMES_UID: "${HERMES_UID:-1000}"
    HERMES_GID: "${HERMES_GID:-1000}"

  volumes:
    - ./data/hermes:/opt/data

  networks:
    - concierge-network
```

Important properties:

* `apps/hermes-agent/Dockerfile` builds `nousresearch/hermes-agent` (pinned
  to a specific release tag, not `:latest`) with OpenTelemetry
  auto-instrumentation layered on top, activated via a build-time
  `PYTHONPATH` rather than a command wrapper. See
  `docs/observability/hermes-trace-context.md` for why (including a real
  incident it fixed), and `docs/roadmap.md` (Milestone 5) for the
  underlying investigation.
* `gateway run` is the unmodified, standard long-running gateway command —
  incoming trace context is extracted without changing how Hermes is
  invoked.
* Hermes waits for the Ollama health check.
* Hermes and Ollama share `concierge-network`.
* Hermes data persists through the `/opt/data` bind mount.
* The container maps generated files to the host user's UID and GID.

The service does not publish a Hermes API port yet.

The initial milestone uses one-off CLI containers for validation. The persistent gateway will become useful after Slack or another platform is configured.

## Validate the Compose Configuration

Validate the complete Compose file:

```bash
docker compose config
```

List the services:

```bash
docker compose config --services
```

Expected services:

```text
ollama
hermes-agent
```

Inspect the dependency configuration:

```bash
docker compose config | grep -A 6 depends_on
```

Hermes should depend on the healthy Ollama service.

## Pull the Hermes Image

Download the Hermes Agent image:

```bash
docker compose pull hermes-agent
```

Display the installed Hermes Agent version:

```bash
docker compose run --rm hermes-agent --version
```

A temporary container may print s6-overlay initialization and shutdown messages. These messages are expected for the official container image.

## Understand the Container Startup Logs

The Hermes image uses s6-overlay as its container supervisor.

A one-off command may display messages similar to:

```text
s6-rc: info: service s6rc-oneshot-runner: starting
cont-init: info: running /etc/cont-init.d/01-hermes-setup
s6-rc: info: service main-hermes: starting
s6-rc: info: service legacy-services: stopping
```

These messages show that the container:

1. Initializes permissions.
2. Reconciles the Hermes profile.
3. Starts supervised services.
4. Runs the requested command.
5. Stops the temporary container cleanly.

These logs are not an Ollama or Hermes connection error by themselves.

## Verify Container-to-Container Connectivity

Start Ollama:

```bash
docker compose up -d ollama
```

Confirm that it becomes healthy:

```bash
docker compose ps
```

Verify the Ollama OpenAI-compatible models endpoint from inside the Hermes container:

```bash
docker compose run --rm hermes-agent \
  sh -lc 'curl -fsS http://ollama:11434/v1/models | python3 -m json.tool'
```

Expected JSON includes:

```json
{
  "object": "list",
  "data": [
    {
      "id": "gemma4:12b",
      "object": "model"
    }
  ]
}
```

This confirms:

```text
Hermes container
    |
    v
Docker DNS resolves "ollama"
    |
    v
Ollama port 11434 is reachable
    |
    v
The OpenAI-compatible API lists gemma4:12b
```

Inside the Hermes container, do not use:

```text
http://localhost:11434
```

`localhost` refers to the Hermes container itself.

Use the Compose service name:

```text
http://ollama:11434
```

The OpenAI-compatible base URL is:

```text
http://ollama:11434/v1
```

## Avoid Mixing s6 Logs with JSON Parsing

The following command sends the complete container output into the host JSON parser:

```bash
docker compose run --rm hermes-agent \
  curl -s http://ollama:11434/v1/models \
  | python3 -m json.tool
```

Because s6-overlay messages may be included in that output, the host parser can fail with:

```text
Expecting value: line 1 column 2 (char 1)
```

This does not necessarily mean the Ollama endpoint failed.

Run the pipe inside the container instead:

```bash
docker compose run --rm hermes-agent \
  sh -lc 'curl -fsS http://ollama:11434/v1/models | python3 -m json.tool'
```

## Configure the Custom Model Provider

Run the interactive model configuration command:

```bash
docker compose run --rm -it hermes-agent model
```

Choose the custom or self-hosted endpoint option.

Use the following values:

```text
Provider:
Custom endpoint

API base URL:
http://ollama:11434/v1

API key:
ollama

Model name:
gemma4:12b

Context length:
65536

API compatibility mode:
Chat Completions
```

For the API compatibility prompt:

```text
Select API compatibility mode:
  1. Auto-detect
  2. Chat Completions
  3. Responses / Codex
  4. Anthropic Messages
```

Select:

```text
2
```

The current setup uses Ollama's OpenAI-compatible endpoint:

```text
POST /v1/chat/completions
```

Therefore, the explicit mode should be:

```text
chat_completions
```

## Expected Model Configuration

After the model wizard completes, inspect the generated configuration:

```bash
sed -n '1,200p' data/hermes/config.yaml
```

The primary model section should contain:

```yaml
model:
  default: gemma4:12b
  provider: custom
  base_url: http://ollama:11434/v1
  api_mode: chat_completions
```

Hermes may store the configured context length in an additional custom-provider section. The exact YAML structure may change between Hermes versions.

The important effective settings are:

```text
Model: gemma4:12b
Provider: custom
Base URL: http://ollama:11434/v1
API mode: chat_completions
Context length: 65536
```

Do not manually replace the generated file unless troubleshooting requires it. Prefer the interactive `model` command because it validates and migrates the configuration format.

## Local Hermes Environment File

Hermes expects its local environment file at:

```text
data/hermes/.env
```

For the local Ollama custom endpoint, use a non-empty placeholder API key:

```bash
printf 'OPENAI_API_KEY=ollama\n' > data/hermes/.env
chmod 600 data/hermes/.env
```

Verify the file permissions:

```bash
ls -l data/hermes/.env
```

Expected permissions begin with:

```text
-rw-------
```

Ollama does not use this value to authenticate the local request in the current setup. It provides a non-empty key for OpenAI-compatible client behavior and prevents the Hermes diagnostic command from reporting a missing environment file.

Do not commit this file.

## Inspect the Hermes Configuration

Display a shareable setup summary:

```bash
docker compose run --rm hermes-agent dump
```

The output should include:

```text
model:            gemma4:12b
provider:         custom
terminal:         local
```

The `dump` command also reports:

* Hermes version
* Operating system
* Python version
* OpenAI SDK version
* Active profile
* API-key presence
* Enabled toolsets
* Gateway state
* Memory provider
* Installed skills
* Configuration overrides

Do not use `--show-keys` when copying output into public issues unless reviewing the redacted prefixes is necessary.

## Run Hermes Doctor

Run the diagnostic command:

```bash
docker compose run --rm hermes-agent doctor
```

Successful sections should include:

* Python environment
* SSL certificates
* Required packages
* Configuration files
* Directory structure
* s6 supervision
* Core tool availability
* Built-in memory provider

Some warnings are expected at this stage.

### Gateway Stopped

The dump output may show:

```text
gateway: stopped
platforms: none
```

This is expected before Slack or another messaging platform is configured.

The current validation uses one-off CLI containers rather than an always-on messaging gateway.

### Missing Cloud Provider Authentication

Warnings for the following providers are not relevant to the local Ollama setup:

* OpenRouter
* Anthropic
* Nous Portal
* OpenAI Codex
* MiniMax
* xAI
* Gemini
* Other cloud providers

These providers do not need to be configured unless the project intentionally uses them.

### Missing Optional Tool Keys

Warnings for web search, image generation, TTS, GitHub, and other optional tools are expected until those integrations are added.

### Missing Hermes Symlink

The doctor command may report:

```text
~/.local/bin/hermes not found
```

This symlink is useful for a normal host installation.

It is not required for the current Docker workflow because the image entrypoint already invokes the Hermes executable inside its virtual environment.

Do not run `doctor --fix` only to resolve this container-specific warning.

### npm Dependency Warnings

The doctor output may report vulnerabilities in the `web` or `ui-tui` workspaces.

These warnings originate from dependencies bundled in the upstream Hermes image. Record them and reevaluate them after updating the image. Do not modify the installed image manually as part of this repository.

## Full Setup Wizard

The full command is:

```bash
docker compose run --rm -it hermes-agent setup
```

It configures multiple areas, including providers, API keys, tools, and messaging platforms.

The full setup wizard is not required for the current milestone because the model provider was configured directly with:

```bash
docker compose run --rm -it hermes-agent model
```

Use the full setup wizard later only when its additional configuration is needed.

## Test Basic Chat

Run a one-turn, non-interactive chat:

```bash
timeout 300 docker compose run --rm hermes-agent \
  chat \
  --max-turns 1 \
  --quiet \
  -q "Reply with exactly: Hermes Agent connected to Ollama."
```

Expected final response:

```text
Hermes Agent connected to Ollama.
```

The test may take several minutes on hardware where the model is partially loaded into system RAM.

The `timeout 300` wrapper terminates the command after five minutes if it becomes unresponsive.

The options mean:

```text
-q, --query       Run a one-shot non-interactive prompt
--quiet           Suppress most interactive UI output
--max-turns 1     Limit tool-calling iterations for this turn
```

## Verify the Ollama Request

In another terminal, follow the Ollama logs:

```bash
docker compose logs -f ollama
```

A successful Hermes chat produces a log similar to:

```text
200 | POST "/v1/chat/completions"
```

This confirms that Hermes used the configured Chat Completions endpoint.

The source address should be another IP on the Compose network, not the host loopback address.

Stop following the logs with `Ctrl+C`.

## Check the Loaded Model

After a Hermes request, inspect Ollama:

```bash
docker compose exec ollama ollama ps
```

Inspect the effective model state through the API:

```bash
curl -s http://localhost:11434/api/ps \
  | python3 -m json.tool
```

Confirm that the model remains loaded with:

```text
context_length: 65536
```

## Test Terminal Tool Calling

Basic chat proves model connectivity but does not prove that the model can select and use Hermes tools.

Run a harmless Terminal Tool test:

```bash
timeout 300 docker compose run --rm hermes-agent \
  chat \
  --toolsets terminal \
  --max-turns 3 \
  -q 'You must use the terminal tool exactly once. Run this exact command: printf "HERMES_TOOL_CALL_OK\n". Then reply with exactly the command output and nothing else.'
```

Expected behavior:

1. Hermes displays a Terminal Tool invocation.
2. The command is executed inside the Hermes container.
3. The tool result is returned to the model.
4. The final response includes:

```text
HERMES_TOOL_CALL_OK
```

The Terminal Tool uses the Hermes container as its local execution environment.

It does not run directly in the WSL2 host shell unless a separate terminal backend is configured.

## Verify That a Tool Runs Only Once

The Hermes session summary may report more tool-call messages than the number of externally visible shell commands.

Verify the actual side effect using a persistent test file.

Remove an old test file:

```bash
rm -f data/hermes/tool-call-count.txt
```

Run the test:

```bash
timeout 300 docker compose run --rm hermes-agent \
  chat \
  --toolsets terminal \
  --max-turns 3 \
  -q 'Use the terminal tool exactly once. Run this exact command without modifying it: printf "1\n" >> /opt/data/tool-call-count.txt. After the tool finishes, reply with only: DONE'
```

Expected final response:

```text
DONE
```

Count the written lines:

```bash
wc -l data/hermes/tool-call-count.txt
```

Expected output:

```text
1 data/hermes/tool-call-count.txt
```

Inspect the content:

```bash
cat data/hermes/tool-call-count.txt
```

Expected content:

```text
1
```

A single line confirms that the shell side effect occurred once, even if the Hermes session summary reports two internal tool-call messages.

Remove the test artifact:

```bash
rm data/hermes/tool-call-count.txt
```

## Observed Tool-Calling Behavior

During initial testing, one Terminal Tool request returned the correct result but also leaked internal-looking formatting into the final response:

```text
HERMES_TOOL_CALL_OK
<channel|><channel|>json
{
  "action": "none",
  "action_input": ""
}

HERMES_TOOL_CALL_OK
```

A second controlled test completed cleanly with:

```text
DONE
```

The actual file side effect occurred only once.

This indicates:

* Terminal Tool selection works.
* Terminal commands execute successfully.
* Tool output is returned to the model.
* Final response formatting may occasionally be inconsistent.
* Tool execution counts should be verified independently for operations with side effects.

Do not treat local-model tool output as fully reliable for sensitive or destructive operations.

## Safety Guidance

The current Hermes and local-model integration is suitable for development and controlled testing.

Before enabling external write operations:

* Restrict enabled toolsets to those required for the task.
* Require human approval for sensitive operations.
* Validate tool names and arguments before execution.
* Log every requested and executed action.
* Use idempotency controls for external writes.
* Avoid unrestricted `--yolo` execution.
* Use read-only tests before write tests.
* Sanitize internal control tokens before returning responses to Slack.
* Keep personal prompts, sessions, memories, and tool outputs outside Git.

The planned Human Approval Service should mediate operations such as:

* Creating or deleting calendar events
* Sending messages
* Modifying GitHub repositories
* Deleting files
* Executing privileged shell commands
* Making purchases or payments
* Updating external accounts

## Common Commands

Pull the Hermes image:

```bash
docker compose pull hermes-agent
```

Display the Hermes version:

```bash
docker compose run --rm hermes-agent --version
```

Configure the model:

```bash
docker compose run --rm -it hermes-agent model
```

Display the setup summary:

```bash
docker compose run --rm hermes-agent dump
```

Run diagnostics:

```bash
docker compose run --rm hermes-agent doctor
```

Start an interactive chat:

```bash
docker compose run --rm -it hermes-agent chat
```

Run a one-shot prompt:

```bash
docker compose run --rm hermes-agent \
  chat \
  -q "Hello from Local Agent Concierge."
```

Run with only the Terminal Toolset:

```bash
docker compose run --rm hermes-agent \
  chat \
  --toolsets terminal \
  -q "Use the terminal tool to print the current working directory."
```

Inspect Hermes data:

```bash
ls -la data/hermes
```

Follow Ollama logs:

```bash
docker compose logs -f ollama
```

Start the persistent Hermes gateway:

```bash
docker compose up -d hermes-agent
```

Check running services:

```bash
docker compose ps
```

Follow Hermes gateway logs:

```bash
docker compose logs -f hermes-agent
```

Stop the environment:

```bash
docker compose down
```

## Persistent Gateway

The Compose service is configured with:

```text
gateway run
```

Start it with:

```bash
docker compose up -d hermes-agent
```

Because Hermes depends on Ollama health, Docker Compose starts Ollama and waits for it before starting Hermes.

At the current milestone, the gateway may report no configured messaging platforms. This is expected.

The persistent gateway becomes the main runtime after Slack is configured.

## Verify Data Persistence

Display the configured provider:

```bash
docker compose run --rm hermes-agent dump
```

Stop the environment:

```bash
docker compose down
```

Start Ollama again:

```bash
docker compose up -d ollama
```

Run the dump again:

```bash
docker compose run --rm hermes-agent dump
```

The following values should still be available:

```text
model: gemma4:12b
provider: custom
```

This confirms that `data/hermes` preserves the Hermes configuration independently of the container lifecycle.

## Upgrade Hermes Agent

Pull the current image:

```bash
docker compose pull hermes-agent
```

Recreate the Hermes service:

```bash
docker compose up -d --force-recreate hermes-agent
```

Verify the installed version:

```bash
docker compose run --rm hermes-agent --version
```

Run diagnostics after every upgrade:

```bash
docker compose run --rm hermes-agent doctor
```

Run the basic chat and Tool Calling tests again because CLI behavior, configuration formats, bundled skills, and local-model compatibility can change between Hermes releases.

The `data/hermes` directory remains separate from the container image.

## Troubleshooting

### The Model Is Not Configured

Run:

```bash
docker compose run --rm -it hermes-agent model
```

Confirm:

```yaml
model:
  default: gemma4:12b
  provider: custom
  base_url: http://ollama:11434/v1
  api_mode: chat_completions
```

### Hermes Cannot Reach Ollama

Check both service definitions:

```bash
docker compose config --services
```

Inspect the Compose network:

```bash
docker network ls | grep concierge
```

Test the endpoint from Hermes:

```bash
docker compose run --rm hermes-agent \
  sh -lc 'curl -fsS http://ollama:11434/v1/models | python3 -m json.tool'
```

Do not configure the container with:

```text
http://localhost:11434/v1
```

Use:

```text
http://ollama:11434/v1
```

### The Models Endpoint Returns No Model

List Ollama models:

```bash
docker compose exec ollama ollama list
```

Download the model when necessary:

```bash
docker compose exec ollama ollama pull gemma4:12b
```

### Context Length Is Too Small

Validate the resolved Compose configuration:

```bash
docker compose config | grep -A 2 OLLAMA_CONTEXT_LENGTH
```

Expected value:

```text
OLLAMA_CONTEXT_LENGTH: "65536"
```

Recreate Ollama after changing the environment variable:

```bash
docker compose up -d --force-recreate ollama
```

Load the model and inspect `/api/ps` again.

### Hermes Reports a Missing `.env`

Create the local Hermes environment file:

```bash
printf 'OPENAI_API_KEY=ollama\n' > data/hermes/.env
chmod 600 data/hermes/.env
```

Do not commit it.

### Permission Denied Under `/opt/data`

Verify the host values:

```bash
id -u
id -g
```

Verify the local `.env`:

```bash
grep -E '^HERMES_(UID|GID)=' .env
```

Inspect ownership:

```bash
ls -ld data/hermes
find data/hermes -maxdepth 1 -printf '%u:%g %p\n'
```

Recreate the temporary container after correcting the values.

### Hermes Chat Is Slow

Inspect Ollama placement:

```bash
docker compose exec ollama ollama ps
```

Monitor GPU memory:

```bash
watch -n 1 \
  'nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader'
```

Monitor system memory:

```bash
watch -n 1 free -h
```

Gemma 4 12B with a 65536-token context may use both GPU memory and system RAM on GPUs with limited VRAM.

Slow inference does not necessarily indicate a connection error.

### Tool Calling Produces Internal Tokens

Possible output may include internal-looking text such as:

```text
<channel|><channel|>json
```

Retry with:

* A shorter prompt
* One explicitly enabled Toolset
* A low `--max-turns` value
* A harmless, deterministic command
* Independent verification of the side effect

Do not allow an inconsistent response format to trigger another external operation automatically.

### The Gateway Shows No Platforms

This is expected before Slack, Discord, Telegram, or another messaging platform is configured.

Platform integration is a later milestone.

### The Gateway Container Exits

Inspect logs:

```bash
docker compose logs --tail=200 hermes-agent
```

Check:

* `data/hermes/.env` exists
* `data/hermes/config.yaml` exists
* Host UID and GID are correct
* Ollama is healthy
* The Compose network is available

### Browser Tools Fail

The official image includes browser tooling, but browser automation may require additional shared memory.

This project does not rely on browser automation in the current milestone.

When browser tools are added, consider configuring additional container shared memory and repeat the security review.

## Security Notes

Do not publish the Hermes data directory.

The following must remain local:

```text
data/hermes/.env
data/hermes/config.yaml
data/hermes/sessions/
data/hermes/memories/
data/hermes/logs/
data/hermes/state.db
```

Although `config.yaml` may contain non-secret endpoint information, it can also accumulate personal configuration and should remain outside the public source tree.

Do not expose a Hermes API or dashboard port without:

* Authentication
* A restricted bind address
* Secret management
* Network access controls
* A clear threat model

The current Compose configuration intentionally avoids publishing Hermes ports.

## Current Limitations

The current milestone does not include:

* Slack integration
* A public Hermes API
* Dashboard exposure
* Google Calendar access
* Shared memory across multiple agents
* Human approval enforcement
* OpenTelemetry instrumentation
* Phoenix tracing
* Multi-agent routing
* Production-grade response sanitization

These features will be added incrementally after the local Hermes-to-Ollama foundation is stable.

## Completion Checklist

* [x] Hermes Agent image downloads successfully
* [x] Hermes Agent version command runs
* [x] Hermes data persists under `data/hermes`
* [x] Host UID and GID are mapped
* [x] Hermes and Ollama share a Docker network
* [x] Hermes waits for a healthy Ollama service
* [x] Hermes resolves the `ollama` service hostname
* [x] Hermes reaches `/v1/models`
* [x] `gemma4:12b` is listed
* [x] Custom Provider configuration is saved
* [x] Chat Completions mode is configured
* [x] Hermes dump reports `gemma4:12b`
* [x] Hermes dump reports the `custom` provider
* [x] Hermes doctor completes
* [x] Basic chat reaches `/v1/chat/completions`
* [x] Basic chat returns a model response
* [x] Terminal Tool Calling executes
* [x] Terminal Tool output reaches the model
* [x] A persistent side effect is verified to occur once
* [x] Hermes configuration survives container recreation
* [ ] Slack messaging is configured
* [ ] Human approval is enforced for sensitive actions
* [ ] Hermes activity is exported to Phoenix
