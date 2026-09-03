"""Deciding whether a diff triggers a path-filtered workflow.

The rule this module enforces is that "a filter exists" and "this diff misses
the filter" are different claims, and only the second explains an absent run.
"""

import pytest

from review_loop.model import PathFilter
from review_loop.path_filter import FilterOutcome, evaluate

CHANGED = ("services/orchestrator/src/orchestrator/http_server.py", "docs/roadmap.md")


def _paths(*patterns):
    return PathFilter(mode="paths", patterns=patterns)


def _ignore(*patterns):
    return PathFilter(mode="paths-ignore", patterns=patterns)


def test_no_filter_always_matches():
    assert evaluate(None, CHANGED) is FilterOutcome.MATCHES


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("services/orchestrator/**", FilterOutcome.MATCHES),
        ("services/**", FilterOutcome.MATCHES),
        ("docs/*.md", FilterOutcome.MATCHES),
        ("packages/agent-contracts/**", FilterOutcome.NO_MATCH),
        ("tools/**", FilterOutcome.NO_MATCH),
    ],
    ids=["dir-star-star", "nested-star-star", "single-star", "other-dir", "unrelated"],
)
def test_paths_patterns_are_decided_against_the_diff(pattern, expected):
    assert evaluate(_paths(pattern), CHANGED) is expected


def test_single_star_does_not_cross_a_slash():
    """``*`` stops at ``/`` while ``**`` does not, per GitHub's filter syntax."""
    assert evaluate(_paths("docs/*"), ("docs/observability/x.md",)) is FilterOutcome.NO_MATCH
    assert evaluate(_paths("docs/**"), ("docs/observability/x.md",)) is FilterOutcome.MATCHES


@pytest.mark.parametrize("pattern", ["docs/?.md", "docs/a+.md", "docs/[ab].md", "!docs/**"])
def test_patterns_using_unmodelled_syntax_are_undecidable(pattern):
    """``?``, ``+``, ``[]`` and leading ``!`` do not mean what globbing means.

    Rather than re-implement them, the filter is reported as undecidable and
    the caller fails closed.
    """
    assert evaluate(_paths(pattern), CHANGED) is FilterOutcome.UNDECIDABLE


@pytest.mark.parametrize(
    "pattern, changed",
    [
        ("docs/**/*.md", "docs/README.md"),
        ("docs/**/*.md", "docs/observability/x.md"),
        ("**/README.md", "README.md"),
        ("**.js", "app.js"),
        ("services/**/tests/**", "services/orchestrator/tests/a.py"),
        ("*/**", "docs/x.md"),
    ],
    ids=[
        "interior-globstar-zero-directories",
        "interior-globstar-one-directory",
        "leading-globstar-at-root",
        "leading-globstar-no-slash",
        "two-globstars",
        "wildcard-prefix",
    ],
)
def test_a_globstar_outside_a_trailing_position_is_undecidable(pattern, changed):
    """The second review round's finding, generalised.

    GitHub does match these forms: ``docs/**/*.md`` matches ``docs/README.md``
    with zero intervening directories, and ``**/README.md`` matches a
    root-level ``README.md``. This slice models only the narrower subset the
    repository uses, so the richer shapes are reported undecidable rather than
    given a confident NO_MATCH that would explain away a workflow which should
    have run.
    """
    assert evaluate(_paths(pattern), (changed,)) is FilterOutcome.UNDECIDABLE


def test_the_repositorys_own_filter_shapes_stay_decidable():
    """Conservatism must not cost the patterns actually in use here."""
    for pattern in (
        "services/orchestrator/**",
        "packages/agent-contracts/**",
        "tools/review-loop/**",
    ):
        assert evaluate(_paths(pattern), ("tools/review-loop/src/x.py",)) in {
            FilterOutcome.MATCHES,
            FilterOutcome.NO_MATCH,
        }
    assert (
        evaluate(_paths(".github/workflows/review-loop.yml"), ("README.md",))
        is FilterOutcome.NO_MATCH
    )


@pytest.mark.parametrize("sibling", ["docs/[ab].md", "docs/**/*.md"])
def test_a_decidable_match_wins_over_an_unmodelled_sibling_pattern(sibling):
    """If something already matches, the undecidable pattern changes nothing."""
    outcome = evaluate(_paths("services/orchestrator/**", sibling), CHANGED)

    assert outcome is FilterOutcome.MATCHES


def test_paths_ignore_runs_when_any_file_escapes_the_ignore_list():
    assert evaluate(_ignore("docs/**"), CHANGED) is FilterOutcome.MATCHES
    assert evaluate(_ignore("docs/**"), ("docs/roadmap.md",)) is FilterOutcome.NO_MATCH


def test_paths_ignore_with_an_unmodelled_pattern_is_undecidable():
    """An uninterpretable pattern could be the one ignoring the last file."""
    assert evaluate(_ignore("docs/[ab].md"), CHANGED) is FilterOutcome.UNDECIDABLE


def test_an_unreadable_filter_is_undecidable():
    assert evaluate(PathFilter(mode="unreadable"), CHANGED) is FilterOutcome.UNDECIDABLE


def test_an_empty_diff_is_undecidable_rather_than_a_miss():
    assert evaluate(_paths("services/**"), ()) is FilterOutcome.UNDECIDABLE
