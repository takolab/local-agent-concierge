from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ActionType(StrEnum):
    """Calendar write operations a proposed action can represent."""

    CREATE_EVENT = "calendar.create_event"
    UPDATE_EVENT = "calendar.update_event"
    DELETE_EVENT = "calendar.delete_event"


# Parameter keys each action type accepts, and which of those are required.
# Every ActionType must have an entry in both maps (enforced by tests).
_REQUIRED_PARAMETERS: dict[ActionType, frozenset[str]] = {
    ActionType.CREATE_EVENT: frozenset({"title", "start", "end"}),
    ActionType.UPDATE_EVENT: frozenset(),
    ActionType.DELETE_EVENT: frozenset(),
}

_ALLOWED_PARAMETERS: dict[ActionType, frozenset[str]] = {
    ActionType.CREATE_EVENT: frozenset({"title", "start", "end"}),
    ActionType.UPDATE_EVENT: frozenset({"title", "start", "end"}),
    ActionType.DELETE_EVENT: frozenset(),
}

_DATETIME_PARAMETERS = frozenset({"start", "end"})


@dataclass(frozen=True)
class ProposedAction:
    """An exact, immutable description of a single calendar write to approve."""

    action_type: ActionType
    target_event_id: str | None
    parameters: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.action_type, ActionType):
            raise ValueError(
                f"Unsupported action type: {self.action_type!r}"
            )

        if not isinstance(self.parameters, Mapping):
            raise ValueError("parameters must be a mapping of strings")

        # Defensively copy so later mutation of the caller's dict, or the
        # mapping this wraps, cannot change an already-constructed action.
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(self.parameters)),
        )

        _validate_target(self.action_type, self.target_event_id)
        _validate_parameters(self.action_type, self.parameters)


def _validate_target(
    action_type: ActionType,
    target_event_id: str | None,
) -> None:
    if action_type is ActionType.CREATE_EVENT:
        if target_event_id is not None:
            raise ValueError(
                "target_event_id must be None for "
                f"{action_type.value}; the event does not exist yet"
            )
        return

    if not isinstance(target_event_id, str) or not target_event_id.strip():
        raise ValueError(
            f"target_event_id is required for {action_type.value}"
        )


def _validate_parameters(
    action_type: ActionType,
    parameters: Mapping[str, str],
) -> None:
    if not all(isinstance(key, str) for key in parameters):
        raise ValueError("parameter keys must be strings")

    allowed = _ALLOWED_PARAMETERS[action_type]
    required = _REQUIRED_PARAMETERS[action_type]

    unknown = set(parameters) - allowed
    if unknown:
        raise ValueError(
            f"Unsupported parameters for {action_type.value}: "
            f"{sorted(unknown)}"
        )

    missing = required - set(parameters)
    if missing:
        raise ValueError(
            f"Missing required parameters for {action_type.value}: "
            f"{sorted(missing)}"
        )

    # Keyed on the action type itself rather than derived from "allowed but
    # not required": UPDATE_EVENT is currently the only action type shaped
    # that way. If a second one is added later, generalize this to
    # `if allowed and not required and not parameters` instead of adding
    # another identity check here.
    if action_type is ActionType.UPDATE_EVENT and not parameters:
        raise ValueError(
            "calendar.update_event requires at least one changed "
            "parameter (title, start, or end)"
        )

    for key, value in parameters.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Parameter {key!r} must be a non-empty string")

    present_datetime_params = _DATETIME_PARAMETERS & set(parameters)
    parsed: dict[str, datetime] = {
        key: _parse_datetime_parameter(key, parameters[key])
        for key in present_datetime_params
    }

    if "start" in parsed and "end" in parsed:
        if parsed["end"] <= parsed["start"]:
            raise ValueError("end must be later than start")


def _parse_datetime_parameter(name: str, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(
            f"Parameter {name!r} must be a valid ISO 8601 date-time"
        ) from error

    require_tz_aware(parsed, name)
    return parsed


def require_tz_aware(value: datetime, name: str) -> None:
    """Raise ValueError unless value is a time-zone-aware datetime.

    Shared by proposed_action.py's start/end parameters and approval.py's
    created_at, which both need the same rule.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include time zone information")


def require_exact_keys(
    data: Mapping[str, Any],
    allowed_keys: set[str],
    label: str,
) -> None:
    """Raise ValueError unless data has exactly allowed_keys, no more, no less.

    Shared by action_from_dict and approval_from_dict.
    """
    unknown_keys = set(data) - allowed_keys
    if unknown_keys:
        raise ValueError(
            f"Unsupported fields in {label}: {sorted(unknown_keys)}"
        )

    missing_keys = allowed_keys - set(data)
    if missing_keys:
        raise ValueError(f"Missing fields in {label}: {sorted(missing_keys)}")


def describe_action(action: ProposedAction) -> str:
    """Return a human-readable summary of a proposed action."""
    if action.action_type is ActionType.CREATE_EVENT:
        return (
            f"Create calendar event '{action.parameters['title']}' "
            f"from {action.parameters['start']} to {action.parameters['end']}"
        )

    if action.action_type is ActionType.UPDATE_EVENT:
        changes = ", ".join(
            f"{key}={value}"
            for key, value in sorted(action.parameters.items())
        )
        return f"Update calendar event {action.target_event_id} ({changes})"

    return f"Delete calendar event {action.target_event_id}"


def action_to_dict(action: ProposedAction) -> dict[str, Any]:
    """Return a JSON-compatible representation of a proposed action."""
    return {
        "action_type": action.action_type.value,
        "target_event_id": action.target_event_id,
        "parameters": dict(action.parameters),
    }


def action_from_dict(data: Mapping[str, Any]) -> ProposedAction:
    """Reconstruct a proposed action from its serialized representation."""
    if not isinstance(data, Mapping):
        raise ValueError("Proposed action data must be a mapping")

    require_exact_keys(
        data,
        {"action_type", "target_event_id", "parameters"},
        "proposed action data",
    )

    raw_action_type = data["action_type"]
    try:
        action_type = ActionType(raw_action_type)
    except ValueError as error:
        raise ValueError(
            f"Unsupported action type: {raw_action_type!r}"
        ) from error

    return ProposedAction(
        action_type=action_type,
        target_event_id=data["target_event_id"],
        parameters=data["parameters"],
    )
