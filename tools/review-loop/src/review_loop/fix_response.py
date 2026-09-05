"""The Structured Fix Response contract a Coding Agent must satisfy.

The Structured Verdict (:mod:`review_loop.verdict`) answers *what is wrong*.
This contract answers *what was done about one finding*, and it is held to a
stricter standard, because a verdict is a claim recorded for a human to read
while a fix response is a claim about a tree that already changed.

Three ideas shape it:

* **One block per routed finding.** A fix turn may carry several findings, so
  unlike a verdict there may be several blocks -- but each names exactly one
  ``Finding ID`` and stands or falls on its own. Identity is never plural.
* **The outcome vocabulary is closed.** ``fixed``, ``unable_to_fix``,
  ``escalate``. A free-text status would make "did this work?" a reading
  comprehension problem, which is exactly what routing exists to remove.
* **The response is evidence to be checked, never to be believed.** Every
  field here is cross-examined against the actual working tree by
  :mod:`review_loop.fix_validation`. ``Files changed`` in particular is not
  informational: a claim that disagrees with ``git status`` fails the run.

The field that carries the correctness argument is ``Target head SHA``, for
the same reason ``Reviewed head SHA`` does in a verdict. A fix is only a fix
*of* the commit it started from. An abbreviated, absent, or merely plausible
SHA is not a weaker claim -- it is a claim about an unknown commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: A response block appears between these two lines. The agent may think out
#: loud around them; only delimited blocks are ever parsed.
FIX_RESPONSE_BEGIN = "BEGIN BOUNDED FIX RESPONSE v1"
FIX_RESPONSE_END = "END BOUNDED FIX RESPONSE v1"

#: How many findings one bounded turn will carry. Above this the turn is not
#: bounded in any meaningful sense and the verdict goes to a human instead.
DEFAULT_MAX_ROUTED_FINDINGS = 5

#: Bounds on one response. Not tuning knobs: an unreasonable value means the
#: output is not the thing the contract describes.
MAX_FIX_FIELD_CHARS = 4000
MAX_FILES_CHANGED = 50
MAX_PATH_CHARS = 300

#: A captured patch above this size is not a bounded fix.
MAX_PATCH_BYTES = 2_000_000


class FixOutcome(Enum):
    """What the Coding Agent says happened to one finding."""

    #: The required outcome is now true, and the tree shows it.
    FIXED = "fixed"
    #: The agent understood the finding and could not satisfy it.
    UNABLE_TO_FIX = "unable_to_fix"
    #: The finding is impossible, self-contradictory, or outside the scope it
    #: was routed with. This is the agent's only route back to a human, and it
    #: is deliberately not the same as failing.
    ESCALATE = "escalate"


class FixRunOutcome(Enum):
    """How one fix turn ended.

    The failure modes stay distinct because a later slice -- push, CI wait,
    re-review -- has to branch on them. "The agent could not fix it" and "the
    agent edited files it was not allowed to touch" both end without a usable
    fix, and they call for opposite responses.
    """

    #: Every routed finding came back ``fixed``, and the working tree agrees.
    FIX_APPLIED = "FIX_APPLIED"
    #: The verdict is valid and has nothing to route: an approval, or a
    #: verdict whose open findings are all already resolved. No agent ran.
    NO_ACTIONABLE_FINDINGS = "NO_ACTIONABLE_FINDINGS"
    #: ``--dry-run``: the routing request was built and shown. No workspace was
    #: created and no agent ran.
    ROUTING_PREPARED = "ROUTING_PREPARED"
    #: The verdict is valid but this slice will not route it: a Blocking
    #: finding, an ``escalate`` recommendation, more findings than one bounded
    #: turn admits, or a finding whose scope cannot be bounded. No agent ran.
    REVIEW_REQUIRES_HUMAN = "REVIEW_REQUIRES_HUMAN"
    #: The routing input is not a validated review this runner produced.
    ROUTING_INPUT_INVALID = "ROUTING_INPUT_INVALID"
    #: The agent's working directory is not a clean checkout of the target, so
    #: no agent was started. Distinct from CODING_AGENT_FAILED because nothing
    #: ran: the fault is in the workspace.
    CODING_AGENT_WORKSPACE_INVALID = "CODING_AGENT_WORKSPACE_INVALID"
    #: The agent process failed, timed out, or produced nothing usable.
    CODING_AGENT_FAILED = "CODING_AGENT_FAILED"
    #: Output could not be parsed, or failed a semantic rule of the contract.
    FIX_RESPONSE_MALFORMED = "FIX_RESPONSE_MALFORMED"
    #: A response describes a commit other than the routed target.
    FIX_TARGET_MISMATCH = "FIX_TARGET_MISMATCH"
    #: The responses do not correspond one-to-one with the routed findings.
    FIX_FINDING_MISMATCH = "FIX_FINDING_MISMATCH"
    #: The working tree disagrees with the response, or holds a change the
    #: routed scope did not permit.
    FIX_SCOPE_VIOLATION = "FIX_SCOPE_VIOLATION"
    #: The agent reported ``unable_to_fix`` for at least one finding, and
    #: escalated none. The turn was well-formed; the fix does not exist.
    FIX_NOT_APPLIED = "FIX_NOT_APPLIED"
    #: The agent reported ``escalate`` for at least one finding. A human is
    #: being asked a question, which outranks every other reportable outcome.
    FIX_ESCALATED = "FIX_ESCALATED"
    #: The captured patch could not be written where it was asked for.
    PATCH_WRITE_FAILED = "PATCH_WRITE_FAILED"
    #: The working tree held a valid fix, but its diff was larger than a
    #: bounded fix may be, so no patch was captured. The worktree is removed
    #: when the run ends, so this turn produced a change nobody can retrieve
    #: -- which is not a success, whatever the response said.
    PATCH_TOO_LARGE = "PATCH_TOO_LARGE"


#: Exit code per outcome, in a block of their own so that no fix outcome can
#: be confused with a verification verdict (0-20) or a review outcome (30-35).
#:
#: Zero means *there is nothing left for this step to do*: either a validated
#: fix exists, or the review gave this step nothing to act on. Every other
#: value is a distinct reason, so an automation can branch on why.
FIX_EXIT_CODES: dict[FixRunOutcome, int] = {
    FixRunOutcome.FIX_APPLIED: 0,
    FixRunOutcome.NO_ACTIONABLE_FINDINGS: 0,
    FixRunOutcome.ROUTING_PREPARED: 0,
    FixRunOutcome.REVIEW_REQUIRES_HUMAN: 40,
    FixRunOutcome.ROUTING_INPUT_INVALID: 41,
    FixRunOutcome.CODING_AGENT_WORKSPACE_INVALID: 42,
    FixRunOutcome.CODING_AGENT_FAILED: 43,
    FixRunOutcome.FIX_RESPONSE_MALFORMED: 44,
    FixRunOutcome.FIX_TARGET_MISMATCH: 45,
    FixRunOutcome.FIX_FINDING_MISMATCH: 46,
    FixRunOutcome.FIX_SCOPE_VIOLATION: 47,
    FixRunOutcome.FIX_NOT_APPLIED: 48,
    FixRunOutcome.FIX_ESCALATED: 49,
    FixRunOutcome.PATCH_WRITE_FAILED: 50,
    FixRunOutcome.PATCH_TOO_LARGE: 51,
}


class FixResponseParseError(ValueError):
    """The agent's output is not a Structured Fix Response at all."""


class FixResponseValidationError(ValueError):
    """The output parsed, but the response it describes is not admissible."""


class FixTargetBindingError(FixResponseValidationError):
    """The response does not describe the exact commit the fix started from."""


class FixFindingBindingError(FixResponseValidationError):
    """The responses do not correspond to the findings that were routed."""


@dataclass(frozen=True)
class FixResponse:
    """One validated response, bound to one finding and one commit."""

    finding_id: str
    target_head_sha: str
    outcome: FixOutcome
    summary: str
    files_changed: tuple[str, ...] = ()
    #: What the agent ran and what it showed. Required for ``fixed``: a fix
    #: reported without any verification is an assertion.
    verification: str | None = None
    #: Why the agent could not fix, or what it is escalating. Required for
    #: every outcome that is not ``fixed``.
    reason: str | None = None
    scope_notes: str | None = None
