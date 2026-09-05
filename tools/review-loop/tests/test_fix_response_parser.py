"""Reading a Coding Agent's output: what is a response, and what is not."""

import pytest

from review_loop.fix_response import (
    FIX_RESPONSE_BEGIN,
    FIX_RESPONSE_END,
    FixResponseParseError,
)
from review_loop.fix_response_parser import extract_blocks, parse, parse_files_changed

from fix_fakes import FULL_SHA, response_text


# --------------------------------------------------------------------------
# Block extraction
# --------------------------------------------------------------------------


def test_one_block_is_read_and_its_preamble_is_not():
    raws = parse(response_text(preamble="Let me think about this first.\n\n"))

    assert len(raws) == 1
    assert raws[0].fields["Finding ID"] == "F1"
    assert "think about this" not in "".join(raws[0].fields.values())


def test_several_blocks_are_read_in_order():
    output = response_text(finding_id="F1") + response_text(finding_id="F2", preamble="")

    raws = parse(output)

    assert [raw.fields["Finding ID"] for raw in raws] == ["F1", "F2"]


def test_text_between_blocks_is_ignored():
    output = (
        response_text(finding_id="F1")
        + "\nNow for the second finding.\n\n"
        + response_text(finding_id="F2", preamble="")
    )

    assert [raw.fields["Finding ID"] for raw in parse(output)] == ["F1", "F2"]


def test_no_output_at_all_is_not_a_response():
    with pytest.raises(FixResponseParseError, match="produced no output"):
        parse("   \n\n")


def test_output_without_a_block_is_not_a_response():
    """A plausible prose answer is exactly what the contract exists to refuse."""
    with pytest.raises(FixResponseParseError, match="contains no"):
        parse("I fixed F1 by enforcing the limit. All tests pass.\n")


def test_a_block_that_never_closes_is_refused():
    with pytest.raises(FixResponseParseError, match="never closed"):
        parse(f"{FIX_RESPONSE_BEGIN}\nFinding ID: F1\n")


def test_a_block_that_closes_without_opening_is_refused():
    with pytest.raises(FixResponseParseError, match="without having begun"):
        parse(f"Finding ID: F1\n{FIX_RESPONSE_END}\n")


def test_a_nested_block_is_refused_rather_than_flattened():
    output = f"{FIX_RESPONSE_BEGIN}\n{FIX_RESPONSE_BEGIN}\nFinding ID: F1\n{FIX_RESPONSE_END}\n"

    with pytest.raises(FixResponseParseError, match="while another is still open"):
        parse(output)


def test_extract_blocks_returns_only_the_delimited_text():
    blocks = extract_blocks(response_text())

    assert len(blocks) == 1
    assert FIX_RESPONSE_BEGIN not in blocks[0]
    assert "Finding ID: F1" in blocks[0]


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------


def test_a_multi_line_field_runs_to_the_next_label():
    output = response_text(summary="first line\n  second line\n  third line")

    assert parse(output)[0].fields["Summary"] == "first line\n  second line\n  third line"


def test_an_indented_label_shaped_line_is_content():
    """A `Summary:` inside a quoted snippet must not open a field."""
    output = response_text(summary="the code said\n  Problem: nothing\n  which is wrong")

    assert "Problem" not in parse(output)[0].fields


def test_an_unknown_label_at_column_zero_is_an_error():
    output = response_text().replace("Summary:", "Sumary:")

    with pytest.raises(FixResponseParseError, match="unknown label 'Sumary'"):
        parse(output)


def test_a_duplicate_single_line_label_is_an_error():
    output = response_text().replace(
        "Outcome: fixed", "Outcome: fixed\nOutcome: escalate"
    )

    with pytest.raises(FixResponseParseError, match="appears more than once"):
        parse(output)


def test_a_duplicate_multi_line_label_is_an_error():
    """Two consecutive `Summary:` lines: the second is not a continuation."""
    output = response_text().replace(
        "Summary: enforced", "Summary: first answer\nSummary: enforced"
    )

    with pytest.raises(FixResponseParseError, match="appears more than once"):
        parse(output)


def test_a_stray_line_before_any_label_is_an_error():
    output = f"{FIX_RESPONSE_BEGIN}\nall done\nFinding ID: F1\n{FIX_RESPONSE_END}\n"

    with pytest.raises(FixResponseParseError, match="not part of any field"):
        parse(output)


# --------------------------------------------------------------------------
# Files changed
# --------------------------------------------------------------------------


def test_a_dash_list_is_read_as_paths():
    assert parse_files_changed("- a/b.py\n- c/d.py") == ("a/b.py", "c/d.py")


def test_an_asterisk_list_is_read_as_paths():
    assert parse_files_changed("* a/b.py") == ("a/b.py",)


@pytest.mark.parametrize("value", ["(none)", "none", "-", "", "n/a", "N/A"])
def test_the_explicit_nothing_spellings_are_an_empty_list(value):
    assert parse_files_changed(value) == ()


def test_a_bare_path_with_no_marker_is_refused():
    """Indistinguishable from a wrapped continuation, so it is not guessed at."""
    with pytest.raises(FixResponseParseError, match="must be written as"):
        parse_files_changed("a/b.py\nc/d.py")


def test_a_list_read_through_a_whole_response():
    output = response_text(files=("x/y.py", "x/tests/test_y.py"))

    fields = parse(output)[0].fields
    assert parse_files_changed(fields["Files changed"]) == ("x/y.py", "x/tests/test_y.py")


def test_an_empty_list_read_through_a_whole_response():
    output = response_text(
        outcome="unable_to_fix", files=(), verification=None, reason="the API is gone"
    )

    fields = parse(output)[0].fields
    assert parse_files_changed(fields["Files changed"]) == ()


def test_the_target_sha_survives_parsing_verbatim():
    assert parse(response_text())[0].fields["Target head SHA"] == FULL_SHA
