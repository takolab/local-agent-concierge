"""Deterministic Agent implementations for local development and the
runtime HTTP smoke test -- NOT production Agents.

`EchoAgent` is registered by `orchestrator.__main__` purely so the
Orchestrator's HTTP runtime boundary (`GET /health`, `POST /dispatch`) has
at least one real, registered Agent to dispatch to when the container
starts. It performs no reasoning, calls no model, and is not a stand-in
for Hermes Agent or any other future Agent implementation.

This module exists only to make the HTTP <-> Orchestrator.dispatch() path
independently verifiable (container health checks, CI runtime smoke
tests, local manual curl) without connecting to Hermes Agent or any other
real Agent. A production Agent registration mechanism is a separate,
later Milestone 7 task and is not designed here -- see
docs/orchestrator/domain-model.md's "Synthetic Agent" section.
"""

from __future__ import annotations

from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse

DEV_ECHO_AGENT_NAME = "dev-echo"


class EchoAgent:
    """Echoes the request's instruction back in the response summary.

    Deterministic and side-effect-free: the same AgentRequest always
    produces the same AgentResponse. Satisfies orchestrator.agent.Agent
    structurally. Development/smoke-test use only -- see this module's
    docstring.
    """

    def handle(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            status="completed",
            summary=f"echo: {request.instruction}",
        )
