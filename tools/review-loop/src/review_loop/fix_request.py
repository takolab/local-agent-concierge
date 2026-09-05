"""What gets routed to a Coding Agent, and how far it is allowed to reach.

Two decisions live here, and they are the two that keep a fix turn bounded.

**Which findings route at all.** Not every valid verdict is a fix task. This
project's standing review-automation decision -- already enforced on the
review side by :mod:`review_loop.verdict_validation` -- is that a ``Blocking``
finding goes to a human and never into an automated fix. The same rule is
re-applied here rather than assumed, because the verdict may have travelled
through a file since it was validated, and a rule that is only checked
upstream is a rule that holds only as long as nothing changes upstream.

**How far a fix may reach.** A reviewer writes ``Location`` for a human, so it
is prose: ``tools/review-loop/src/review_loop/verdict.py:42``, or
``the README's Tests section``. Deriving an exact permitted file set from
prose is not possible, and pretending otherwise would produce a scope check
that fails on correct fixes and passes on incorrect ones.

The scope is therefore built from **two** sources, and which one is the
authority matters more than the arithmetic:

1. **The pull request's own change set is the outer boundary**, taken from
   git via :func:`review_loop.agent_workspace.resolve_change_set` -- the paths
   this branch changed relative to where it diverged from its base. Neither
   the reviewer nor the coding agent has any influence over it. Each changed
   path contributes its **component root**: the nearest ancestor directory
   holding a build manifest (``pyproject.toml``, ``package.json``, ``go.mod``,
   ``Cargo.toml``), never the repository root; failing that the path's own
   directory, and for a repository-root file, the file itself.
2. **A finding's ``Location`` selects within that boundary.** Its cited paths
   contribute their own component roots, and a component root outside the
   boundary is discarded rather than granted.

The direction of that second rule is the whole point. An earlier version of
this module derived the allowed scope from ``Location`` alone, which meant
reviewer-written text was itself an authority over what the coding agent was
permitted to edit -- so a finding could name a component this pull request had
never touched and the runner would hand the agent write access to it. The
prompt-injection boundary was applied *after* the scope had already been
computed from the same untrusted text. Reviewer prose can now narrow the
scope; it cannot widen it. (Independent review of PR #34 found this; the
finding was correct.)

The result is "primary location + its related tests + its nearby docs, inside
what this pull request already touches": for a finding in
``tools/review-loop/src/...`` of a pull request that changed that package, the
agent may edit its source, its tests and its README, and may not touch
``services/orchestrator`` or ``.github/workflows``. Wider than the single
cited file on purpose -- a fix whose test cannot be updated is not a fix --
and much narrower than the repository.

Two ways a finding fails to bound, both refused rather than guessed at, and
both ending with a human reading the finding:

* its ``Location`` cites no path that exists at the target commit;
* every path it cites lies outside the pull request's own change set.

``--allow-path`` is the deliberate escape hatch, and it is the *only* input
that may reach beyond the change-set boundary -- because it comes from an
operator who has read the finding, which is exactly the human authorization
the boundary exists to require. It *extends* the boundary rather than sitting
beside it, so a finding that points outside the change set becomes routable
once a human has said so, and not before. It is explicit in the output and in
the agent's own task contract.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from enum import Enum

from .fix_response import (
    FIX_RESPONSE_BEGIN,
    FIX_RESPONSE_END,
    MAX_PATH_CHARS,
)
from .review_target import ReviewTarget
from .verdict import Finding, Recommendation, ReviewVerdict, Severity

#: A directory holding one of these is a component: the unit a fix, its tests
#: and its documentation naturally live inside.
COMPONENT_MANIFESTS = ("pyproject.toml", "package.json", "go.mod", "Cargo.toml")

#: Path-shaped tokens in a reviewer's prose. Broad on purpose: everything it
#: yields is then required to exist at the target commit *and* to fall inside
#: the change set, which are the filters that actually decide.
#:
#: A leading dot is admitted only when a name follows it, so that a finding
#: about ``.github/workflows/pytest.yml`` or ``.gitignore`` cites a path this
#: runner can see -- without ``..`` ever becoming a token.
_PATH_TOKEN = re.compile(r"(?:[A-Za-z0-9_]|\.(?=[A-Za-z0-9_]))[A-Za-z0-9_.+/-]*")

#: ``file.py:42``, ``file.py:42:9``, ``file.py#anchor``, ``file.py:L42``.
_LOCATION_SUFFIX = re.compile(r"(?::L?\d+(?::\d+)?|#.*)\Z")

_TRAILING_PUNCTUATION = ".,;:)]}'\"`"


class RoutingDecision(Enum):
    """Whether a validated verdict becomes a fix task."""

    #: There are findings to route, and they can be bounded.
    ROUTE = "ROUTE"
    #: The verdict is valid and asks for nothing. No agent should run.
    NOTHING_TO_DO = "NOTHING_TO_DO"
    #: The verdict is valid and this slice will not route it automatically.
    REQUIRES_HUMAN = "REQUIRES_HUMAN"


class ScopeError(ValueError):
    """A finding's fix task cannot be bounded to a set of paths."""


@dataclass(frozen=True)
class Selection:
    """Which findings a verdict offers to a fix turn, and why."""

    decision: RoutingDecision
    findings: tuple[Finding, ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AllowedPath:
    """One entry of an allowed scope: a directory prefix or an exact file."""

    path: str
    is_directory: bool

    def contains(self, candidate: str) -> bool:
        if self.is_directory:
            return candidate == self.path or candidate.startswith(self.path + "/")
        return candidate == self.path

    def display(self) -> str:
        return self.path + "/" if self.is_directory else self.path


@dataclass(frozen=True)
class RoutedFinding:
    """One finding, together with the paths its fix may touch."""

    finding: Finding
    cited_paths: tuple[str, ...]
    allowed_paths: tuple[AllowedPath, ...]
    #: Paths the finding cites that exist at the target but lie outside the
    #: pull request's change set. Recorded rather than silently dropped: they
    #: are what the reviewer was pointing at, and an operator deciding whether
    #: to pass ``--allow-path`` needs to see them.
    out_of_boundary_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class FixRequest:
    """The bounded task one Coding Agent turn is given.

    Every provenance field the review turn established travels with it. The
    agent is never told "fix this in the latest branch": it is told which
    repository, which pull request, which commit, which review round and which
    finding id, and its response is checked against all of them.
    """

    target: ReviewTarget
    round: int
    findings: tuple[RoutedFinding, ...]
    operator_allowed_paths: tuple[AllowedPath, ...] = ()
    #: The component roots of the paths this pull request itself changed. The
    #: outer limit on every finding-derived entry above, and the one input to
    #: this request that no agent and no reviewer can influence.
    change_set_boundary: tuple[AllowedPath, ...] = ()

    @property
    def allowed_paths(self) -> tuple[AllowedPath, ...]:
        """Every path entry any routed finding may touch, deduplicated."""
        seen: dict[tuple[str, bool], AllowedPath] = {}
        for routed in self.findings:
            for entry in routed.allowed_paths:
                seen.setdefault((entry.path, entry.is_directory), entry)
        for entry in self.operator_allowed_paths:
            seen.setdefault((entry.path, entry.is_directory), entry)
        return tuple(seen.values())

    def permits(self, candidate: str) -> bool:
        return any(entry.contains(candidate) for entry in self.allowed_paths)

    @property
    def finding_ids(self) -> tuple[str, ...]:
        return tuple(routed.finding.finding_id for routed in self.findings)


def select_findings(
    verdict: ReviewVerdict, *, max_findings: int
) -> Selection:
    """Decide whether this verdict's findings may be routed automatically.

    Runs before any workspace exists, so that a verdict nothing can be done
    with never causes a checkout, a fetch, or an agent process.
    """
    if verdict.recommendation is Recommendation.APPROVED:
        return Selection(
            decision=RoutingDecision.NOTHING_TO_DO,
            reasons=("the review recommends 'approved' and reports no open finding",),
        )

    if not verdict.open_findings:
        return Selection(
            decision=RoutingDecision.NOTHING_TO_DO,
            reasons=("the review reports no open finding",),
        )

    blocking = [f for f in verdict.open_findings if f.severity is Severity.BLOCKING]
    if blocking:
        return Selection(
            decision=RoutingDecision.REQUIRES_HUMAN,
            reasons=(
                "the review reports "
                + ", ".join(f.finding_id for f in blocking)
                + " as Blocking; a Blocking finding goes to a human and is never "
                "routed to a coding agent",
            ),
        )

    if verdict.recommendation is Recommendation.ESCALATE:
        return Selection(
            decision=RoutingDecision.REQUIRES_HUMAN,
            reasons=(
                "the review recommends 'escalate'"
                + (
                    f": {verdict.escalation_reason}"
                    if verdict.escalation_reason
                    else ""
                ),
            ),
        )

    if len(verdict.open_findings) > max_findings:
        return Selection(
            decision=RoutingDecision.REQUIRES_HUMAN,
            reasons=(
                f"the review reports {len(verdict.open_findings)} open findings, "
                f"above the {max_findings} this runner will carry in one bounded "
                "fix turn",
            ),
        )

    return Selection(
        decision=RoutingDecision.ROUTE,
        findings=verdict.open_findings,
        reasons=(
            f"{len(verdict.open_findings)} open finding(s) are routable: "
            + ", ".join(f.finding_id for f in verdict.open_findings),
        ),
    )


def candidate_paths(text: str) -> tuple[str, ...]:
    """Extract path-shaped tokens from reviewer prose, in order, deduplicated.

    Nothing here decides that a token *is* a path. It proposes tokens; the
    target commit decides.
    """
    found: list[str] = []
    for match in _PATH_TOKEN.finditer(text):
        token = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        token = _LOCATION_SUFFIX.sub("", token)
        token = token.rstrip(_TRAILING_PUNCTUATION)
        if not token or len(token) > MAX_PATH_CHARS:
            continue
        # A bare word is not a path. Either it has a separator, or it has a
        # file extension -- "README.md" cites a file, "Evidence" does not.
        if "/" not in token and "." not in posixpath.basename(token):
            continue
        if token not in found:
            found.append(token)
    return tuple(found)


def _is_inside(worktree: str, relative: str) -> str | None:
    """Return the absolute path of ``relative`` inside ``worktree``, or None.

    A cited path is untrusted text. ``../../etc/passwd`` and an absolute path
    are rejected outright, and the resolved real path is required to stay
    inside the worktree, so a symlink planted in the tree cannot widen the
    scope by pointing outward.
    """
    if not relative or posixpath.isabs(relative) or relative.startswith("~"):
        return None
    if ".." in relative.split("/"):
        return None
    root = os.path.realpath(worktree)
    resolved = os.path.realpath(os.path.join(worktree, relative))
    if resolved != root and not resolved.startswith(root + os.sep):
        return None
    return resolved


def component_root(worktree: str, relative: str) -> AllowedPath:
    """The bounded scope one cited path contributes."""
    directory = posixpath.dirname(relative)

    ancestors: list[str] = []
    current = directory
    while current:
        ancestors.append(current)
        current = posixpath.dirname(current)

    for ancestor in ancestors:  # nearest to the cited path first
        if any(
            os.path.isfile(os.path.join(worktree, ancestor, manifest))
            for manifest in COMPONENT_MANIFESTS
        ):
            return AllowedPath(ancestor, is_directory=True)

    # No manifest below the repository root. The repository root is never a
    # component root -- that would permit everything -- so fall back to the
    # cited path's own directory, and for a root-level file to the file.
    if directory:
        return AllowedPath(directory, is_directory=True)
    return AllowedPath(relative, is_directory=False)


def boundary_from_change_set(
    change_set: tuple[str, ...], *, worktree: str
) -> tuple[AllowedPath, ...]:
    """The outer scope limit: the component roots this pull request touched.

    ``change_set`` comes from git, not from any text a reviewer or an agent
    wrote, which is the only property that makes it usable as an authority.
    """
    entries: list[AllowedPath] = []
    for path in change_set:
        normalised = posixpath.normpath(path)
        if _is_inside(worktree, normalised) is None:
            continue
        entry = component_root(worktree, normalised)
        if entry not in entries:
            entries.append(entry)
    if not entries:
        raise ScopeError(
            "this pull request's change set yields no component root, so there "
            "is no boundary to bound a fix inside"
        )
    return tuple(entries)


def resolve_finding(
    finding: Finding, *, worktree: str, boundary: tuple[AllowedPath, ...]
) -> RoutedFinding:
    """Bound one finding's fix to a set of paths, or refuse it.

    ``worktree`` is a checkout of the exact target commit, already verified by
    :mod:`review_loop.reviewer_workspace`. Existence is checked there rather
    than against the operator's own checkout, so "this path exists" means it
    exists *in the commit under review*.
    """
    for forbidden in (FIX_RESPONSE_BEGIN, FIX_RESPONSE_END):
        for label, value in (
            ("Location", finding.location),
            ("Problem", finding.problem),
            ("Evidence", finding.evidence),
            ("Required outcome", finding.required_outcome),
            ("Scope boundary", finding.scope_boundary or ""),
        ):
            if forbidden in value:
                raise ScopeError(
                    f"finding {finding.finding_id}: {label!r} contains "
                    f"{forbidden!r}, the fix response delimiter; reviewer text may "
                    "not contain the marker its own answer is read from"
                )

    cited: list[str] = []
    entries: list[AllowedPath] = []
    outside: list[str] = []
    for token in candidate_paths(finding.location):
        normalised = posixpath.normpath(token)
        if _is_inside(worktree, normalised) is None:
            continue
        if not os.path.exists(os.path.join(worktree, normalised)):
            continue
        cited.append(normalised)
        entry = (
            AllowedPath(normalised, is_directory=True)
            if os.path.isdir(os.path.join(worktree, normalised))
            else component_root(worktree, normalised)
        )
        # The boundary decides. A cited path whose component root this pull
        # request never touched is discarded, not granted: reviewer text may
        # select inside the change set and may not reach beyond it.
        if not any(limit.contains(entry.path) for limit in boundary):
            if normalised not in outside:
                outside.append(normalised)
            continue
        if entry not in entries:
            entries.append(entry)

    if not cited:
        raise ScopeError(
            f"finding {finding.finding_id}: its Location "
            f"({finding.location[:120]!r}) cites no path that exists at "
            "the target commit, so the fix cannot be bounded to a set of files"
        )
    if not entries:
        raise ScopeError(
            f"finding {finding.finding_id}: every path its Location cites "
            f"({', '.join(outside)}) lies outside what this pull request "
            "changed, so routing it would widen the coding agent's scope on "
            "the reviewer's say-so. Pass --allow-path deliberately, or fix "
            "this finding by hand"
        )

    return RoutedFinding(
        finding=finding,
        cited_paths=tuple(cited),
        allowed_paths=tuple(entries),
        out_of_boundary_paths=tuple(outside),
    )


def parse_allow_path(value: str, *, worktree: str) -> AllowedPath:
    """Read one operator-supplied ``--allow-path``.

    A trailing slash, or an existing directory, means a prefix; anything else
    is one exact path -- which may not exist yet, because a legitimate fix
    creates a new test file.
    """
    text = value.strip()
    if not text:
        raise ScopeError("an --allow-path entry is empty")
    is_directory = text.endswith("/")
    normalised = posixpath.normpath(text.rstrip("/"))
    if _is_inside(worktree, normalised) is None:
        raise ScopeError(
            f"--allow-path {value!r} is not a relative path inside the target "
            "commit's tree"
        )
    if os.path.isdir(os.path.join(worktree, normalised)):
        is_directory = True
    return AllowedPath(normalised, is_directory=is_directory)


def build_request(
    *,
    target: ReviewTarget,
    round: int,
    findings: tuple[Finding, ...],
    worktree: str,
    change_set: tuple[str, ...],
    allow_paths: tuple[str, ...] = (),
) -> FixRequest:
    """Build the bounded routing request for one fix turn.

    ``change_set`` is what this pull request changed, per git. It is required
    rather than optional: without it there is no authority behind the scope
    but the reviewer's own prose, which is the thing this boundary exists to
    stop being an authority.
    """
    boundary = boundary_from_change_set(change_set, worktree=worktree)
    operator = tuple(parse_allow_path(p, worktree=worktree) for p in allow_paths)

    # The operator's own entries extend the boundary rather than sitting
    # beside it, so ``--allow-path`` actually rescues a finding that points
    # outside the change set -- which is what the refusal message tells the
    # operator to do. Resolving findings against the change set alone and
    # adding the operator's paths afterwards would refuse the finding first
    # and then permit paths nothing was routed to.
    effective = boundary + tuple(
        entry for entry in operator if entry not in boundary
    )
    routed = tuple(
        resolve_finding(f, worktree=worktree, boundary=effective) for f in findings
    )
    return FixRequest(
        target=target,
        round=round,
        findings=routed,
        operator_allowed_paths=operator,
        change_set_boundary=boundary,
    )
