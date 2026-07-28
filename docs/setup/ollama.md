# Ollama Setup

This document explains how to run Ollama with Docker Compose, download the Gemma 4 12B model, verify GPU access, and confirm that model data persists across container recreation.

## Overview

Ollama runs as a Docker container and provides the local LLM inference service for Local Agent Concierge.

The initial model is:

```text
gemma4:12b
```

The initial context length is:

```text
8192 tokens
```

The local architecture is:

```text
Host
  |
  | http://localhost:11434
  v
Ollama container
  |
  +-- NVIDIA GPU
  |
  +-- ollama-data Docker volume
        |
        +-- gemma4:12b
```

Future containers, such as Hermes Agent, will connect to Ollama through the internal Docker Compose network:

```text
http://ollama:11434
```

## Prerequisites

The following software is required:

* Docker Engine
* Docker Compose
* An NVIDIA GPU
* NVIDIA drivers
* NVIDIA Container Toolkit

Check Docker:

```bash
docker --version
```

Check Docker Compose:

```bash
docker compose version
```

Check the NVIDIA GPU on the host:

```bash
nvidia-smi
```

Check that Docker containers can access the GPU:

```bash
docker run --rm --gpus all ubuntu nvidia-smi
```

The command may download the `ubuntu:latest` image the first time it is executed.

The GPU test is successful when NVIDIA GPU, driver, and CUDA information are displayed from inside the container.

To display only the detected GPU devices:

```bash
docker run --rm --gpus all ubuntu nvidia-smi -L
```

## Docker Compose Configuration

The Ollama service is defined in the repository root:

```text
docker-compose.yml
```

The configuration includes:

* The official Ollama container image
* NVIDIA GPU access
* An 8192-token context length
* A localhost-only API port
* A persistent Docker volume
* A dedicated Docker network
* A health check

The API is exposed on:

```text
http://localhost:11434
```

The port is bound to `127.0.0.1`, so the Ollama API is not intentionally exposed to other machines on the network.

The model files are stored in the named Docker volume:

```text
ollama-data
```

## Validate the Compose Configuration

Run the following command from the repository root:

```bash
docker compose config
```

This validates the Compose file and displays its resolved configuration.

List the defined services:

```bash
docker compose config --services
```

Expected output:

```text
ollama
```

## Pull the Ollama Container Image

Download the container image:

```bash
docker compose pull ollama
```

## Start Ollama

Start the Ollama service in detached mode:

```bash
docker compose up -d ollama
```

Check the service status:

```bash
docker compose ps
```

The health status may initially be:

```text
health: starting
```

After startup completes, it should become:

```text
healthy
```

## Inspect Logs

Display recent Ollama logs:

```bash
docker compose logs --tail=100 ollama
```

Follow logs in real time:

```bash
docker compose logs -f ollama
```

Press `Ctrl+C` to stop following the logs. This does not stop the Ollama container.

To search for GPU-related log entries:

```bash
docker compose logs ollama | grep -iE "cuda|gpu|nvidia|vram"
```

## Verify the Ollama API

Check that the Ollama API is reachable from the host:

```bash
curl http://localhost:11434/api/tags
```

Before a model is downloaded, the response should contain an empty model list similar to:

```json
{
  "models": []
}
```

A JSON response confirms that the Ollama API is running.

## Download Gemma 4 12B

Download the model into the Ollama container:

```bash
docker compose exec ollama ollama pull gemma4:12b
```

The model is stored in the `ollama-data` Docker volume rather than in the disposable container filesystem.

List the downloaded models:

```bash
docker compose exec ollama ollama list
```

The output should include:

```text
gemma4:12b
```

## Test the Model from the CLI

Send a single prompt:

```bash
docker compose exec ollama \
  ollama run gemma4:12b \
  "Reply with exactly: Local Agent Concierge is ready."
```

Expected response:

```text
Local Agent Concierge is ready.
```

Start an interactive session:

```bash
docker compose exec ollama ollama run gemma4:12b
```

Example prompt:

```text
What are the responsibilities of a personal AI concierge?
```

Exit the interactive session:

```text
/bye
```

## Test the Chat API

Send a chat request through the Ollama API:

```bash
curl -s http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma4:12b",
    "messages": [
      {
        "role": "system",
        "content": "You are a helpful personal AI concierge."
      },
      {
        "role": "user",
        "content": "Explain your role in two short sentences."
      }
    ],
    "stream": false,
    "options": {
      "num_ctx": 8192
    }
  }' | python3 -m json.tool
```

A successful response contains fields similar to:

```json
{
  "model": "gemma4:12b",
  "message": {
    "role": "assistant",
    "content": "..."
  },
  "done": true
}
```

If `jq` is installed, display only the generated response:

```bash
curl -s http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma4:12b",
    "messages": [
      {
        "role": "user",
        "content": "Reply with exactly: API test successful."
      }
    ],
    "stream": false,
    "options": {
      "num_ctx": 8192
    }
  }' | jq -r '.message.content'
```

## Check CPU and GPU Usage

After running the model, inspect how Ollama loaded it:

```bash
docker compose exec ollama ollama ps
```

Check the `PROCESSOR` column.

Possible examples include:

```text
100% GPU
```

```text
100% CPU
```

```text
40%/60% CPU/GPU
```

A mixed CPU/GPU result is expected when the complete model does not fit into GPU VRAM.

Monitor the NVIDIA GPU from another terminal:

```bash
watch -n 1 nvidia-smi
```

Press `Ctrl+C` to stop monitoring.

A more focused command is:

```bash
watch -n 1 \
  'nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader'
```

## Verify Model Persistence

Stop and remove the container:

```bash
docker compose down
```

Start Ollama again:

```bash
docker compose up -d ollama
```

Wait until the service is healthy:

```bash
docker compose ps
```

Verify that the model remains available:

```bash
docker compose exec ollama ollama list
```

If `gemma4:12b` is still listed, the `ollama-data` volume is working correctly.

A normal `docker compose down` removes the container and network but preserves the named volume.

Do not run the following command unless the Ollama models should also be deleted:

```bash
docker compose down --volumes
```

## Common Commands

Start Ollama:

```bash
docker compose up -d ollama
```

Check service status:

```bash
docker compose ps
```

Display logs:

```bash
docker compose logs -f ollama
```

List models:

```bash
docker compose exec ollama ollama list
```

List running models:

```bash
docker compose exec ollama ollama ps
```

Run Gemma 4 12B:

```bash
docker compose exec ollama ollama run gemma4:12b
```

Stop the environment:

```bash
docker compose down
```

Restart Ollama:

```bash
docker compose restart ollama
```

Inspect the Ollama volume:

```bash
docker volume ls | grep ollama
```

## Troubleshooting

### Ollama Container Is Not Healthy

Check the current status:

```bash
docker compose ps
```

Inspect the logs:

```bash
docker compose logs --tail=200 ollama
```

Restart the service:

```bash
docker compose restart ollama
```

### The API Returns Connection Refused

Confirm that the container is running:

```bash
docker compose ps
```

Check whether port `11434` is listening:

```bash
ss -lntp | grep 11434
```

Check the API again:

```bash
curl http://localhost:11434/api/tags
```

### Port 11434 Is Already in Use

Check which process is using the port:

```bash
sudo ss -lntp | grep 11434
```

A separately installed Ollama service may already be running on the host.

Check for an Ollama system service:

```bash
systemctl status ollama
```

Check running containers:

```bash
docker ps
```

Only one service can bind to the same host IP address and port at the same time.

### Docker Cannot Access the GPU

Run the standalone GPU test:

```bash
docker run --rm --gpus all ubuntu nvidia-smi
```

Check that NVIDIA Container Toolkit is configured for Docker.

Restart Docker after changing the NVIDIA runtime configuration:

```bash
sudo systemctl restart docker
```

Then recreate the Ollama container:

```bash
docker compose down
docker compose up -d ollama
```

### The Model Runs Mostly on the CPU

Inspect the model placement:

```bash
docker compose exec ollama ollama ps
```

Gemma 4 12B may not fit completely into GPUs with limited VRAM.

Possible ways to reduce memory usage include:

* Keep the context length at 8192
* Avoid loading multiple models simultaneously
* Stop unused applications that consume GPU memory
* Allow Ollama to use a mixture of GPU VRAM and system RAM

### Remove the Model

Remove only Gemma 4 12B:

```bash
docker compose exec ollama ollama rm gemma4:12b
```

This frees the model storage without deleting the complete Docker volume.

### Remove All Ollama Data

Stop the environment and remove its volumes:

```bash
docker compose down --volumes
```

This deletes downloaded models stored in the Compose-managed Ollama volume.

Use this command only when a complete reset is intended.

## Security Notes

The Ollama API does not need to be exposed publicly for local development.

The current port binding is:

```text
127.0.0.1:11434:11434
```

This permits access from the local host while avoiding an intentional bind to every network interface.

Future Docker Compose services should access Ollama through the internal service hostname:

```text
http://ollama:11434
```

Secrets, personal prompts, model outputs, and observability traces must not be committed to the public Git repository.

## Completion Checklist

* [x] Docker Compose configuration is valid
* [x] Docker containers can access the NVIDIA GPU
* [x] Ollama starts successfully
* [x] The Ollama health check passes
* [x] Gemma 4 12B is downloaded
* [x] The CLI returns a model response
* [x] The Chat API returns a model response
* [x] CPU and GPU usage can be inspected
* [x] The model survives container recreation

