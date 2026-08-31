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
    treated as opaque mappings with non-empty string keys and any
    JSON-compatible value (str, int, float, bool, None, or a nested
    list/mapping of those) — deliberately permissive rather than
    interpreting the content, so e.g. a real
    `packages.approvals.ProposedAction`'s serialized shape (a nullable
    `target_event_id`, a nested `parameters` mapping) fits without this
    module depending on `packages/approvals`. This module also does not
    enforce a `status` vocabulary or anticipate the eventual transport
    schema. See docs/agent-contracts/domain-model.md for what is
    deliberately deferred.
    """

    status: str
    summary: str
    proposed_actions: tuple[Mapping[str, Any], ...] = ()
    memory_candidates: tuple[Mapping[str, Any], ...] = ()

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


def _coerce_entry_tuple(value: Any, name: str) -> tuple[Mapping[str, Any], ...]:
    # Restricted to list/tuple (rather than any Iterable) so a single
    # mapping, or a bare string, can't be silently misread as the outer
    # collection.
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list or tuple of mappings")

    return tuple(_coerce_entry_mapping(item, f"{name} entry") for item in value)


def _coerce_entry_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")

    return _coerce_json_mapping(value, name)


def _coerce_json_mapping(value: Mapping[Any, Any], name: str) -> Mapping[str, Any]:
    copied: dict[str, Any] = {}
    for key, item in value.items():
        _require_non_empty_str(key, f"{name} key")
        copied[key] = _coerce_json_value(item, f"{name}[{key!r}]")
    return MappingProxyType(copied)


def _coerce_json_value(value: Any, name: str) -> Any:
    # Deliberately permissive ("opaque data"): any JSON-compatible value is
    # accepted and recursively normalized into an immutable shape (lists
    # become tuples, mappings become read-only MappingProxyType copies), at
    # every nesting depth — this module never interprets what a value means.
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, (list, tuple)):
        return tuple(
            _coerce_json_value(item, f"{name}[{index}]")
            for index, item in enumerate(value)
        )

    if isinstance(value, Mapping):
        return _coerce_json_mapping(value, name)

    raise ValueError(
        f"{name} must be a JSON-compatible value "
        "(str, int, float, bool, None, list, or mapping)"
    )


def _to_plain_json_value(value: Any) -> Any:
    # Inverse of _coerce_json_value: unwraps the immutable internal shape
    # (MappingProxyType, tuple) back into plain dict/list for JSON output.
    if isinstance(value, Mapping):
        return {key: _to_plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain_json_value(item) for item in value]
    return value


def agent_response_to_dict(response: AgentResponse) -> dict[str, Any]:
    """Return a JSON-compatible representation of an agent response."""
    return {
        "status": response.status,
        "summary": response.summary,
        "proposed_actions": [
            _to_plain_json_value(entry) for entry in response.proposed_actions
        ],
        "memory_candidates": [
            _to_plain_json_value(entry) for entry in response.memory_candidates
        ],
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
