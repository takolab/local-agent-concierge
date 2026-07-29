# Slack Gateway Setup

This document explains how to create and configure a Slack application, run the Slack Gateway through Docker Compose, and verify the end-to-end communication path from Slack to Hermes Agent and Ollama.

## Overview

The Slack Gateway provides the conversational entry point for Local Agent Concierge.

The current communication path is:

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
gemma4:12b
    |
    v
Slack thread response
```

The Slack Gateway is intentionally limited to Slack-specific responsibilities:

* Maintain the Slack Socket Mode connection.
* Receive direct-message events.
* Validate and normalize Slack event data.
* Associate messages with Slack threads and Hermes conversations.
* Forward user text to the Hermes API.
* Display a temporary processing status.
* Return generated responses to Slack.
* Display a user-friendly error when Hermes is unavailable.
* Prevent duplicate processing of the same Slack event.

The Slack Gateway does not contain agent reasoning, model logic, or business-specific tool behavior.

## Tested Environment

This setup was initially verified with:

```text
Slack Bolt for Python: 1.30.x
Python: 3.12
Hermes Agent: 0.19.0
Ollama model: gemma4:12b
Operating environment: WSL2
Container orchestration: Docker Compose
Slack connection mode: Socket Mode
```

Dependency versions and Slack administration screens may change over time.

## Prerequisites

Complete the Ollama and Hermes Agent setup before configuring Slack.

The following items must already work:

* Docker Engine
* Docker Compose
* Ollama running through Docker Compose
* `gemma4:12b` installed in Ollama
* Hermes Agent connected to Ollama
* Hermes API server enabled
* A local `.env` file excluded from Git

Verify the existing services:

```bash
docker compose up -d ollama hermes-agent
docker compose ps
```

Expected services:

```text
ollama          running and healthy
hermes-agent    running
```

## Create a Slack Application

Open the Slack application management page and create a new application.

Choose:

```text
Create New App
    |
    v
From scratch
```

Provide:

```text
App Name:
A name for the personal concierge

Development Workspace:
The Slack workspace where the gateway will run
```

This project is currently designed for a personal development workspace rather than public Slack Marketplace distribution.

## Configure Bot Token Scopes

Open:

```text
OAuth & Permissions
    |
    v
Scopes
    |
    v
Bot Token Scopes
```

Add these scopes:

```text
chat:write
im:history
```

Their responsibilities are:

| Scope        | Purpose                                             |
| ------------ | --------------------------------------------------- |
| `chat:write` | Post processing, response, and error messages       |
| `im:history` | Receive direct-message events and access DM context |

The current implementation does not require:

```text
chat:write.public
channels:history
groups:history
mpim:history
```

It listens only for direct messages to the Slack application.

After changing scopes, reinstall the application to the workspace so that the new permissions are applied.

## Enable Direct Messages in App Home

Open:

```text
App Home
    |
    v
Show Tabs
```

Enable the Messages tab.

Also enable the option that allows users to send messages to the application from the Messages tab.

Depending on the Slack administration interface, the option may be labelled similarly to:

```text
Allow users to send Slash commands and messages
from the messages tab
```

Without this setting, Slack may display:

```text
Sending messages to this app has been turned off.
```

The Home tab itself is optional for the current milestone. The Messages tab is required.

## Configure Event Subscriptions

Open:

```text
Event Subscriptions
```

Turn on:

```text
Enable Events
```

Under:

```text
Subscribe to bot events
```

add:

```text
message.im
```

The `message.im` event is emitted when a message is posted in a one-to-one direct-message channel with the application.

Because this project uses Socket Mode, no public Request URL is required.

## Enable Socket Mode

Open:

```text
Socket Mode
```

Turn on:

```text
Enable Socket Mode
```

Socket Mode delivers Slack events over an outbound WebSocket connection initiated by the Slack Gateway.

The local environment therefore does not need to expose an HTTP endpoint for incoming Slack events.

The current Compose service publishes no host port for the Slack Gateway.

## Create an App-Level Token

Open:

```text
Basic Information
    |
    v
App-Level Tokens
    |
    v
Generate Token and Scopes
```

Create a token with a descriptive name such as:

```text
local-agent-concierge
```

Add the following app-level scope:

```text
connections:write
```

Generate the token and store the value securely.

The App-Level Token starts with:

```text
xapp-
```

This token is used only to establish the Socket Mode WebSocket connection.

## Install the Application to the Workspace

Open:

```text
OAuth & Permissions
```

Select:

```text
Install to Workspace
```

Approve the requested permissions.

After installation, copy the Bot User OAuth Token.

The Bot User OAuth Token starts with:

```text
xoxb-
```

If scopes or event subscriptions are changed later, Slack may require the application to be reinstalled.

## Required Credentials

The current Slack Gateway requires two Slack credentials:

| Environment variable | Token type           | Prefix  |
| -------------------- | -------------------- | ------- |
| `SLACK_BOT_TOKEN`    | Bot User OAuth Token | `xoxb-` |
| `SLACK_APP_TOKEN`    | App-Level Token      | `xapp-` |

It also requires the shared Hermes API key:

```text
HERMES_API_SERVER_KEY
```

The public `.env.example` contains empty placeholders:

```dotenv
# Hermes Agent
HERMES_API_SERVER_KEY=

# Slack
SLACK_BOT_TOKEN=
SLACK_APP_TOKEN=
SLACK_SIGNING_SECRET=
```

The current Socket Mode implementation does not read `SLACK_SIGNING_SECRET`.

That variable is reserved for a possible future HTTP Events API or OAuth-based deployment. It may remain empty for the current setup.

## Configure the Local Environment

Open the local `.env` file:

```bash
nano .env
```

Set the required values:

```dotenv
HERMES_API_SERVER_KEY=replace-with-a-random-local-key

SLACK_BOT_TOKEN=xoxb-replace-with-your-bot-token
SLACK_APP_TOKEN=xapp-replace-with-your-app-token
```

Do not add spaces around `=`.

Do not wrap the values in placeholder angle brackets.

The Hermes API key must be identical for both:

```text
Hermes API server
Slack Gateway client
```

Docker Compose passes the same local value to both containers.

## Protect Credentials

The local `.env` file must not be committed.

Verify that it is ignored:

```bash
git check-ignore -v .env
```

Verify that Git is not tracking it:

```bash
git ls-files .env
```

The second command should produce no output.

Do not commit:

* Slack Bot Tokens
* Slack App-Level Tokens
* Slack signing secrets
* Hermes API keys
* Slack messages
* Slack conversation history

## Docker Compose Configuration

The Slack Gateway is defined in the repository root `docker-compose.yml`.

The effective service configuration is:

```yaml
slack-gateway:
  build:
    context: ./apps/slack-gateway

  restart: unless-stopped

  depends_on:
    hermes-agent:
      condition: service_started

  environment:
    SLACK_BOT_TOKEN: "${SLACK_BOT_TOKEN:?SLACK_BOT_TOKEN must be set in .env}"
    SLACK_APP_TOKEN: "${SLACK_APP_TOKEN:?SLACK_APP_TOKEN must be set in .env}"
    HERMES_API_BASE_URL: "http://hermes-agent:8642"
    HERMES_API_SERVER_KEY: "${HERMES_API_SERVER_KEY:?HERMES_API_SERVER_KEY must be set in .env}"

  networks:
    - concierge-network
```

Important properties:

* The Gateway image is built from `apps/slack-gateway`.
* The Gateway starts after the Hermes container starts.
* Slack credentials are read from environment variables.
* The Hermes API is accessed through the Compose service hostname.
* The Slack Gateway and Hermes Agent share `concierge-network`.
* No Slack Gateway port is published to the host.
* No Hermes API port is published to the host.

The internal Hermes API URL is:

```text
http://hermes-agent:8642
```

Inside the Slack Gateway container, do not use:

```text
http://localhost:8642
```

`localhost` would refer to the Slack Gateway container itself.

## Hermes API Configuration

The Hermes service enables its API server with:

```yaml
API_SERVER_ENABLED: "true"
API_SERVER_HOST: "0.0.0.0"
API_SERVER_KEY: "${HERMES_API_SERVER_KEY}"
```

The Hermes API listens on the container network but is not published to the host.

The resulting path is:

```text
Slack Gateway
    |
    | Bearer HERMES_API_SERVER_KEY
    v
http://hermes-agent:8642/v1/responses
```

The request uses:

```json
{
  "model": "hermes-agent",
  "input": "User message",
  "conversation": "Slack conversation identifier",
  "store": true
}
```

## Validate the Compose Configuration

Validate the complete Compose file:

```bash
docker compose config --quiet
```

List the configured services:

```bash
docker compose config --services
```

Expected services:

```text
ollama
hermes-agent
slack-gateway
```

Inspect the Slack Gateway environment without printing secret values manually:

```bash
docker compose config \
  | sed -n '/slack-gateway:/,/^[^ ]/p'
```

Be careful when sharing the output of `docker compose config`, because Compose may interpolate local secret values.

## Build the Slack Gateway

Build the image:

```bash
docker compose build slack-gateway
```

Start all required services:

```bash
docker compose up -d
```

Inspect the service state:

```bash
docker compose ps
```

Expected state:

```text
ollama          running and healthy
hermes-agent    running
slack-gateway   running
```

## Inspect Startup Logs

Display recent Slack Gateway logs:

```bash
docker compose logs --tail=100 slack-gateway
```

Follow logs in real time:

```bash
docker compose logs -f slack-gateway
```

The logs should not contain:

* Invalid Slack authentication
* Invalid App-Level Token errors
* Missing environment-variable errors
* Socket Mode connection failures
* Hermes API authentication failures
* Repeated Python tracebacks

Stop following logs with:

```text
Ctrl+C
```

This does not stop the container.

## Verify the Socket Mode Connection

With the Slack Gateway running, open the application in Slack.

The Messages tab should allow a direct message to be entered.

Send a short test request:

```text
Reply with exactly SLACK_GATEWAY_OK.
```

Expected flow:

```text
Slack direct message
    |
    v
Temporary processing status
    |
    v
Hermes Agent request
    |
    v
Ollama response
    |
    v
Final response posted to the Slack thread
    |
    v
Temporary processing status removed
```

The final Slack thread should contain the generated response and should not retain the temporary processing message.

## Verify Thread Continuity

Send a top-level direct message containing a simple fact:

```text
Remember the code word ORANGE-42.
```

Reply inside the generated Slack thread:

```text
What code word did I give you?
```

The second event should use the same Hermes conversation identifier as the first event.

The Gateway derives the conversation identifier as:

```text
slack:{workspace_id}:{channel_id}:{root_thread_ts}
```

For a top-level direct message:

```text
root_thread_ts = event.ts
```

For a reply inside a thread:

```text
root_thread_ts = event.thread_ts
```

This keeps each Slack thread isolated as its own Hermes conversation.

## Processing Status

When a valid Slack message is received, the Gateway posts a temporary processing message before waiting for Hermes.

This gives the user immediate feedback during local model inference, which may take longer than a hosted API request.

After a successful Hermes response:

1. The final response is posted to the same Slack thread.
2. The temporary processing message is deleted.

The Gateway posts first and deletes second. This avoids leaving the thread empty if posting the final response fails.

Updating the temporary message is intentionally avoided because Slack may display an `(edited)` label.

## User-Facing Error Handling

When the Slack Gateway cannot complete a request through Hermes, it posts a generic user-facing error:

```text
⚠️ I couldn't complete that request. Please try again.
```

Internal exception details are written to container logs rather than exposed to the Slack user.

This separation prevents implementation details, internal URLs, and provider errors from being shown unnecessarily in Slack.

## Verify the Hermes-Unavailable Error

Stop the Hermes Agent container:

```bash
docker compose stop hermes-agent
```

Send a new direct message to the Slack application.

Expected final Slack message:

```text
⚠️ I couldn't complete that request. Please try again.
```

The temporary processing message should be removed.

Inspect the Gateway logs:

```bash
docker compose logs --tail=150 slack-gateway
```

The logs should contain the internal connection failure.

Restart Hermes:

```bash
docker compose start hermes-agent
docker compose ps
```

Send another direct message and confirm that normal responses resume.

## Duplicate Event Handling

Slack may retry event delivery.

The Gateway uses the top-level Slack `event_id` to prevent the same event from being processed more than once.

The current deduplicator:

* Stores claimed event IDs in memory.
* Uses a 24-hour time-to-live.
* Retains at most 10,000 event IDs.
* Uses a lock for access within one Gateway process.
* Removes expired and oldest entries.

This implementation is appropriate for one Slack Gateway process.

The history is lost when the container restarts.

If the Gateway is scaled to multiple containers or processes, replace the in-memory implementation with a shared store such as Redis.

A distributed implementation should claim each Slack event ID atomically, for example with Redis `SET NX` and an expiration time.

## Ignored Slack Events

The current Gateway ignores:

* Messages posted by bots
* Message events with Slack subtypes
* Empty messages
* Events without a valid `event_id`
* Events without required routing fields
* Events with an invalid thread timestamp
* Duplicate events

Ignoring bot messages prevents the Gateway from processing its own responses and creating an infinite reply loop.

Message edits and deletion events contain subtypes and are not forwarded to Hermes.

## Restart the Complete Stack

Verify that the complete system survives recreation:

```bash
docker compose down
docker compose up -d
docker compose ps
```

After all services start, send another Slack direct message.

The Gateway should reconnect to Socket Mode automatically and return a response.

Hermes configuration and conversation state remain under:

```text
data/hermes
```

Ollama models remain in the persistent Docker volume:

```text
ollama-data
```

## Troubleshooting

### Sending Messages Is Disabled

Symptom:

```text
Sending messages to this app has been turned off.
```

Check:

1. Open the Slack application configuration.
2. Open `App Home`.
3. Enable the Messages tab.
4. Allow users to send messages from the Messages tab.
5. Reopen the application in Slack.

### Invalid Bot Token

Symptoms may include:

```text
invalid_auth
not_authed
```

Check that:

* `SLACK_BOT_TOKEN` is present in `.env`.
* The token starts with `xoxb-`.
* The application is installed in the intended workspace.
* The Bot Token has not been revoked or regenerated.

Recreate the Gateway after changing the token:

```bash
docker compose up -d --force-recreate slack-gateway
```

### Socket Mode Connection Failure

Check that:

* Socket Mode is enabled.
* `SLACK_APP_TOKEN` starts with `xapp-`.
* The App-Level Token has `connections:write`.
* The token belongs to the same Slack application as the Bot Token.
* The host can make outbound HTTPS and WebSocket connections.

Inspect logs:

```bash
docker compose logs --tail=200 slack-gateway
```

### Missing Scope

A Slack API error may report:

```text
missing_scope
```

Check that the application has:

```text
chat:write
im:history
```

After adding scopes, reinstall the Slack application to the workspace.

### Direct Messages Are Not Received

Check:

* Event Subscriptions are enabled.
* `message.im` is listed under bot events.
* `im:history` is included in Bot Token Scopes.
* The application was reinstalled after permission changes.
* The Messages tab is enabled.
* The Slack Gateway has an active Socket Mode connection.

### Hermes Connection Failure

Inspect container state:

```bash
docker compose ps
```

Inspect Hermes logs:

```bash
docker compose logs --tail=200 hermes-agent
```

Inspect Gateway logs:

```bash
docker compose logs --tail=200 slack-gateway
```

Confirm the internal URL:

```text
http://hermes-agent:8642
```

Confirm that the two services share:

```text
concierge-network
```

### Hermes Authentication Failure

Check that the same local value is used for:

```text
HERMES_API_SERVER_KEY
```

The value is passed to:

```text
Hermes API_SERVER_KEY
Slack Gateway HERMES_API_SERVER_KEY
```

Recreate both services after changing it:

```bash
docker compose up -d --force-recreate \
  hermes-agent \
  slack-gateway
```

### Processing Status Remains Visible

This means the final response or error may have been posted successfully, but deletion of the temporary processing message failed.

Inspect the Slack Gateway logs for a `chat.delete` error.

Confirm that the processing message was originally posted by the same Bot Token currently used by the Gateway.

### No Response After a Long Delay

Local inference time depends on:

* Model size
* Prompt length
* Conversation length
* Available GPU memory
* GPU utilization
* Agent iterations
* Tool calls

Inspect live logs:

```bash
docker compose logs -f \
  slack-gateway \
  hermes-agent \
  ollama
```

Inspect GPU usage from the host:

```bash
watch -n 1 \
  'nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader'
```

## Known Hermes Responses API Limitation

This setup was verified with Hermes Agent 0.19.0.

In this version, some internal model-provider failures may be flattened by the non-streaming `/v1/responses` endpoint into an apparently successful response.

An observed response was:

```json
{
  "status": "completed",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "API call failed after 3 retries: Connection error."
        }
      ]
    }
  ],
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0
  }
}
```

The HTTP status was `200`.

Because the response is marked as completed, the Slack Gateway cannot reliably distinguish it from a legitimate assistant response without parsing human-readable error text.

This project intentionally does not use error-message string matching as a permanent workaround.

The intended upstream behavior is to preserve the structured Agent failure state and return a machine-readable failure response.

Related upstream work:

* Issue: `NousResearch/hermes-agent#22496`
* Pull request: `NousResearch/hermes-agent#22501`
* Chat Completions fix: `NousResearch/hermes-agent#22775`

Until the Responses API path is fixed upstream:

* Stopping the Hermes container is correctly detected as a Gateway connection failure.
* A provider failure inside a running Hermes container may still appear as raw response text.
* Internal error-string detection is not implemented in this repository.
* The Hermes version should be retested when upgrading the container image.

## Security Notes

The Slack Gateway handles private messages and authentication credentials.

Follow these rules:

* Never commit `.env`.
* Never print Slack tokens in logs.
* Never include tokens in screenshots or issue reports.
* Do not expose the Hermes API port publicly.
* Do not expose a Slack Gateway HTTP port when Socket Mode is used.
* Do not persist raw Slack messages outside the intended Hermes conversation store.
* Review logs before sharing them publicly.
* Rotate a token immediately if it is exposed.

## Stop the Services

Stop the running services:

```bash
docker compose down
```

Persistent data remains available in:

```text
data/hermes
ollama-data
```

## References

* Slack Socket Mode: https://docs.slack.dev/apis/events-api/using-socket-mode/
* Slack Python Socket Mode client: https://docs.slack.dev/tools/python-slack-sdk/socket-mode/
* Slack `connections:write` scope: https://docs.slack.dev/reference/scopes/connections.write/
* Slack `message.im` event: https://docs.slack.dev/reference/events/message.im
* Slack App Home: https://docs.slack.dev/surfaces/app-home/
* Hermes Agent issue 22496: https://github.com/NousResearch/hermes-agent/issues/22496
* Hermes Agent pull request 22501: https://github.com/NousResearch/hermes-agent/pull/22501
* Hermes Agent pull request 22775: https://github.com/NousResearch/hermes-agent/pull/22775
