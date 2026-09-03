"""Resolving the exact review target from a pull request number.

The point of these tests is that the 40-character head SHA is the only
accepted identity, and that an abbreviated SHA can never be mistaken for one.
"""

import pytest

from review_loop.github_client import GitHubClient
from review_loop.model import NotAFullShaError, PullRequestTarget, short_sha
from review_loop.runner import build_target, verify_pull_request
from review_loop.model import Verdict

from fakes import FULL_SHA, OTHER_SHA, FakeGitHubClient, pull_request_payload, run_payload


def test_pull_request_number_resolves_to_the_exact_forty_character_head_sha():
    target = build_target(pull_request_payload(number=27, head_sha=FULL_SHA), 27)

    assert target.number == 27
    assert target.head_sha == FULL_SHA
    assert len(target.head_sha) == 40


def test_head_sha_is_not_the_merge_commit_or_the_base_sha():
    payload = pull_request_payload(number=27, head_sha=FULL_SHA)

    target = build_target(payload, 27)

    assert target.head_sha != payload["merge_commit_sha"]
    assert target.head_sha != payload["base"]["sha"]


@pytest.mark.parametrize(
    "bad_sha",
    [
        "3b51470",
        "3b514700c1c2c257a39a7037f1a21ca5b906410",  # 39 characters
        "3B514700C1C2C257A39A7037F1A21CA5B9064106",  # uppercase
        "",
        None,
        object(),
    ],
    ids=["abbreviated", "too-short", "uppercase", "empty", "none", "not-a-string"],
)
def test_anything_other_than_a_full_lowercase_sha_is_rejected_as_an_identity(bad_sha):
    with pytest.raises(NotAFullShaError):
        PullRequestTarget(
            number=27, head_sha=bad_sha, base_ref="master", head_ref="feat/x", state="open"
        )


def test_short_sha_is_display_only_and_never_round_trips_into_an_identity():
    target = build_target(pull_request_payload(head_sha=FULL_SHA), 27)

    assert short_sha(target.head_sha) == FULL_SHA[:7]
    with pytest.raises(NotAFullShaError):
        PullRequestTarget(
            number=27,
            head_sha=short_sha(target.head_sha),
            base_ref="master",
            head_ref="feat/x",
            state="open",
        )


def test_the_runs_query_refuses_an_abbreviated_sha_instead_of_returning_no_ci():
    """An abbreviated SHA must fail loudly at the client boundary.

    The live endpoint answers an abbreviated ``head_sha`` with HTTP 200 and
    ``total_count: 0``, which is indistinguishable from a commit that has no
    CI at all. Rejecting it here is what keeps that from becoming a verdict.
    """
    client = GitHubClient("takolab/local-agent-concierge")

    with pytest.raises(NotAFullShaError):
        client.list_workflow_runs_for_sha(FULL_SHA[:7])


def test_runs_are_matched_on_the_exact_sha_not_a_prefix():
    client = FakeGitHubClient(
        pull_requests=[pull_request_payload(head_sha=FULL_SHA)],
        runs=[run_payload(run_id=1, head_sha=OTHER_SHA)],
    )

    evaluation = verify_pull_request(client, 27)

    # The run belongs to a different commit, so it is not evidence here.
    assert evaluation.outcomes == ()
    assert evaluation.verdict is Verdict.AMBIGUOUS
