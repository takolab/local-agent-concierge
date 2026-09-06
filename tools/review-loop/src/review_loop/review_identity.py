"""A deterministic identity for one validated review.

Every stage after the review turn has to answer "which review is this a fix
for?", and until now each answered it with a different subset of facts:
repository and pull request, then the reviewed head, then the finding ids.
None of those identifies the *artifact*. Finding ids are labels local to one
review turn -- and ``F1``, ``F2`` is the convention the reviewer prompt
itself suggests -- so two independent reviews of the same commit routinely
carry the same ids for entirely different findings:

```text
Review A of H1              Review B of H1
F1 worker.py  not atomic    F1 process.py  credentials leak
F2 ci.py      stale CI      F2 README.md   false guarantee
```

Both have ``round 1``, head ``H1`` and finding ids ``{F1, F2}``. Pairing
either with the same push satisfies every check that compares those fields,
and the re-review would then hand a fresh reviewer the wrong findings to
resolve and record the answer as history about the fix.

The merge context has the same gap. This package already treats a review of
``H`` onto ``B1`` as a different record from a review of the same ``H`` onto
``B2`` -- that is why ``base_sha`` is in :class:`RecordIdentity` -- and the
review identity has to agree with that, or the two disagree about what "the
same review" means.

So one function answers it once, for the whole chain: the canonical bytes of
the validated review model, and their SHA-256. It travels

```text
review --json -> fix handoff -> push --json -> re-review
```

and the re-review **recomputes** it from the review document it was given
rather than reading it back, so agreement means the two documents really are
the same validated artifact.

What this is and is not: it is a checksum over content, not a signature. It
detects the accidental pairing of two distinct validated reviews, which is
the whole class of error this provenance layer exists to catch. It does not
survive an adversary who edits both documents -- neither does anything else
in a chain of operator-controlled files, which is why git and GitHub, not the
files, decide which commit is read.

**Two fields are deliberately outside the digest.** ``ci_evidence`` is CI
observation rather than review content, and -- decisively --
:func:`review_loop.routing.load_handoff` does not read it back, so it is
absent from the model every later stage holds; hashing it would make the
digest uncomputable downstream. ``resolved_finding_ids`` is likewise not
carried by the handoff, and is always empty in round 1.
"""

from __future__ import annotations

import hashlib
import json

from .review_target import ReviewTarget
from .verdict import ReviewVerdict

#: Named in the hashed bytes so that a future change to the canonical form
#: produces a different digest rather than a silent collision with this one.
CANONICAL_VERSION = "local-agent-concierge:review-identity:v1"


def canonical_document(target: ReviewTarget, verdict: ReviewVerdict) -> str:
    """The exact text hashed for this review, as a canonical JSON document.

    Returned rather than kept private so a mismatch can be diffed: "the
    digests differ" is not a diagnosis, and an operator holding two review
    files needs to see *which* field differs.

    Findings keep the order the verdict gave them. That order is part of the
    artifact and survives the handoff unchanged, so preserving it is both
    simpler and strictly more discriminating than sorting.
    """
    payload = {
        "version": CANONICAL_VERSION,
        "repo": target.repo,
        "number": target.number,
        "head_sha": target.head_sha,
        "base_ref": target.base_ref,
        "ci_merge_base_sha": target.ci_merge_base_sha,
        "round": verdict.round,
        "recommendation": verdict.recommendation.value,
        "escalation_reason": verdict.escalation_reason,
        "findings": [
            {
                "finding_id": finding.finding_id,
                "severity": finding.severity.value,
                "location": finding.location,
                "problem": finding.problem,
                "evidence": finding.evidence,
                "required_outcome": finding.required_outcome,
                "scope_boundary": finding.scope_boundary,
            }
            for finding in verdict.open_findings
        ],
    }
    # sort_keys makes the key order independent of this dict literal;
    # separators removes the whitespace a formatter could otherwise change;
    # ensure_ascii=False keeps the bytes a function of the text rather than of
    # an escaping choice, and the encoding below pins them.
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def review_sha256(target: ReviewTarget, verdict: ReviewVerdict) -> str:
    """The identity of one validated review, as a 64-character hex digest."""
    return hashlib.sha256(
        canonical_document(target, verdict).encode("utf-8")
    ).hexdigest()
