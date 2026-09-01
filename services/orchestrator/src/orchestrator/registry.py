from orchestrator.agent import Agent


class DuplicateAgentError(Exception):
    """Raised by AgentRegistry.register() when the name is already registered."""


class UnknownAgentError(Exception):
    """Raised by AgentRegistry.get() when the name has no registered Agent."""


class AgentRegistry:
    """An in-memory `name -> Agent` mapping.

    Deliberately minimal: no discovery, persistence, dynamic loading,
    configuration files, or network registration. See
    docs/orchestrator/domain-model.md for what this leaves out.
    """

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, name: str, agent: Agent) -> None:
        """Register agent under name.

        Raises ValueError if name is not a non-empty string, or if it has
        leading or trailing whitespace — otherwise " foo " and "foo" would
        silently become distinct registry keys. Raises DuplicateAgentError
        if name is already registered — in which case the existing
        registration is left unchanged.
        """
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError(
                "agent name must be a non-empty string with no leading or "
                "trailing whitespace"
            )

        if name in self._agents:
            raise DuplicateAgentError(f"Agent already registered: {name!r}")

        self._agents[name] = agent

    def get(self, name: str) -> Agent:
        """Return the Agent registered under name, or raise UnknownAgentError."""
        try:
            return self._agents[name]
        except KeyError:
            raise UnknownAgentError(f"Unknown agent: {name!r}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._agents
