"""Classifying workflow triggers into required, conditional or unknown."""

import pytest

from review_loop.model import TriggerExpectation
from review_loop.workflow_config import classify_trigger, classify_workflow_files

from fakes import (
    BASELINE_PATH,
    BASELINE_YAML,
    DEFAULT_WORKFLOW_FILES,
    FILTERED_PATH,
    FILTERED_YAML,
    SECOND_FILTERED_PATH,
)


def test_a_pull_request_workflow_without_a_path_filter_is_required():
    definition = classify_trigger(BASELINE_YAML, BASELINE_PATH, "master")

    assert definition.expectation is TriggerExpectation.REQUIRED
    assert definition.name == "Python tests"


@pytest.mark.parametrize("filter_key", ["paths", "paths-ignore"])
def test_a_path_filtered_workflow_is_conditional(filter_key):
    source = f"""\
name: Filtered
on:
  pull_request:
    branches: [master]
    {filter_key}:
      - "services/**"
jobs:
  test:
    runs-on: ubuntu-latest
"""

    definition = classify_trigger(source, FILTERED_PATH, "master")

    assert definition.expectation is TriggerExpectation.CONDITIONAL


def test_a_bare_on_key_is_read_as_a_trigger_not_as_the_boolean_true():
    """YAML 1.1 parses an unquoted ``on:`` as ``True``.

    Missing this would classify every workflow in the repository as having no
    pull_request trigger, which would silently remove the baseline anchor.
    """
    definition = classify_trigger(BASELINE_YAML, BASELINE_PATH, "master")
    assert definition.expectation is TriggerExpectation.REQUIRED

    quoted = classify_trigger(
        BASELINE_YAML.replace("on:", '"on":', 1), BASELINE_PATH, "master"
    )
    assert quoted.expectation is TriggerExpectation.REQUIRED


@pytest.mark.parametrize(
    "trigger, expected",
    [
        ("on: pull_request", TriggerExpectation.REQUIRED),
        ("on: [pull_request, push]", TriggerExpectation.REQUIRED),
        ("on: [push]", TriggerExpectation.NOT_EXPECTED),
        ("on: push", TriggerExpectation.NOT_EXPECTED),
        ("on:\n  pull_request:", TriggerExpectation.REQUIRED),
        ("on:\n  workflow_dispatch:", TriggerExpectation.NOT_EXPECTED),
        ("on:\n  pull_request: 7", TriggerExpectation.UNKNOWN),
        ("on: 7", TriggerExpectation.UNKNOWN),
    ],
    ids=[
        "scalar-pull-request",
        "list-with-pull-request",
        "list-without-pull-request",
        "scalar-push",
        "mapping-empty-pull-request",
        "dispatch-only",
        "unreadable-pull-request-block",
        "unreadable-trigger-block",
    ],
)
def test_trigger_shapes_are_classified_or_flagged_unknown(trigger, expected):
    source = f"name: Example\n{trigger}\njobs:\n  test:\n    runs-on: ubuntu-latest\n"

    assert classify_trigger(source, BASELINE_PATH, "master").expectation is expected


def _branch_source(key: str, patterns: str) -> str:
    return (
        f"name: Example\non:\n  pull_request:\n    {key}: {patterns}\n"
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
    )


def test_a_workflow_targeting_another_base_branch_by_literal_name_is_not_expected():
    source = _branch_source("branches", "[release]")

    assert (
        classify_trigger(source, BASELINE_PATH, "master").expectation
        is TriggerExpectation.NOT_EXPECTED
    )
    assert (
        classify_trigger(source, BASELINE_PATH, "release").expectation
        is TriggerExpectation.REQUIRED
    )


def test_a_branches_ignore_match_removes_the_workflow_from_expectation():
    source = _branch_source("branches-ignore", "[master]")

    assert (
        classify_trigger(source, BASELINE_PATH, "master").expectation
        is TriggerExpectation.NOT_EXPECTED
    )


@pytest.mark.parametrize(
    "pattern", ["release/*", "releases/**", "feature/?", "release/[12]", "!master", "v+"]
)
@pytest.mark.parametrize("key", ["branches", "branches-ignore"])
def test_a_branch_pattern_beyond_a_literal_name_is_unknown(key, pattern):
    """GitHub's branch globs are not Python's, so none of these are decided.

    Its ``*`` does not span ``/`` -- which is why ``releases/**`` exists as a
    separate documented form -- and ``!`` negates in order. ``fnmatch`` gets
    both wrong, and a wrong NOT_EXPECTED silently excuses a missing run.
    """
    source = _branch_source(key, f'["{pattern}"]')

    assert (
        classify_trigger(source, BASELINE_PATH, "master").expectation
        is TriggerExpectation.UNKNOWN
    )


def test_a_nested_branch_is_not_quietly_matched_by_a_single_star():
    """The concrete case: ``release/*`` must not be read as spanning ``/``."""
    source = _branch_source("branches-ignore", '["release/*"]')

    assert (
        classify_trigger(source, BASELINE_PATH, "release/1.0/hotfix").expectation
        is TriggerExpectation.UNKNOWN
    )


def test_unparseable_yaml_is_unknown_rather_than_absent():
    definition = classify_trigger("name: [unterminated\n", BASELINE_PATH, "master")

    assert definition.expectation is TriggerExpectation.UNKNOWN


def test_the_repository_layout_yields_one_baseline_and_two_conditional_workflows():
    definitions = classify_workflow_files(DEFAULT_WORKFLOW_FILES, "master")

    by_path = {d.path: d.expectation for d in definitions}
    assert by_path[BASELINE_PATH] is TriggerExpectation.REQUIRED
    assert by_path[FILTERED_PATH] is TriggerExpectation.CONDITIONAL
    assert by_path[SECOND_FILTERED_PATH] is TriggerExpectation.CONDITIONAL


def test_branches_and_branches_ignore_together_are_unknown():
    """GitHub does not allow both keys for one event.

    Evaluating either of them on an invalid configuration would produce a
    confident NOT_EXPECTED, and a missing run would then never be questioned.
    """
    source = (
        "name: Example\non:\n  pull_request:\n"
        "    branches: [master]\n    branches-ignore: [master]\n"
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
    )

    assert (
        classify_trigger(source, BASELINE_PATH, "master").expectation
        is TriggerExpectation.UNKNOWN
    )


@pytest.mark.parametrize("base_ref", ["master", "release", "anything"])
def test_the_invalid_branch_combination_is_unknown_for_every_base(base_ref):
    """No base branch can make an uninterpretable configuration interpretable."""
    source = (
        "name: Example\non:\n  pull_request:\n"
        "    branches: [master]\n    branches-ignore: [release]\n"
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
    )

    assert (
        classify_trigger(source, BASELINE_PATH, base_ref).expectation
        is TriggerExpectation.UNKNOWN
    )


def test_paths_and_paths_ignore_together_stay_unreadable():
    """The path-filter side already behaved this way; the branch side now matches."""
    source = (
        "name: Example\non:\n  pull_request:\n    branches: [master]\n"
        '    paths: ["docs/**"]\n    paths-ignore: ["docs/**"]\n'
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
    )

    definition = classify_trigger(source, BASELINE_PATH, "master")

    assert definition.expectation is TriggerExpectation.CONDITIONAL
    assert definition.path_filter.mode == "unreadable"
