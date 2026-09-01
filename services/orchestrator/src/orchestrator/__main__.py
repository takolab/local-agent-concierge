"""Orchestrator runtime entrypoint.

Builds an AgentRegistry containing only the development/smoke-test
EchoAgent (see orchestrator.dev_agents), constructs an Orchestrator
around it, and serves the HTTP runtime boundary defined in
orchestrator.http_server until terminated.

This does not implement production Agent registration/discovery, or any
connection to Hermes Agent or the Slack Gateway -- see
docs/orchestrator/domain-model.md for the current runtime boundaries.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from orchestrator.dev_agents import DEV_ECHO_AGENT_NAME, EchoAgent
from orchestrator.http_server import DEFAULT_HOST, DEFAULT_PORT, create_server
from orchestrator.orchestrator import Orchestrator
from orchestrator.registry import AgentRegistry

logger = logging.getLogger("orchestrator")


def build_orchestrator() -> Orchestrator:
    registry = AgentRegistry()
    registry.register(DEV_ECHO_AGENT_NAME, EchoAgent())
    return Orchestrator(registry)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    server = create_server(build_orchestrator(), DEFAULT_HOST, DEFAULT_PORT)

    logger.info(
        "Starting Orchestrator HTTP runtime on %s:%d (registered agents: %s)",
        DEFAULT_HOST,
        DEFAULT_PORT,
        DEV_ECHO_AGENT_NAME,
    )

    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()

    shutdown_event = threading.Event()

    def _handle_shutdown_signal(signum: int, frame: FrameType | None) -> None:
        logger.info("Received signal %d, shutting down", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    shutdown_event.wait()

    # shutdown() must be called from a thread other than the one running
    # serve_forever() -- this is that other thread, by design.
    server.shutdown()
    server.server_close()
    serve_thread.join(timeout=5)

    logger.info("Orchestrator HTTP runtime stopped")


if __name__ == "__main__":
    main()
