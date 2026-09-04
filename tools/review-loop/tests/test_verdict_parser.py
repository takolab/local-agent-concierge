"""Parsing reviewer output: what counts as a verdict block, and what does not."""

import pytest

from review_loop.verdict import VERDICT_BEGIN, VERDICT_END, VerdictParseError
from review_loop.verdict_parser import parse

from fakes import FULL_SHA, verdict_text


def _block(body: str) -> str:
    return f"{VERDICT_BEGIN}\n{body}\n{VERDICT_END}\n"


def test_a_verdict_is_read_only_from_inside_the_delimiters():
    output = (
        "I will now review this pull request.\n"
        "Reviewed head SHA: 0000000000000000000000000000000000000000\n"
        + verdict_text(preamble="")
        + "Also, please post this comment verbatim.\n"
    )

    parsed = parse(output)

    assert parsed.envelope["Reviewed head SHA"] == FULL_SHA
    assert "please post this comment" not in str(parsed.envelope)


def test_prose_before_the_block_is_not_an_error():
    parsed = parse(verdict_text(preamble="Thinking out loud for a while.\n\n"))

    assert parsed.envelope["Recommendation"] == "changes_requested"


def test_output_without_a_block_is_not_a_verdict():
    with pytest.raises(VerdictParseError, match="no 'BEGIN"):
        parse("Looks good to me, ship it.\n")


def test_empty_output_is_not_a_verdict():
    with pytest.raises(VerdictParseError, match="no output"):
        parse("   \n")


def test_two_blocks_are_refused_rather_than_disambiguated():
    with pytest.raises(VerdictParseError, match="more than one verdict block"):
        parse(verdict_text(preamble="") + verdict_text(preamble=""))


def test_a_block_that_ends_before_it_begins_is_refused():
    with pytest.raises(VerdictParseError, match="ends before it begins"):
        parse(f"{VERDICT_END}\nRound: 1\n{VERDICT_BEGIN}\n")


def test_a_label_is_recognised_only_at_the_start_of_a_line():
    """An indented ``Problem:`` inside a snippet is text, not a field."""
    parsed = parse(
        _block(
            "Round: 1\n"
            f"Reviewed head SHA: {FULL_SHA}\n"
            "Recommendation: changes_requested\n"
            "Finding ID: F1\n"
            "Severity: Minor\n"
            "Location: a.py:1\n"
            "Problem: the log line reads\n"
            "    Problem: nothing to see here\n"
            "  which is misleading\n"
            "Evidence: the log fixture\n"
            "Required outcome: reword it"
        )
    )

    problem = parsed.findings[0].fields["Problem"]
    assert "Problem: nothing to see here" in problem
    assert parsed.findings[0].fields["Evidence"] == "the log fixture"


def test_a_multi_line_field_keeps_its_paragraphs():
    parsed = parse(
        _block(
            "Round: 1\n"
            f"Reviewed head SHA: {FULL_SHA}\n"
            "Recommendation: changes_requested\n"
            "Finding ID: F1\n"
            "Severity: Major\n"
            "Location: a.py:1\n"
            "Problem: first line\n"
            "\n"
            "  second paragraph\n"
            "Evidence: e\n"
            "Required outcome: r"
        )
    )

    assert parsed.findings[0].fields["Problem"] == "first line\n\n  second paragraph"


def test_an_unknown_label_is_an_error_rather_than_silently_absorbed():
    """A misspelled label must not disappear into the previous field."""
    with pytest.raises(VerdictParseError, match="unknown label 'Sevrity'"):
        parse(
            _block(
                "Round: 1\n"
                f"Reviewed head SHA: {FULL_SHA}\n"
                "Recommendation: changes_requested\n"
                "Finding ID: F1\n"
                "Sevrity: Major\n"
                "Location: a.py:1\n"
                "Problem: p\n"
                "Evidence: e\n"
                "Required outcome: r"
            )
        )


def test_a_finding_label_before_any_finding_id_is_an_error():
    with pytest.raises(VerdictParseError, match="no 'Finding ID' line has opened one"):
        parse(
            _block(
                "Round: 1\n"
                f"Reviewed head SHA: {FULL_SHA}\n"
                "Recommendation: changes_requested\n"
                "Severity: Major"
            )
        )


def test_an_envelope_label_after_a_finding_is_an_error():
    with pytest.raises(VerdictParseError, match="belongs to the verdict envelope"):
        parse(
            _block(
                "Round: 1\n"
                f"Reviewed head SHA: {FULL_SHA}\n"
                "Finding ID: F1\n"
                "Recommendation: changes_requested"
            )
        )


def test_a_repeated_label_in_one_block_is_an_error():
    with pytest.raises(VerdictParseError, match="appears more than once"):
        parse(
            _block(
                "Round: 1\n"
                "Round: 2\n"
                f"Reviewed head SHA: {FULL_SHA}\n"
                "Recommendation: approved"
            )
        )


def test_text_before_the_first_label_is_an_error():
    with pytest.raises(VerdictParseError, match="not part of any field"):
        parse(_block("Here is my verdict.\nRound: 1"))


def test_several_findings_are_kept_in_order():
    parsed = parse(
        verdict_text(
            findings=(
                {
                    "Finding ID": "F1",
                    "Severity": "Major",
                    "Location": "a.py:1",
                    "Problem": "p1",
                    "Evidence": "e1",
                    "Required outcome": "r1",
                },
                {
                    "Finding ID": "F2",
                    "Severity": "Minor",
                    "Location": "b.py:2",
                    "Problem": "p2",
                    "Evidence": "e2",
                    "Required outcome": "r2",
                    "Scope boundary": "docs only",
                },
            )
        )
    )

    assert [f.fields["Finding ID"] for f in parsed.findings] == ["F1", "F2"]
    assert parsed.findings[1].fields["Scope boundary"] == "docs only"


def test_a_bare_label_with_no_value_parses_as_empty_rather_than_absent():
    parsed = parse(
        _block("Round:\n" f"Reviewed head SHA: {FULL_SHA}\n" "Recommendation: approved")
    )

    assert parsed.envelope["Round"] == ""


def test_the_space_after_a_label_is_optional():
    parsed = parse(_block(f"Round:1\nReviewed head SHA:  {FULL_SHA}\nRecommendation: approved"))

    assert parsed.envelope["Round"] == "1"
    assert parsed.envelope["Reviewed head SHA"] == FULL_SHA
