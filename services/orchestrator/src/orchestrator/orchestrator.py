from agent_contracts.agent_request import AgentRequest
from agent_contracts.agent_response import AgentResponse

from orchestrator.registry import AgentRegistry


class Orchestrator:
    """Dispatches a request to one explicitly named Agent.

    This is the first, bounded routing slice for Milestone 7: the caller
    names the target Agent explicitly. dispatch() does not inspect the
    request's instruction, classify it, or select an Agent automatically
    — see docs/orchestrator/domain-model.md for the rationale and for the
    later Milestone 7 work this intentionally leaves out.
    """

    def __init__(self, registry: AgentRegistry) -> None:
        self._registry = registry

    def dispatch(self, agent_name: str, request: AgentRequest) -> AgentResponse:
        """Look up agent_name, call its handle(request), and return the result.

        The request is passed through unmodified and the response is
        returned unwrapped. Exceptions raised by the Agent (including
        UnknownAgentError from an unregistered agent_name) propagate to
        the caller unchanged — this method does not catch, translate, or
        retry them.
        """
        agent = self._registry.get(agent_name)
        return agent.handle(request)
