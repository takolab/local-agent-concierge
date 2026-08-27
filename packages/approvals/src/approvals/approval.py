from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from approvals.proposed_action import (
    ProposedAction,
    action_from_dict,
    action_to_dict,
    require_exact_keys,
    require_tz_aware,
)


class ApprovalState(StrEnum):
    """Lifecycle states for a single approval request.

    PENDING is the only non-terminal state. APPROVED, REJECTED, and EXPIRED
    are terminal: once reached, no further transition is allowed out of
    them (see _ALLOWED_TRANSITIONS). There is no separate "executed" state
    here — recording execution results is a later Milestone 6 task that
    builds on top of this foundation, not part of it.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# The complete transition table. Every ApprovalState must have an entry
# (enforced by tests). An empty set means the state is terminal.
_ALLOWED_TRANSITIONS: dict[ApprovalState, frozenset[ApprovalState]] = {
    ApprovalState.PENDING: frozenset(
        {
            ApprovalState.APPROVED,
            ApprovalState.REJECTED,
            ApprovalState.EXPIRED,
        }
    ),
    ApprovalState.APPROVED: frozenset(),
    ApprovalState.REJECTED: frozenset(),
    ApprovalState.EXPIRED: frozenset(),
}

TERMINAL_STATES: frozenset[ApprovalState] = frozenset(
    state
    for state, allowed in _ALLOWED_TRANSITIONS.items()
    if not allowed
)


@dataclass(frozen=True)
class Approval:
    """A human decision request bound to one exact proposed action.

    Binds a ProposedAction to the requesting actor (`requested_by`) and the
    conversation it originated in (`conversation_id`) — two of the bindings
    docs/architecture.md's Human Approval section lists. It does not yet
    have a field for that section's separate "originating task" binding
    (`task_id` in the Agent Contract example): task identity has not been
    formalized anywhere in this repository yet, and is Milestone 7 scope
    ("Define the common agent request schema"). Approval token design,
    persistence, and expiration scheduling are also deliberately not part
    of this model yet — see docs/approval/domain-model.md.
    """

    action: ProposedAction
    requested_by: str
    conversation_id: str
    created_at: datetime
    state: ApprovalState = ApprovalState.PENDING

    def __post_init__(self) -> None:
        if not isinstance(self.action, ProposedAction):
            raise ValueError("action must be a ProposedAction")

        if (
            not isinstance(self.requested_by, str)
            or not self.requested_by.strip()
        ):
            raise ValueError("requested_by must be a non-empty string")

        if (
            not isinstance(self.conversation_id, str)
            or not self.conversation_id.strip()
        ):
            raise ValueError("conversation_id must be a non-empty string")

        if not isinstance(self.created_at, datetime):
            raise ValueError("created_at must be a datetime")

        require_tz_aware(self.created_at, "created_at")

        if not isinstance(self.state, ApprovalState):
            raise ValueError(f"Unsupported approval state: {self.state!r}")


def transition(approval: Approval, to_state: ApprovalState) -> Approval:
    """Return a new Approval moved to to_state.

    Enforces _ALLOWED_TRANSITIONS: raises ValueError instead of returning
    an Approval for any transition not explicitly allowed, including
    transitions out of a terminal state and same-state "transitions". The
    original Approval instance is untouched.
    """
    if not isinstance(to_state, ApprovalState):
        raise ValueError(f"Unsupported approval state: {to_state!r}")

    allowed = _ALLOWED_TRANSITIONS[approval.state]
    if to_state not in allowed:
        raise ValueError(
            f"Cannot transition approval from {approval.state.value!r} "
            f"to {to_state.value!r}"
        )

    return replace(approval, state=to_state)


def approve(approval: Approval) -> Approval:
    """Move a pending approval to the approved state."""
    return transition(approval, ApprovalState.APPROVED)


def reject(approval: Approval) -> Approval:
    """Move a pending approval to the rejected state."""
    return transition(approval, ApprovalState.REJECTED)


def expire(approval: Approval) -> Approval:
    """Move a pending approval to the expired state."""
    return transition(approval, ApprovalState.EXPIRED)


def is_terminal(state: ApprovalState) -> bool:
    """Return whether a state has no further allowed transitions."""
    return state in TERMINAL_STATES


def matches_action(approval: Approval, action: ProposedAction) -> bool:
    """Return whether action is exactly what this approval was bound to.

    ProposedAction is a frozen dataclass, so `==` is already an exact,
    field-by-field structural comparison — stable across processes and
    unaffected by PYTHONHASHSEED, since equality never depends on hash().
    A future executor is expected to call this immediately before
    performing a write and refuse to proceed when it returns False.
    """
    return approval.action == action


def approval_to_dict(approval: Approval) -> dict[str, Any]:
    """Return a JSON-compatible representation of an approval."""
    return {
        "action": action_to_dict(approval.action),
        "requested_by": approval.requested_by,
        "conversation_id": approval.conversation_id,
        "created_at": approval.created_at.isoformat(),
        "state": approval.state.value,
    }


def approval_from_dict(data: Mapping[str, Any]) -> Approval:
    """Reconstruct an approval from its serialized representation."""
    if not isinstance(data, Mapping):
        raise ValueError("Approval data must be a mapping")

    require_exact_keys(
        data,
        {"action", "requested_by", "conversation_id", "created_at", "state"},
        "approval data",
    )

    action_data = data["action"]
    if not isinstance(action_data, Mapping):
        raise ValueError("action must be a mapping")

    raw_created_at = data["created_at"]
    try:
        created_at = datetime.fromisoformat(raw_created_at)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "created_at must be a valid ISO 8601 date-time"
        ) from error

    raw_state = data["state"]
    try:
        state = ApprovalState(raw_state)
    except ValueError as error:
        raise ValueError(f"Unsupported approval state: {raw_state!r}") from error

    return Approval(
        action=action_from_dict(action_data),
        requested_by=data["requested_by"],
        conversation_id=data["conversation_id"],
        created_at=created_at,
        state=state,
    )
