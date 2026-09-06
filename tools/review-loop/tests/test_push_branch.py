"""Which ref this runner may write, and every shape that is refused.

The whole slice's blast radius is decided here, so these tests are mostly
about the branches that must be impossible to reach: a default branch, a
fork's branch, the base branch, and anything whose name git would read as
something other than an ordinary branch.
"""

from __future__ import annotations

import pytest

from fakes import FULL_SHA, REPO, pull_request_payload
from review_loop.push_branch import (
    BranchAuthorityError,
    check_branch_name,
    resolve,
)

NUMBER = 27


def payload(**overrides) -> dict:
    kwargs = {"number": NUMBER, "head_sha": FULL_SHA, "head_ref": "feat/example"}
    kwargs.update(overrides)
    return pull_request_payload(**kwargs)


# --------------------------------------------------------------------------
# The one shape that is allowed
# --------------------------------------------------------------------------


def test_the_push_target_is_the_pull_requests_own_head_branch():
    target = resolve(payload(), repo=REPO, number=NUMBER)

    assert target.branch == "feat/example"
    assert target.ref == "refs/heads/feat/example"
    assert target.base_ref == "master"
    assert target.default_branch == "master"
    assert target.head_sha == FULL_SHA


def test_the_refspec_is_a_plain_fast_forward_of_one_exact_commit():
    target = resolve(payload(), repo=REPO, number=NUMBER)

    refspec = target.refspec(FULL_SHA)

    assert refspec == f"{FULL_SHA}:refs/heads/feat/example"
    assert not refspec.startswith("+")
    assert "refs/tags" not in refspec


@pytest.mark.parametrize("sha", ["abc1234", "", "HEAD", FULL_SHA.upper(), "a" * 41])
def test_a_refspec_is_never_built_for_an_inexact_commit(sha):
    target = resolve(payload(), repo=REPO, number=NUMBER)

    with pytest.raises(BranchAuthorityError):
        target.refspec(sha)


# --------------------------------------------------------------------------
# Branches that must be unreachable
# --------------------------------------------------------------------------


def test_the_default_branch_is_never_a_push_target():
    with pytest.raises(BranchAuthorityError, match="default branch"):
        resolve(
            payload(head_ref="master", base_ref="release", default_branch="master"),
            repo=REPO,
            number=NUMBER,
        )


def test_the_base_branch_is_never_a_push_target():
    with pytest.raises(BranchAuthorityError, match="head and"):
        resolve(
            payload(head_ref="develop", base_ref="develop", default_branch="master"),
            repo=REPO,
            number=NUMBER,
        )


def test_a_fork_head_is_refused_rather_than_pushed_to_this_repository():
    with pytest.raises(BranchAuthorityError, match="lives in someone/fork"):
        resolve(
            payload(head_repo="someone/fork"), repo=REPO, number=NUMBER
        )


def test_a_deleted_head_repository_is_refused():
    with pytest.raises(BranchAuthorityError, match="which repository"):
        resolve(payload(head_repo=None), repo=REPO, number=NUMBER)


@pytest.mark.parametrize("state", ["closed", "merged", "", None])
def test_a_pull_request_that_is_not_open_is_not_a_push_target(state):
    with pytest.raises(BranchAuthorityError, match="not a branch this runner pushes"):
        resolve(payload(state=state), repo=REPO, number=NUMBER)


def test_a_pull_request_object_for_another_number_is_refused():
    with pytest.raises(BranchAuthorityError, match="not the #27"):
        resolve(payload(number=99), repo=REPO, number=NUMBER)


def test_a_payload_without_a_default_branch_cannot_rule_out_pushing_to_one():
    broken = payload()
    broken["base"]["repo"] = {"full_name": REPO}

    with pytest.raises(BranchAuthorityError, match="default branch"):
        resolve(broken, repo=REPO, number=NUMBER)


def test_a_payload_without_a_base_ref_is_refused():
    broken = payload()
    broken["base"]["ref"] = ""

    with pytest.raises(BranchAuthorityError, match="base branch"):
        resolve(broken, repo=REPO, number=NUMBER)


@pytest.mark.parametrize("sha", ["", "abc1234", None, FULL_SHA.upper()])
def test_a_payload_without_an_exact_head_sha_is_refused(sha):
    with pytest.raises(BranchAuthorityError, match="head SHA"):
        resolve(payload(head_sha=sha), repo=REPO, number=NUMBER)


# --------------------------------------------------------------------------
# Branch names that are not branch names
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "refs/heads/master",
        "refs/tags/v1",
        "--force",
        "-b",
        "feat/../master",
        "feat/x@{1}",
        "feat/x:y",
        "feat/x^",
        "feat/x~1",
        "feat/x?",
        "feat/x*",
        "feat/x[1]",
        "feat\\x",
        "feat/x y",
        "feat//x",
        "feat/x/",
        "feat/x.lock",
        "HEAD",
        ".hidden",
        "feat/.hidden",
        "",
        None,
        123,
        "a" * 256,
    ],
)
def test_an_unusable_branch_name_is_refused(name):
    with pytest.raises(BranchAuthorityError):
        check_branch_name(name)


@pytest.mark.parametrize(
    "name", ["master-fix", "feat/review-loop-push", "release/1.2.3", "x", "a_b.c/d"]
)
def test_an_ordinary_branch_name_is_accepted(name):
    assert check_branch_name(name) == name


def test_an_unusable_branch_name_stops_resolution_too():
    with pytest.raises(BranchAuthorityError):
        resolve(payload(head_ref="refs/heads/sneaky"), repo=REPO, number=NUMBER)


# --------------------------------------------------------------------------
# Which repository receives the push
# --------------------------------------------------------------------------

TARGET = "takolab/local-agent-concierge"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/takolab/local-agent-concierge.git", ("github.com", TARGET)),
        ("https://github.com/takolab/local-agent-concierge", ("github.com", TARGET)),
        ("http://github.com/takolab/local-agent-concierge.git", ("github.com", TARGET)),
        ("git@github.com:takolab/local-agent-concierge.git", ("github.com", TARGET)),
        ("git@github.com:takolab/local-agent-concierge", ("github.com", TARGET)),
        ("ssh://git@github.com/takolab/local-agent-concierge.git", ("github.com", TARGET)),
        ("ssh://git@ssh.github.com:443/takolab/local-agent-concierge.git", ("ssh.github.com", TARGET)),
        ("git://github.com/takolab/local-agent-concierge.git", ("github.com", TARGET)),
        ("https://GITHUB.COM/takolab/local-agent-concierge.git", ("github.com", TARGET)),
        # A token embedded by a credential helper must not change the identity.
        ("https://x-access-token:ghs_secret@github.com/takolab/local-agent-concierge.git", ("github.com", TARGET)),
        # Hostless forms: a local path or a file:// URL.
        ("/srv/mirrors/takolab/local-agent-concierge.git", (None, TARGET)),
        ("file:///srv/mirrors/takolab/local-agent-concierge.git", (None, TARGET)),
        ("../takolab/local-agent-concierge.git", (None, TARGET)),
        # Another forge, another repository, and something unreadable.
        ("https://gitlab.com/takolab/local-agent-concierge.git", ("gitlab.com", TARGET)),
        ("https://github.com/someone/fork.git", ("github.com", "someone/fork")),
        ("/srv/origin.git", (None, "srv/origin")),
        ("origin.git", (None, None)),
        ("", (None, None)),
    ],
)
def test_a_remote_url_is_read_as_a_repository_identity(url, expected):
    from review_loop.push_branch import repository_from_url

    assert repository_from_url(url) == expected


def test_a_remote_naming_the_target_repository_is_accepted():
    from review_loop.push_branch import check_remote_repository

    check_remote_repository(
        (
            "https://github.com/takolab/local-agent-concierge.git",
            "git@github.com:takolab/local-agent-concierge.git",
        ),
        expected_repo=TARGET,
        remote="origin",
    )


def test_a_case_difference_in_the_repository_name_is_still_the_same_repository():
    from review_loop.push_branch import check_remote_repository

    check_remote_repository(
        ("https://github.com/TakoLab/Local-Agent-Concierge.git",),
        expected_repo=TARGET,
        remote="origin",
    )


@pytest.mark.parametrize(
    "urls,fragment",
    [
        (("https://github.com/someone/fork.git",), "someone/fork"),
        (("https://gitlab.com/takolab/local-agent-concierge.git",), "host is not GitHub"),
        (("/srv/mirrors/someone/mirror.git",), "someone/mirror"),
        (("origin.git",), "cannot read as a repository"),
        ((), "reports no URL"),
        # The push URL differs from the fetch URL: the one that decides where
        # a push lands must be checked too, so agreement is required.
        (
            (
                "https://github.com/takolab/local-agent-concierge.git",
                "https://github.com/someone/fork.git",
            ),
            "someone/fork",
        ),
    ],
)
def test_a_remote_that_is_not_the_target_repository_is_refused(urls, fragment):
    from review_loop.push_branch import check_remote_repository

    with pytest.raises(BranchAuthorityError) as error:
        check_remote_repository(urls, expected_repo=TARGET, remote="origin")

    assert fragment in str(error.value)
