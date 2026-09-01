from typing import Protocol

from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse


class Agent(Protocol):
    """Structural contract satisfied by any Agent implementation.

    Any object with a matching `handle` method satisfies this Protocol —
    no inheritance required. This is intentionally the same AgentRequest /
    AgentResponse shape already defined in `agent_contracts`; it is not a
    new schema. See docs/orchestrator/domain-model.md for how Orchestrator
    and AgentRegistry build on top of this contract.
    """

    def handle(self, request: AgentRequest) -> AgentResponse: ...
