from collections.abc import Mapping
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class AgentResponse:
    """An immutable, normalized response returned by an Agent implementation.

    Mirrors the response shape shown in docs/architecture.md's Agent
    Contract example: `status`, `summary`, `proposed_actions`, and
    `memory_candidates`. This is a **provisional** domain representation —
    the minimal schema matching that example today, not a final or
    wire-level Agent Contract; docs/architecture.md itself notes the exact
    schema will evolve. `proposed_actions` and `memory_candidates` are
    treated as opaque string-to-string mappings here: this module does not
    depend on packages/approvals.ProposedAction, enforce a `status`
    vocabulary, or anticipate the eventual transport schema. See
    docs/agent-contracts/domain-model.md for what is deliberately deferred.
    """

    status: str
    summary: str
    proposed_actions: tuple[Mapping[str, str], ...] = ()
    memory_candidates: tuple[Mapping[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_str(self.status, "status")
        _require_non_empty_str(self.summary, "summary")

        # Defensively copy into a new tuple of read-only mappings so later
        # mutation of a caller's own list — or of a dict inside it — cannot
        # change an already-constructed response.
        object.__setattr__(
            self,
            "proposed_actions",
            _coerce_entry_tuple(self.proposed_actions, "proposed_actions"),
        )
        object.__setattr__(
            self,
            "memory_candidates",
            _coerce_entry_tuple(self.memory_candidates, "memory_candidates"),
        )


_FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(AgentResponse))


def _require_non_empty_str(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _coerce_entry_tuple(value: Any, name: str) -> tuple[Mapping[str, str], ...]:
    # Restricted to list/tuple (rather than any Iterable) so a single
    # mapping, or a bare string, can't be silently misread as the outer
    # collection.
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list or tuple of mappings")

    return tuple(_coerce_entry_mapping(item, f"{name} entry") for item in value)


def _coerce_entry_mapping(value: Any, name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")

    copied: dict[str, str] = {}
    for key, entry_value in value.items():
        _require_non_empty_str(key, f"{name} key")
        _require_non_empty_str(entry_value, f"{name} value")
        copied[key] = entry_value
    return MappingProxyType(copied)


def agent_response_to_dict(response: AgentResponse) -> dict[str, Any]:
    """Return a JSON-compatible representation of an agent response."""
    return {
        "status": response.status,
        "summary": response.summary,
        "proposed_actions": [dict(entry) for entry in response.proposed_actions],
        "memory_candidates": [dict(entry) for entry in response.memory_candidates],
    }


def agent_response_from_dict(data: Mapping[str, Any]) -> AgentResponse:
    """Reconstruct an agent response from its serialized representation.

    Rejects a non-mapping input, missing fields, and unknown fields, rather
    than silently ignoring or defaulting them — construction then applies
    the same validation as calling AgentResponse(...) directly.
    """
    if not isinstance(data, Mapping):
        raise ValueError("Agent response data must be a mapping")

    allowed_keys = set(_FIELD_NAMES)
    unknown_keys = set(data) - allowed_keys
    if unknown_keys:
        raise ValueError(
            f"Unsupported fields in agent response data: {sorted(unknown_keys)}"
        )

    missing_keys = allowed_keys - set(data)
    if missing_keys:
        raise ValueError(
            f"Missing fields in agent response data: {sorted(missing_keys)}"
        )

    return AgentResponse(
        status=data["status"],
        summary=data["summary"],
        proposed_actions=data["proposed_actions"],
        memory_candidates=data["memory_candidates"],
    )
