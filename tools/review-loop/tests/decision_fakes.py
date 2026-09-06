"""The merge-brief turn's three inputs, produced by the turns that emit them.

The review and push documents come from :mod:`rereview_fakes`, which builds
them in the shape the real commands emit. The re-review document is not built
at all: it is obtained by **running the real ``review-loop re-review``** over
offline fakes and capturing its ``--json`` output.

That is deliberate and it is the point of this module. A hand-written fixture
for the last document would encode one test author's idea of what the
re-review turn writes, and would keep passing after that turn changed its
output -- which is exactly the drift the merge-brief turn exists to detect,
since its whole job is to read those documents back faithfully.
"""

from __future__ import annotations

import io
import json

from review_loop.cli import main
from review_loop.reviewer_process import ReviewerRun

from fakes import (
    AUTOMATION_LOGIN,
    BASE_TIP,
    BASELINE_PATH,
    FILTERED_PATH,
    FakeCommentReader,
    FakeCommentWriter,
    FakeGitHubClient,
    FakeReviewer,
    pull_request_payload,
    run_payload,
)
from rereview_fakes import (
    PR,
    PUSHED_SHA,
    fresh_block,
    push_document,
    rereview_text,
    resolution_block,
    review_document,
)


def write(tmp_path, name, content) -> str:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def green_client(
    *, head_sha: str = PUSHED_SHA, merge_base: str = BASE_TIP, base_tip: str | None = None
) -> FakeGitHubClient:
    """A pull request at ``head_sha`` whose authoritative CI is green."""
    runs = [
        run_payload(
            run_id=1,
            path=BASELINE_PATH,
            head_sha=head_sha,
            pr_number=PR,
            merge_base=merge_base,
        ),
        run_payload(
            run_id=2,
            workflow_id=347481064,
            path=FILTERED_PATH,
            head_sha=head_sha,
            pr_number=PR,
            merge_base=merge_base,
        ),
    ]
    return FakeGitHubClient(
        pull_requests=[pull_request_payload(number=PR, head_sha=head_sha)],
        runs=runs,
        base_tip=merge_base if base_tip is None else base_tip,
    )


# -- re-reviewer outputs, one per case the classification distinguishes -------


def all_resolved() -> str:
    """Every original finding resolved, nothing found fresh."""
    return rereview_text(
        recommendation="approved",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
    )


def fresh_minor() -> str:
    return rereview_text(
        recommendation="changes_requested",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(fresh_block("R2.F1", severity="Minor"),),
    )


def fresh_major() -> str:
    return rereview_text(
        recommendation="changes_requested",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(fresh_block("R2.F1", severity="Major"),),
    )


def fresh_blocking() -> str:
    return rereview_text(
        recommendation="escalate",
        resolutions=(resolution_block("F1"), resolution_block("F2")),
        fresh=(fresh_block("R2.F1", severity="Blocking"),),
    )


def one_unresolved() -> str:
    return rereview_text(
        recommendation="changes_requested",
        resolutions=(
            resolution_block(
                "F1",
                resolution="UNRESOLVED",
                evidence="http_server.py still returns 500 with no detail.",
                reason="The handler was renamed but still swallows the error.",
            ),
            resolution_block("F2"),
        ),
    )


def one_escalated() -> str:
    return rereview_text(
        recommendation="escalate",
        resolutions=(
            resolution_block(
                "F1",
                resolution="ESCALATE",
                evidence="The dispatch path was rewritten around the finding.",
                reason="Whether the new contract satisfies F1 is a product question.",
            ),
            resolution_block("F2"),
        ),
    )


# -- the chain ---------------------------------------------------------------


def rereview_document(
    tmp_path,
    *,
    review: str | None = None,
    push: str | None = None,
    reviewer_output: str | None = None,
    client: FakeGitHubClient | None = None,
) -> str:
    """Run the real re-review turn over fakes and return its ``--json`` output."""
    review = review_document() if review is None else review
    push = push_document(review=review) if push is None else push

    out = io.StringIO()
    code = main(
        [
            "re-review",
            "--review-json",
            write(tmp_path, "chain-review.json", review),
            "--push-json",
            write(tmp_path, "chain-push.json", push),
            "--json",
        ],
        client=green_client() if client is None else client,
        reader=FakeCommentReader(),
        writer=FakeCommentWriter(comment_id=7777),
        reviewer=FakeReviewer(
            ReviewerRun(
                stdout=all_resolved() if reviewer_output is None else reviewer_output
            )
        ),
        expected_author=AUTOMATION_LOGIN,
        stream=out,
    )
    document = out.getvalue()
    assert code == 0, f"the re-review fixture did not produce a valid re-review: {document}"
    return document


def chain(
    tmp_path,
    *,
    reviewer_output: str | None = None,
    review: str | None = None,
    push: str | None = None,
    merge_base: str = BASE_TIP,
) -> tuple[str, str, str]:
    """``(review document, push document, re-review document)`` for one fix.

    ``merge_base`` moves the whole chain to a different integration state --
    the review, the push and the re-review all describing this head merged
    onto that base -- which is what distinguishes "the base advanced under an
    old chain" from "a newer chain against the advanced base".
    """
    review = review_document(merge_base=merge_base) if review is None else review
    push = push_document(review=review) if push is None else push
    return review, push, rereview_document(
        tmp_path,
        review=review,
        push=push,
        reviewer_output=reviewer_output,
        client=green_client(merge_base=merge_base),
    )


def edited(document: str, mutate) -> str:
    """Return ``document`` with ``mutate`` applied to its parsed payload."""
    payload = json.loads(document)
    mutate(payload)
    return json.dumps(payload, indent=2)


def invoke(
    tmp_path,
    *,
    documents: tuple[str, str, str] | None = None,
    extra: tuple[str, ...] = (),
    client: FakeGitHubClient | None = None,
    reader=None,
    writer=None,
):
    """Run ``review-loop merge-brief`` and return ``(code, output, writer)``."""
    review, push, rereview = (
        chain(tmp_path) if documents is None else documents
    )
    writer = FakeCommentWriter() if writer is None else writer
    out = io.StringIO()
    code = main(
        [
            "merge-brief",
            "--review-json",
            write(tmp_path, "review.json", review),
            "--push-json",
            write(tmp_path, "push.json", push),
            "--rereview-json",
            write(tmp_path, "rereview.json", rereview),
            *extra,
        ],
        client=green_client() if client is None else client,
        reader=FakeCommentReader() if reader is None else reader,
        writer=writer,
        expected_author=AUTOMATION_LOGIN,
        stream=out,
    )
    return code, out.getvalue(), writer
