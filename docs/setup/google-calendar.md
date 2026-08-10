# Google Calendar Setup

This document explains how to configure Google Calendar read access for Local Agent Concierge, run the Google Calendar MCP server through Docker Compose, connect it to Hermes Agent, and verify the complete Slack-to-Google-Calendar flow.

## Overview

Google Calendar is exposed to Hermes Agent through a dedicated MCP server.

The current communication path is:

```text
Slack direct message
    |
    v
Slack Gateway
    |
    | POST /v1/responses
    v
Hermes Agent
    |
    | MCP
    v
Google Calendar MCP
    |
    | Google Calendar API
    v
Google Calendar
```

The Google Calendar integration is currently read-only.

It supports:

* Reading upcoming calendar events
* Reading events within a specified time range
* Reading busy periods
* Calculating free periods
* Getting the current date and time for an IANA time zone

Calendar write operations are intentionally outside the scope of this milestone. Creating, updating, or deleting calendar events will require a separate human-approval workflow.

## Prerequisites

Before configuring Google Calendar, the following components should already work:

* Docker Engine
* Docker Compose
* Ollama running through Docker Compose
* Hermes Agent connected to Ollama
* Slack Gateway connected to Hermes Agent
* A Google account with access to the target calendar
* A Google Cloud project
* A local `.env` file excluded from Git

Verify the existing Docker Compose application:

```bash
docker compose ps
```

The core services should already be running successfully.

## Google Calendar MCP Service

The Google Calendar MCP server is located under:

```text
mcp/google-calendar/
```

It runs as an independent Docker Compose service:

```text
google-calendar-mcp
```

The service communicates with Hermes Agent through the shared Docker Compose network.

Hermes therefore connects to the service using:

```text
http://google-calendar-mcp:8000/mcp
```

Do not use:

```text
http://localhost:8000/mcp
```

from inside the Hermes container because `localhost` refers to the Hermes container itself.

## Create or Select a Google Cloud Project

Open the Google Cloud Console and create a new project or select an existing project for Local Agent Concierge.

The project is used only to configure Google Calendar API access and OAuth credentials.

## Enable the Google Calendar API

In the selected Google Cloud project, open:

```text
APIs & Services
    |
    v
Library
```

Search for:

```text
Google Calendar API
```

Enable the API.

## Configure the OAuth Consent Screen

Open the OAuth configuration for the Google Cloud project.

Configure the consent screen for the Google account that will authorize calendar access.

For a personal development environment, the application can remain limited to test users if Google requires the application to remain in testing mode.

Add the Google account that owns or can access the target calendar as a test user when necessary.

## Create OAuth Client Credentials

Create a new OAuth client.

Choose:

```text
Application type: Desktop app
```

The Google Calendar bootstrap process uses the installed-application OAuth flow.

Download the generated client credentials JSON file.

The downloaded file may have a generated name such as:

```text
client_secret_<identifier>.json
```

Rename or copy it to:

```text
credentials.json
```

## OAuth Scopes

The Google Calendar MCP server currently requests the following scopes:

```text
https://www.googleapis.com/auth/calendar.events.readonly
https://www.googleapis.com/auth/calendar.events.freebusy
```

These permissions allow the service to read event information and availability without granting calendar write access.

The scopes are defined in:

```text
mcp/google-calendar/src/google_calendar_mcp/config.py
```

## Prepare the Local Credential Directory

Google OAuth credentials and tokens are stored under:

```text
data/google-calendar/
```

This directory is excluded from Git.

Create the directory if necessary:

```bash
mkdir -p data/google-calendar
```

If Docker created the directory as `root`, change its ownership:

```bash
sudo chown -R "$USER:$USER" data/google-calendar
```

Restrict access to the directory:

```bash
chmod 700 data/google-calendar
```

Copy the downloaded OAuth credentials to:

```text
data/google-calendar/credentials.json
```

For example:

```bash
cp /path/to/credentials.json \
  data/google-calendar/credentials.json
```

Restrict access to the file:

```bash
chmod 600 data/google-calendar/credentials.json
```

Verify the file:

```bash
ls -l data/google-calendar
```

Do not commit this file.

## Build the Google Calendar MCP Image

Build the service:

```bash
docker compose build google-calendar-mcp
```

Start it:

```bash
docker compose up -d google-calendar-mcp
```

Check its status:

```bash
docker compose ps google-calendar-mcp
```

The service should eventually report:

```text
healthy
```

Inspect logs if necessary:

```bash
docker compose logs --tail=100 google-calendar-mcp
```

## Run the Initial OAuth Authorization

The initial authorization process creates:

```text
data/google-calendar/token.json
```

The bootstrap command starts a temporary HTTP callback listener on port `8080`.

When the Docker host is a remote VM, such as a Google Compute Engine VM, forward the callback port from the local workstation to the VM.

### Create the SSH Tunnel

If Docker is running on a remote host, create an SSH tunnel from the local workstation to the remote host before starting the OAuth bootstrap.

Open a separate terminal on the local workstation and run:

```bash
ssh -L 8080:localhost:8080 <user>@<remote-host>
```

Replace:

```text
<user>
```

with the SSH user on the remote host, and:

```text
<remote-host>
```

with the hostname or IP address of the machine running Docker Compose.

For example, the connection conceptually forwards:

```text
Local workstation
localhost:8080
    |
    | SSH tunnel
    v
Remote Docker host
localhost:8080
```

Keep this SSH session open during the OAuth authorization flow.

If the remote machine is managed through a cloud provider, use that provider's SSH mechanism as long as it supports equivalent local port forwarding from port `8080` to `localhost:8080` on the remote host.

### Start the OAuth Bootstrap

On the VM, run:

```bash
docker compose run --rm \
  -p 127.0.0.1:8080:8080 \
  google-calendar-mcp \
  python -m google_calendar_mcp.bootstrap
```

The command prints a Google authorization URL.

Open the URL in a browser on the local workstation.

Sign in with the Google account that owns or can access the calendar and approve the requested read-only permissions.

The browser redirects to:

```text
http://localhost:8080/
```

The SSH tunnel forwards the callback to the bootstrap process running inside the remote VM.

After authorization succeeds, the bootstrap process creates:

```text
data/google-calendar/token.json
```

## Verify the OAuth Token

Check that the token exists:

```bash
ls -l data/google-calendar/token.json
```

The file should have restrictive permissions.

If necessary:

```bash
chmod 600 data/google-calendar/token.json
```

Never commit:

```text
credentials.json
token.json
```

The refresh token stored in `token.json` allows the application to renew Google access without repeating the browser authorization flow each time.

## Restart the Google Calendar MCP Service

After creating `token.json`, make sure the normal service is running:

```bash
docker compose up -d google-calendar-mcp
```

Verify:

```bash
docker compose ps google-calendar-mcp
```

## Verify Google Calendar API Access

Before testing Hermes Agent, verify the Calendar client directly.

Run:

```bash
docker compose exec -T google-calendar-mcp python - <<'PY'
import json

from google_calendar_mcp.calendar_client import list_upcoming_events

events = list_upcoming_events(max_results=5)

print(
    json.dumps(
        events,
        ensure_ascii=False,
        indent=2,
    )
)
PY
```

The command should return actual upcoming events from the primary Google Calendar.

Using `-T` is important when input is supplied through a shell heredoc.

Without it, Docker Compose may report an error similar to:

```text
cannot attach stdin to a TTY-enabled container because stdin is not a terminal
```

## Google Calendar MCP Tools

The MCP server currently provides the following tools:

```text
get_server_status
get_current_datetime
list_upcoming_events
list_events
list_busy_periods
list_free_periods
```

`list_events` requires ISO 8601 date-time values containing time-zone information.

For example:

```text
2026-08-10T09:00:00+01:00
```

Calendar events are retrieved from Google Calendar when requested. They should not be treated as long-term agent memory.

## Configure Hermes Agent

Hermes Agent connects to external MCP servers through `mcp_servers` in its configuration.

The persistent Hermes configuration is stored locally under:

```text
data/hermes/config.yaml
```

Add the Google Calendar MCP server:

```yaml
mcp_servers:
  google_calendar:
    url: "http://google-calendar-mcp:8000/mcp"
    timeout: 120
    connect_timeout: 30
```

If `mcp_servers` already exists, add `google_calendar` under the existing block instead of creating a second top-level `mcp_servers` key.

## Local Model Tool Search Compatibility

Hermes Agent can use progressive tool disclosure through its Tool Search feature.

When enabled, MCP tools may be exposed indirectly through bridge tools such as:

```text
tool_search
tool_describe
tool_call
```

During testing with the local `gemma4:12b` model, the model successfully discovered the Google Calendar tool but occasionally produced only reasoning after `tool_describe` instead of issuing the next tool call.

For the current small tool set, Tool Search is therefore disabled:

```yaml
tools:
  tool_search:
    enabled: off
```

If a top-level `tools` block already exists, add `tool_search` under that existing block.

Disabling Tool Search does not disable Hermes tools or MCP tools. It exposes the available MCP tool definitions directly to the model instead of requiring discovery through the bridge tools.

This setting can be reconsidered if the number of MCP or plugin tools becomes large or if the local model's multi-step tool-calling reliability improves.

No custom Hermes Agent container image is required for the current setup.

## Restart Hermes Agent

After updating the Hermes configuration, recreate the service:

```bash
docker compose up -d --force-recreate hermes-agent
```

Inspect the logs:

```bash
docker compose logs --tail=200 hermes-agent
```

To focus on MCP-related messages:

```bash
docker compose logs hermes-agent \
  | grep -iE 'mcp|google_calendar|google-calendar'
```

## Verify Calendar Access Through Hermes

Test Hermes directly before testing Slack:

```bash
docker compose run --rm hermes-agent \
  chat \
  --quiet \
  -q "Use Google Calendar and tell me my next calendar event."
```

The returned event should match the real Google Calendar data.

Additional tests include:

```text
What are my next five calendar events?
```

```text
What does my calendar look like tomorrow?
```

```text
When am I busy tomorrow?
```

```text
When am I free tomorrow?
```

These requests exercise the main read-only Calendar tools.

## Verify the Complete Slack Flow

Start the complete application:

```bash
docker compose up -d
```

Check all services:

```bash
docker compose ps
```

The relevant communication path is:

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
Google Calendar MCP
    |
    v
Google Calendar API
```

Send a new direct message to the Slack application:

```text
What is my next calendar event?
```

The response should match the actual next event in Google Calendar.

Also verify:

```text
What does my calendar look like tomorrow?
```

```text
When am I busy tomorrow?
```

```text
When am I free tomorrow?
```

A successful response confirms the complete end-to-end Calendar read path.

## Security

Google Calendar contains personal data and must be treated as an authoritative external data source rather than copied into the repository.

Never commit:

* Google OAuth client credentials
* Google OAuth access tokens
* Google OAuth refresh tokens
* Personal calendar event data
* Debug logs containing calendar data

The repository `.gitignore` excludes:

```text
data/
credentials.json
client_secret*.json
token.json
```

Verify that local credential files are ignored:

```bash
git check-ignore -v \
  data/google-calendar/credentials.json \
  data/google-calendar/token.json
```

Neither file should appear in:

```bash
git status
```

## Troubleshooting

### `data/google-calendar` Is Owned by Root

If a command such as:

```bash
chmod 700 data/google-calendar
```

returns:

```text
Operation not permitted
```

Docker may have created the bind-mounted directory as `root`.

Fix ownership:

```bash
sudo chown -R "$USER:$USER" data/google-calendar
```

Then apply restrictive permissions again.

### OAuth Browser Cannot Reach the Callback

When Docker is running on a remote host, the OAuth redirect targets:

```text
http://localhost:8080/
```

The browser interprets `localhost` as the local workstation, while the OAuth bootstrap listener is running on the remote Docker host.

Create an SSH tunnel from the workstation:

```bash
ssh -L 8080:localhost:8080 <user>@<remote-host>
```

Keep the SSH connection open and then rerun the OAuth bootstrap command.

Any SSH client or cloud-provider SSH command may be used as long as it forwards:

```text
local localhost:8080
        |
        v
remote localhost:8080
```

### Docker Compose Reports a TTY Error

If a heredoc test returns:

```text
cannot attach stdin to a TTY-enabled container because stdin is not a terminal
```

use:

```bash
docker compose exec -T ...
```

The `-T` option disables pseudo-TTY allocation.

### Hermes Discovers the Calendar Tool but Does Not Call It

A local model may produce reasoning such as:

```text
Next, I need to actually call the tool...
```

without issuing the actual tool call.

Hermes logs may then contain messages similar to:

```text
Thinking-only response
Empty response from model
```

For the currently tested `gemma4:12b` configuration, disable progressive Tool Search:

```yaml
tools:
  tool_search:
    enabled: off
```

Then recreate Hermes:

```bash
docker compose up -d --force-recreate hermes-agent
```

### Calendar MCP Is Unhealthy

Check:

```bash
docker compose ps google-calendar-mcp
```

and:

```bash
docker compose logs --tail=200 google-calendar-mcp
```

Also verify that both credential files exist:

```bash
ls -l data/google-calendar
```

The expected files are:

```text
credentials.json
token.json
```

## Current Limitations

The current Google Calendar integration is intentionally limited to read operations.

It does not currently:

* Create events
* Update events
* Delete events
* Send invitations
* Modify attendee lists

Calendar write access will be introduced only together with an explicit human-approval workflow.
