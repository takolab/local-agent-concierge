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


def test_a_workflow_targeting_another_base_branch_is_not_expected():
    source = """\
name: Release
on:
  pull_request:
    branches: [release/*]
jobs:
  test:
    runs-on: ubuntu-latest
"""

    assert (
        classify_trigger(source, BASELINE_PATH, "master").expectation
        is TriggerExpectation.NOT_EXPECTED
    )
    assert (
        classify_trigger(source, BASELINE_PATH, "release/1.0").expectation
        is TriggerExpectation.REQUIRED
    )


def test_a_branches_ignore_match_removes_the_workflow_from_expectation():
    source = """\
name: Example
on:
  pull_request:
    branches-ignore: [master]
jobs:
  test:
    runs-on: ubuntu-latest
"""

    assert (
        classify_trigger(source, BASELINE_PATH, "master").expectation
        is TriggerExpectation.NOT_EXPECTED
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
