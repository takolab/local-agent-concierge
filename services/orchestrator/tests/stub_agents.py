"""Stub Agent implementations used only by this package's own tests.

Neither class is production code and neither is registered anywhere
outside a test: `services/orchestrator/src` has no stub Agents of its own.
Both satisfy the `orchestrator.agent.Agent` Protocol structurally (a
matching `handle` method) without importing or inheriting from it.
"""

from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse


class RecordingAgent:
    """Returns a fixed AgentResponse and records every request it receives."""

    def __init__(self, response: AgentResponse) -> None:
        self.response = response
        self.calls: list[AgentRequest] = []

    def handle(self, request: AgentRequest) -> AgentResponse:
        self.calls.append(request)
        return self.response


class ExplodingAgent:
    """Always raises the exact exception instance it was constructed with."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def handle(self, request: AgentRequest) -> AgentResponse:
        raise self._error
