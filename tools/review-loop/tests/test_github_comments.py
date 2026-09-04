"""The GitHub write boundary, asserted at the source level.

PR #28's client is read-only by construction, and the tests in
``test_cli.py`` keep it that way. This slice adds exactly one write, and it
lives here: one method, one HTTP method, one endpoint.
"""

import ast
from pathlib import Path

import pytest

from review_loop import github_comments
from review_loop.github_comments import IssueCommentReader, IssueCommentWriter

_SOURCE = Path(github_comments.__file__).read_text()
_TREE = ast.parse(_SOURCE)
_STRINGS = {
    node.value
    for node in ast.walk(_TREE)
    if isinstance(node, ast.Constant) and isinstance(node.value, str)
}


def test_only_get_and_post_appear_in_this_module():
    assert not {"PATCH", "PUT", "DELETE"} & _STRINGS


@pytest.mark.parametrize(
    "forbidden", ["/reviews", "/labels", "/merge", "/dispatches", "/rerun", "/pulls"]
)
def test_no_endpoint_other_than_issue_comments_is_referenced(forbidden):
    assert forbidden not in _SOURCE


def test_the_writer_exposes_exactly_one_public_method():
    public = [
        name
        for name in vars(IssueCommentWriter)
        if not name.startswith("_")
    ]

    assert public == ["create_comment"]


def test_the_reader_exposes_exactly_one_public_method():
    public = [name for name in vars(IssueCommentReader) if not name.startswith("_")]

    assert public == ["list_comments"]


def test_the_reader_issues_no_write_method():
    """The read path names GET and never the write constant."""
    reader_source = _SOURCE.split("class IssueCommentReader")[1].split("class IssueCommentWriter")[0]

    assert "_READ_METHOD" in reader_source
    assert "_WRITE_METHOD" not in reader_source


def test_the_write_method_constant_is_post():
    assert github_comments._WRITE_METHOD == "POST"
    assert github_comments._READ_METHOD == "GET"


def test_the_comment_body_is_never_part_of_the_command_line():
    """A body travels as JSON on stdin, so reviewer text cannot become argv."""
    writer_source = _SOURCE.split("class IssueCommentWriter")[1]
    argument_vector = writer_source.split("_run_gh(")[1].split("stdin=")[0]

    assert '"--input"' in argument_vector
    assert "body" not in argument_vector
    assert 'stdin=json.dumps({"body": body})' in writer_source


@pytest.mark.parametrize("cls", [IssueCommentReader, IssueCommentWriter])
def test_a_repository_must_be_owner_and_name(cls):
    with pytest.raises(ValueError):
        cls("local-agent-concierge")


def test_no_other_module_in_the_package_can_write_to_github():
    """Exactly one module may name a write method or a comment endpoint.

    String literals only: prose mentioning POST, and identifiers like
    ``MAX_OUTPUT_BYTES``, are not call sites.
    """
    package = Path(github_comments.__file__).parent
    write_methods = {"POST", "PATCH", "PUT", "DELETE"}
    offenders = {}

    for module in sorted(package.glob("*.py")):
        if module.name == "github_comments.py":
            continue
        literals = {
            node.value
            for node in ast.walk(ast.parse(module.read_text()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        hits = sorted(
            literal
            for literal in literals
            if literal in write_methods or "issues/" in literal
        )
        if hits:
            offenders[module.name] = hits

    assert offenders == {}, offenders
