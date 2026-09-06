"""What the current evidence says the next workflow action is -- and nothing more.

Every stage before this one produced *facts*: a validated review, a bounded
fix, an exact pushed commit, authoritative CI, and a fresh re-review that said
what became of each original finding and what it found on its own. None of
them says what should happen to the pull request, and none of them may: that
is a human's decision.

This module sits in the one place between those two things, and its whole
design is the separation the loop would otherwise lose:

```text
facts            -- gathered, never derived from each other
routing          -- one deterministic function of those facts
human authority  -- untouched
```

:class:`DecisionFacts` is the first half. Its fields are deliberately six
lists of finding ids rather than a handful of booleans, because every boolean
this loop has tried has been ambiguous in the same way: ``major_findings_remain``
reads across the original findings and the fresh ones while counting only one
of them, so ``false`` beside an UNRESOLVED original Major finding is a lie
told by a field name. Ids say which findings, in which collection, and a
reader who wants a count takes ``len``.

:func:`classify` is the second half: a pure function of those facts, with no
input and no I/O of its own. Reading it is the whole specification of what
this runner is willing to conclude.

And that is where it stops. ``READY_FOR_HUMAN_MERGE_DECISION`` is not
``MERGE``; ``FIX_REQUIRED`` does not start a fix; ``HUMAN_ESCALATION`` does
not page anyone. They name the next *workflow* state so a human reading one
compact artifact knows what they are being asked, which is the opposite of
deciding on their behalf.

**Why the reviewer's own recommendation is not simply copied.** It is one
fact among several and it can only raise the classification, never lower it.
A re-review that recommends ``approved`` says nothing about whether the pull
request has moved since, whether its CI is still green, or whether the base
advanced underneath it -- and those are exactly the conditions under which a
recommendation is stale rather than wrong. Conversely a re-review that
recommends ``changes_requested`` because it raised a fresh *Minor* finding is
not a reason to withhold a merge decision from a human; it is a reason to put
that finding in front of them. So the recommendation is read for what only it
can say -- that the reviewer is escalating -- and the rest is derived from
the collections.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .rereview import ReReview, Resolution
from .verdict import Finding, Recommendation, Severity


class NextAction(Enum):
    """The mechanically-derived next workflow action. Never a decision.

    Three of these describe the pull request. The fourth describes *this
    runner's evidence about it*, and the difference is load-bearing: "a fresh
    Major finding exists" is a current answer, while "the head moved" is a
    refusal to answer at all. Collapsing the second into the first would
    report a state of the pull request that nobody established.
    """

    #: Every required piece of evidence is current and nothing in it prevents
    #: a human from considering a merge. It does **not** mean merge, and this
    #: runner has no code path that could perform one.
    READY_FOR_HUMAN_MERGE_DECISION = "READY_FOR_HUMAN_MERGE_DECISION"
    #: The evidence is current and says another bounded fix is the likely next
    #: workflow action. Nothing is started: no Coding Agent is invoked, no
    #: finding is routed, and no commit is made.
    FIX_REQUIRED = "FIX_REQUIRED"
    #: The evidence is current and requires a human's judgement before the
    #: loop can continue at all. Never downgraded to FIX_REQUIRED.
    HUMAN_ESCALATION = "HUMAN_ESCALATION"
    #: The re-review may have been valid when it was made, but it is no longer
    #: authoritative for the pull request's present state. No decision-ready
    #: brief is produced, and nothing is recorded.
    EVIDENCE_NOT_CURRENT = "EVIDENCE_NOT_CURRENT"


class DecisionOutcome(Enum):
    """How one merge-brief turn ended.

    Separate from :class:`NextAction` on purpose. The action is *what the
    evidence says*; the outcome is *what this run did about it*. A brief that
    classifies ``FIX_REQUIRED`` and a brief that classifies
    ``READY_FOR_HUMAN_MERGE_DECISION`` are both successful runs that recorded
    one artifact, and an operator branching on the exit code should not have
    to treat the second as a failure to get the first.
    """

    #: A current brief was recorded as a new comment (or, in a dry run, would
    #: have been). Its classification is in its content, not in this value.
    BRIEF_RECORDED = "BRIEF_RECORDED"
    #: A record for this exact identity already exists; nothing was written.
    COMMENT_ALREADY_EXISTS = "COMMENT_ALREADY_EXISTS"
    #: The inputs are not a validated review, the PUSH_READY push of its fix
    #: and the validated re-review of that push. No GitHub write was made.
    DECISION_INPUT_INVALID = "DECISION_INPUT_INVALID"
    #: The pull request has moved out from under the re-review. A diagnostic
    #: brief explains why no decision can be made; nothing is recorded.
    EVIDENCE_NOT_CURRENT = "EVIDENCE_NOT_CURRENT"
    #: The brief was current but the comment could not be created.
    GITHUB_WRITE_FAILED = "GITHUB_WRITE_FAILED"
    #: GitHub could not be queried.
    API_ERROR = "API_ERROR"


#: Exit code per outcome, in a block of their own so that no merge-brief
#: outcome can be confused with a verification verdict (0-20), a review
#: outcome (30-35), a fix outcome (40-51), a push outcome (60-75) or a
#: re-review outcome (80-88).
#:
#: Zero means *a current merge decision brief exists for this exact state*.
#: It does **not** mean the pull request may merge, and it does not even mean
#: the classification was ``READY_FOR_HUMAN_MERGE_DECISION``: what the brief
#: concluded is in the brief.
DECISION_EXIT_CODES: dict[DecisionOutcome, int] = {
    DecisionOutcome.BRIEF_RECORDED: 0,
    DecisionOutcome.COMMENT_ALREADY_EXISTS: 0,
    DecisionOutcome.DECISION_INPUT_INVALID: 90,
    DecisionOutcome.EVIDENCE_NOT_CURRENT: 91,
    DecisionOutcome.GITHUB_WRITE_FAILED: 92,
    DecisionOutcome.API_ERROR: 93,
}


class DecisionInputError(ValueError):
    """The inputs are not the validated evidence chain a brief is built from."""


@dataclass(frozen=True)
class DecisionFacts:
    """The evidence a classification is derived from, as explicit facts.

    Every finding-id field names both *which collection* it reads and *what
    it reports*, so no field can be read as saying something about the other
    collection. ``unresolved_original_finding_ids`` is history about round 1;
    ``fresh_major_finding_ids`` is this round's own review of the current
    state; and there is deliberately no field that sums them.

    ``evidence_not_current_reasons`` is the one field that is not about
    findings at all. It is empty exactly when the re-review still describes
    the pull request's present, verified state.
    """

    #: Original findings, by resolution. Together these are a partition of the
    #: round-1 finding ids: the re-review contract requires exactly one
    #: resolution per original finding, so nothing can fall out of all three.
    resolved_original_finding_ids: tuple[str, ...] = ()
    unresolved_original_finding_ids: tuple[str, ...] = ()
    escalated_original_finding_ids: tuple[str, ...] = ()
    #: Original findings that are not RESOLVED *and* were raised as Blocking
    #: in round 1. Reachable only through a hand-edited review document -- the
    #: review contract escalates a Blocking finding rather than requesting
    #: changes, and the re-review input refuses a review that did anything
    #: else -- but named here so "any Blocking finding escalates" is one rule
    #: over both collections rather than a property of where it was raised.
    unresolved_blocking_original_finding_ids: tuple[str, ...] = ()
    #: Findings this round raised, by severity. Fresh only, always.
    fresh_blocking_finding_ids: tuple[str, ...] = ()
    fresh_major_finding_ids: tuple[str, ...] = ()
    fresh_minor_finding_ids: tuple[str, ...] = ()
    #: What the re-reviewer recommended. Read for its escalation only.
    rereview_recommendation: Recommendation = Recommendation.APPROVED
    #: What the re-reviewer said it was asking a human, if anything.
    escalation_reason: str | None = None
    #: Why the re-review is no longer authoritative for the current state.
    #: Empty means it still is.
    evidence_not_current_reasons: tuple[str, ...] = ()

    @property
    def evidence_current(self) -> bool:
        return not self.evidence_not_current_reasons


@dataclass(frozen=True)
class Classification:
    """One next action, and the facts that produced it.

    The reasons are part of the result rather than something a renderer
    reconstructs: a human reading ``FIX_REQUIRED`` needs to know which of the
    several conditions that can produce it actually did.
    """

    next_action: NextAction
    reasons: tuple[str, ...]


def _ids(values) -> str:
    return ", ".join(values)


def gather_facts(
    original_findings: tuple[Finding, ...],
    rereview: ReReview,
    *,
    evidence_not_current_reasons: tuple[str, ...] = (),
) -> DecisionFacts:
    """Reduce one validated re-review to the facts a classification reads.

    ``original_findings`` supplies the severities, which the re-review itself
    does not carry: a resolution names a finding id and what became of it, and
    the severity that finding was raised at belongs to the round-1 record. So
    the two are joined here rather than either one being asked to hold both.
    """
    severities = {f.finding_id: f.severity for f in original_findings}

    by_resolution: dict[Resolution, list[str]] = {r: [] for r in Resolution}
    blocking_outstanding: list[str] = []
    for resolution in rereview.resolutions:
        by_resolution[resolution.resolution].append(resolution.finding_id)
        if (
            resolution.resolution is not Resolution.RESOLVED
            and severities.get(resolution.finding_id) is Severity.BLOCKING
        ):
            blocking_outstanding.append(resolution.finding_id)

    def fresh(severity: Severity) -> tuple[str, ...]:
        return tuple(
            f.finding_id for f in rereview.fresh_findings if f.severity is severity
        )

    return DecisionFacts(
        resolved_original_finding_ids=tuple(by_resolution[Resolution.RESOLVED]),
        unresolved_original_finding_ids=tuple(by_resolution[Resolution.UNRESOLVED]),
        escalated_original_finding_ids=tuple(by_resolution[Resolution.ESCALATE]),
        unresolved_blocking_original_finding_ids=tuple(blocking_outstanding),
        fresh_blocking_finding_ids=fresh(Severity.BLOCKING),
        fresh_major_finding_ids=fresh(Severity.MAJOR),
        fresh_minor_finding_ids=fresh(Severity.MINOR),
        rereview_recommendation=rereview.recommendation,
        escalation_reason=rereview.escalation_reason,
        evidence_not_current_reasons=tuple(evidence_not_current_reasons),
    )


def classify(facts: DecisionFacts) -> Classification:
    """Derive the next workflow action from the facts. A pure function.

    The order of the four branches is the specification, and each one is a
    rule this pipeline has already committed to elsewhere:

    1. **Currency first.** A re-review of a commit the pull request has moved
       off is historical evidence. Nothing about the findings is asked,
       because no answer about them would be about the current state.
    2. **Anything Blocking or escalated escalates.** The review contract has
       said since round 1 that a Blocking finding escalates rather than
       requesting changes; the same rule is applied here over both
       collections at once, and a reviewer that escalated explicitly is taken
       at its word.
    3. **Anything outstanding or freshly Major needs another fix.** This is
       the ``changes_requested`` condition, stated over the collections
       rather than copied from the recommendation.
    4. **Otherwise the human decides.** Fresh Minor findings do not withhold
       that decision -- they are surfaced in the brief for the human to
       accept or defer, which is a decision this runner does not hold.
    """
    if facts.evidence_not_current_reasons:
        return Classification(
            NextAction.EVIDENCE_NOT_CURRENT, facts.evidence_not_current_reasons
        )

    reasons: list[str] = []
    if facts.escalated_original_finding_ids:
        reasons.append(
            "the re-review escalated original finding(s) "
            f"{_ids(facts.escalated_original_finding_ids)}, which is a question for a "
            "human rather than a change to request"
        )
    if facts.fresh_blocking_finding_ids:
        reasons.append(
            f"the re-review raised fresh Blocking finding(s) "
            f"{_ids(facts.fresh_blocking_finding_ids)}; a Blocking finding always "
            "escalates"
        )
    if facts.unresolved_blocking_original_finding_ids:
        reasons.append(
            "original Blocking finding(s) "
            f"{_ids(facts.unresolved_blocking_original_finding_ids)} are still "
            "outstanding; a Blocking finding always escalates"
        )
    if facts.rereview_recommendation is Recommendation.ESCALATE:
        reasons.append(
            "the re-reviewer's own recommendation is 'escalate'"
            + (f": {facts.escalation_reason}" if facts.escalation_reason else "")
        )
    if reasons:
        return Classification(NextAction.HUMAN_ESCALATION, tuple(reasons))

    if facts.unresolved_original_finding_ids:
        reasons.append(
            "original finding(s) "
            f"{_ids(facts.unresolved_original_finding_ids)} are UNRESOLVED at the "
            "re-reviewed commit"
        )
    if facts.fresh_major_finding_ids:
        reasons.append(
            "the re-review raised fresh Major finding(s) "
            f"{_ids(facts.fresh_major_finding_ids)} against the current state"
        )
    if reasons:
        reasons.append(
            "no fix round is started by this command; whether one runs is a human's "
            "decision"
        )
        return Classification(NextAction.FIX_REQUIRED, tuple(reasons))

    ready = [
        "every original finding is RESOLVED",
        "no fresh Blocking or Major finding was raised",
        "the pull request is still at the re-reviewed commit, with authoritative CI "
        "READY against the current merge context",
    ]
    if facts.fresh_minor_finding_ids:
        ready.append(
            "fresh Minor finding(s) "
            f"{_ids(facts.fresh_minor_finding_ids)} are open and are listed above; "
            "accepting or deferring them is part of the human's decision, not a bar "
            "to making it"
        )
    return Classification(NextAction.READY_FOR_HUMAN_MERGE_DECISION, tuple(ready))
