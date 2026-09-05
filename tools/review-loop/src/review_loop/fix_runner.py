"""One bounded fix turn, routed from one validated review.

The order of the steps is the design, exactly as it is for the review turn --
and the ordering decisions here are the ones that keep "route a finding to an
agent" from quietly becoming "let an agent loose on a checkout".

**Gate before you prepare.** Whether a verdict may be routed at all is
decided first, from the verdict alone: an approval, a Blocking finding, an
``escalate`` recommendation or too many findings all stop the turn *before*
any fetch, any worktree, and any child process. Nothing is created for a
verdict that was never going to be acted on.

**Bind before you build the task.** The worktree comes next, and it is
prepared and verified by PR #32's machinery, unchanged. Only then is the
routing scope resolved -- against the target commit's own tree, so "this
path exists" means it exists in the commit under fix rather than in whatever
the operator happens to have checked out.

**Inspect before you read the answer.** When the agent returns, the working
tree is read *first*, and it is read for every outcome including a malformed
response. Reading the agent's answer first and the tree second would mean
deciding what to look for based on what the agent said it did, which is the
one thing the tree is there to check.

**Capture before you clean up.** The worktree is removed on every path,
success and failure alike, so the patch is taken while it still exists. A fix
this runner could not hand back is a fix that did not happen.

The whole turn performs **no GitHub request of any kind** -- not a read, not
a write. Target currency is established by git: the prepared worktree
resolves ``refs/pull/N/head`` from the remote and refuses anything that is
not the reviewed commit, which is the same fact a GitHub round-trip would
have established, obtained without a credential.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .agent_prompt import build_prompt
from .agent_workspace import WorkspaceInspection, inspect_workspace
from .fix_request import (
    FixRequest,
    RoutingDecision,
    ScopeError,
    build_request,
    select_findings,
)
from .fix_response import (
    DEFAULT_MAX_ROUTED_FINDINGS,
    FIX_EXIT_CODES,
    FixOutcome,
    FixResponseParseError,
    FixResponseValidationError,
    FixRunOutcome,
    FixTargetBindingError,
    FixFindingBindingError,
)
from .fix_response_parser import parse
from .fix_validation import ScopeViolation, ValidatedFix, validate
from .review_target import ReviewTarget
from .reviewer_workspace import WorkspaceError
from .verdict import ReviewVerdict


@dataclass(frozen=True)
class FixResult:
    """Everything one fix turn established, and what it did about it."""

    outcome: FixRunOutcome
    reasons: tuple[str, ...] = ()
    target: ReviewTarget | None = None
    request: FixRequest | None = None
    validated: ValidatedFix | None = None
    inspection: WorkspaceInspection | None = None
    agent_invoked: bool = False
    workspace_created: bool = False
    dry_run: bool = False
    patch_path: str | None = None
    agent_stdout: str = field(default="", repr=False)
    agent_stderr: str = field(default="", repr=False)

    @property
    def exit_code(self) -> int:
        return FIX_EXIT_CODES[self.outcome]

    @property
    def patch(self) -> str:
        return self.inspection.patch if self.inspection is not None else ""


def _aggregate(validated: ValidatedFix) -> tuple[FixRunOutcome, tuple[str, ...]]:
    """Turn per-finding outcomes into the turn's single outcome.

    Escalation outranks everything reportable. A turn that fixed two findings
    and escalated a third is an escalation: a human has been asked a
    question, and a green exit code would bury it.
    """
    escalated = [r for r in validated.responses if r.outcome is FixOutcome.ESCALATE]
    unable = [r for r in validated.responses if r.outcome is FixOutcome.UNABLE_TO_FIX]
    fixed = [r for r in validated.responses if r.outcome is FixOutcome.FIXED]

    if escalated:
        return FixRunOutcome.FIX_ESCALATED, tuple(
            f"{r.finding_id} escalated: {r.reason}" for r in escalated
        ) + tuple(f"{r.finding_id} was fixed" for r in fixed)
    if unable:
        return FixRunOutcome.FIX_NOT_APPLIED, tuple(
            f"{r.finding_id} was not fixed: {r.reason}" for r in unable
        ) + tuple(f"{r.finding_id} was fixed" for r in fixed)
    return FixRunOutcome.FIX_APPLIED, tuple(
        f"{r.finding_id} was fixed in "
        + ", ".join(r.files_changed)
        for r in fixed
    )


def run_fix(
    *,
    agent,
    workspace,
    target: ReviewTarget,
    verdict: ReviewVerdict,
    allow_paths: tuple[str, ...] = (),
    max_findings: int = DEFAULT_MAX_ROUTED_FINDINGS,
    dry_run: bool = False,
) -> FixResult:
    """Route one validated review's findings to one bounded Coding Agent turn."""

    # 1. May anything be routed at all? Decided from the verdict alone, so a
    #    verdict nobody can act on costs no fetch, no worktree, no process.
    selection = select_findings(verdict, max_findings=max_findings)
    if selection.decision is RoutingDecision.NOTHING_TO_DO:
        return FixResult(
            outcome=FixRunOutcome.NO_ACTIONABLE_FINDINGS,
            reasons=selection.reasons,
            target=target,
            dry_run=dry_run,
        )
    if selection.decision is RoutingDecision.REQUIRES_HUMAN:
        return FixResult(
            outcome=FixRunOutcome.REVIEW_REQUIRES_HUMAN,
            reasons=selection.reasons,
            target=target,
            dry_run=dry_run,
        )

    # 2. A dry run stops here, before the first side effect of any kind. No
    #    fetch, no worktree, no agent -- so "dry run" names the same thing an
    #    operator means by it.
    if dry_run:
        return FixResult(
            outcome=FixRunOutcome.ROUTING_PREPARED,
            reasons=selection.reasons
            + (
                "dry run: no workspace was created and no coding agent was invoked. "
                "The allowed scope is resolved against the target commit's tree, "
                "which a dry run does not check out.",
            ),
            target=target,
            dry_run=True,
        )

    try:
        with workspace.open(target.head_sha) as worktree:
            return _run_in_workspace(
                agent=agent,
                worktree=worktree,
                target=target,
                verdict=verdict,
                findings=selection.findings,
                allow_paths=allow_paths,
                selection_reasons=selection.reasons,
            )
    except WorkspaceError as exc:
        # Raised before the agent starts, or by the inspection afterwards.
        # Either way no fix survives it, and the fault is in the workspace.
        return FixResult(
            outcome=FixRunOutcome.CODING_AGENT_WORKSPACE_INVALID,
            reasons=(str(exc),),
            target=target,
        )


def _run_in_workspace(
    *,
    agent,
    worktree: str,
    target: ReviewTarget,
    verdict: ReviewVerdict,
    findings,
    allow_paths: tuple[str, ...],
    selection_reasons: tuple[str, ...],
) -> FixResult:
    """Everything that happens while the bound worktree exists."""

    # 3. Resolve the scope against the target commit's own tree. A finding
    #    that cannot be bounded is refused rather than routed with a guess.
    try:
        request = build_request(
            target=target,
            round=verdict.round,
            findings=findings,
            worktree=worktree,
            allow_paths=allow_paths,
        )
    except ScopeError as exc:
        return FixResult(
            outcome=FixRunOutcome.REVIEW_REQUIRES_HUMAN,
            reasons=(str(exc),),
            target=target,
            workspace_created=True,
        )

    # 4. One bounded turn. The prompt names the commit, the finding ids and
    #    the allowed paths; nothing untrusted reaches the command line.
    run = agent.invoke(build_prompt(request), cwd=worktree)
    if not run.ok:
        return FixResult(
            outcome=FixRunOutcome.CODING_AGENT_FAILED,
            reasons=(run.failure or "the coding agent failed",),
            target=target,
            request=request,
            agent_invoked=True,
            workspace_created=True,
            agent_stderr=run.stderr,
        )

    # 5. Read the tree before reading the answer, and read it on every path:
    #    an operator diagnosing a malformed response still needs to know what
    #    the agent left behind.
    inspection = inspect_workspace(worktree, target_head_sha=target.head_sha)

    def failed(outcome: FixRunOutcome, reason: str) -> FixResult:
        return FixResult(
            outcome=outcome,
            reasons=(reason,),
            target=target,
            request=request,
            inspection=inspection,
            agent_invoked=True,
            workspace_created=True,
            agent_stdout=run.stdout,
            agent_stderr=run.stderr,
        )

    # 6. Validate: the contract's shape, then its identity, then the tree.
    try:
        validated = validate(parse(run.stdout), request=request, inspection=inspection)
    except FixTargetBindingError as exc:
        return failed(FixRunOutcome.FIX_TARGET_MISMATCH, str(exc))
    except FixFindingBindingError as exc:
        return failed(FixRunOutcome.FIX_FINDING_MISMATCH, str(exc))
    except ScopeViolation as exc:
        return failed(FixRunOutcome.FIX_SCOPE_VIOLATION, str(exc))
    except (FixResponseParseError, FixResponseValidationError) as exc:
        return failed(FixRunOutcome.FIX_RESPONSE_MALFORMED, str(exc))

    outcome, reasons = _aggregate(validated)
    if inspection.patch_refused:
        reasons = reasons + (inspection.patch_refused,)
    return FixResult(
        outcome=outcome,
        reasons=selection_reasons + reasons,
        target=target,
        request=request,
        validated=validated,
        inspection=inspection,
        agent_invoked=True,
        workspace_created=True,
        agent_stdout=run.stdout,
        agent_stderr=run.stderr,
    )
