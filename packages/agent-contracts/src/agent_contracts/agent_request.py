from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True)
class AgentRequest:
    """An immutable, normalized request handed to an Agent implementation.

    Mirrors the "Agent Contract" request shape in docs/architecture.md.
    This is intentionally the first, bounded slice of that contract:
    identifiers, the instruction text, opaque memory-scope/permission
    strings, and an optional trace id. It does not interpret identifier
    formats, memory-scope grammar, permission taxonomy, or trace id
    formats — none of those are defined anywhere in this repository yet,
    so nothing here assumes a shape beyond "non-empty string".
    """

    task_id: str
    user_id: str
    conversation_id: str
    instruction: str
    memory_scopes: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    trace_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.task_id, "task_id")
        _require_non_empty_str(self.user_id, "user_id")
        _require_non_empty_str(self.conversation_id, "conversation_id")
        _require_non_empty_str(self.instruction, "instruction")

        # Defensively copy into a new tuple so later mutation of a caller's
        # own list cannot change an already-constructed request.
        object.__setattr__(
            self,
            "memory_scopes",
            _coerce_string_tuple(self.memory_scopes, "memory_scopes"),
        )
        object.__setattr__(
            self,
            "permissions",
            _coerce_string_tuple(self.permissions, "permissions"),
        )

        if self.trace_id is not None:
            _require_non_empty_str(self.trace_id, "trace_id")


_FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(AgentRequest))


def _require_non_empty_str(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _coerce_string_tuple(value: Any, name: str) -> tuple[str, ...]:
    # Restricted to list/tuple (rather than any Iterable) so a bare string
    # can't silently iterate into one-character entries, and a dict can't
    # silently collapse into just its keys.
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list or tuple of non-empty strings")

    for item in value:
        _require_non_empty_str(item, f"{name} entry")
    return tuple(value)


def agent_request_to_dict(request: AgentRequest) -> dict[str, Any]:
    """Return a JSON-compatible representation of an agent request."""
    return {
        "task_id": request.task_id,
        "user_id": request.user_id,
        "conversation_id": request.conversation_id,
        "instruction": request.instruction,
        "memory_scopes": list(request.memory_scopes),
        "permissions": list(request.permissions),
        "trace_id": request.trace_id,
    }


def agent_request_from_dict(data: Mapping[str, Any]) -> AgentRequest:
    """Reconstruct an agent request from its serialized representation.

    Rejects a non-mapping input, missing fields, and unknown fields, rather
    than silently ignoring or defaulting them — construction then applies
    the same validation as calling AgentRequest(...) directly.
    """
    if not isinstance(data, Mapping):
        raise ValueError("Agent request data must be a mapping")

    allowed_keys = set(_FIELD_NAMES)
    unknown_keys = set(data) - allowed_keys
    if unknown_keys:
        raise ValueError(
            f"Unsupported fields in agent request data: {sorted(unknown_keys)}"
        )

    missing_keys = allowed_keys - set(data)
    if missing_keys:
        raise ValueError(
            f"Missing fields in agent request data: {sorted(missing_keys)}"
        )

    return AgentRequest(
        task_id=data["task_id"],
        user_id=data["user_id"],
        conversation_id=data["conversation_id"],
        instruction=data["instruction"],
        memory_scopes=data["memory_scopes"],
        permissions=data["permissions"],
        trace_id=data["trace_id"],
    )
