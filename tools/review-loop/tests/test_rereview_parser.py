"""The Bounded Re-Review Response parser: labelled fields, or a refusal."""

import pytest

from review_loop.rereview import (
    RE_REVIEW_BEGIN,
    RE_REVIEW_END,
    ReReviewParseError,
)
from review_loop.rereview_parser import parse

from rereview_fakes import PUSHED_SHA, fresh_block, resolution_block, rereview_text


def _block(*lines: str) -> str:
    return "\n".join([RE_REVIEW_BEGIN, *lines, RE_REVIEW_END]) + "\n"


def test_the_envelope_the_resolutions_and_the_fresh_findings_are_separate():
    parsed = parse(
        rereview_text(
            recommendation="changes_requested",
            resolutions=(
                resolution_block("F1"),
                resolution_block("F2", "UNRESOLVED", reason="the claim is still there"),
            ),
            fresh=(fresh_block(),),
        )
    )

    assert parsed.envelope["Reviewed head SHA"] == PUSHED_SHA
    assert [b.fields["Finding ID"] for b in parsed.resolutions] == ["F1", "F2"]
    assert [b.fields["Fresh finding ID"] for b in parsed.fresh] == ["R2.F1"]
    # 'Evidence' is spelled the same in both kinds of block and lands in the
    # one its opener chose.
    assert "Evidence" in parsed.resolutions[0].fields
    assert "Evidence" in parsed.fresh[0].fields


def test_a_re_review_with_no_fresh_finding_parses_to_an_empty_section():
    parsed = parse(rereview_text())

    assert len(parsed.resolutions) == 2
    assert parsed.fresh == []


def test_only_the_delimited_block_is_read():
    text = (
        "Round: 99\nReviewed head SHA: nonsense\n"
        + rereview_text(preamble="")
        + "Recommendation: approved\n"
    )
    parsed = parse(text)

    assert parsed.envelope["Round"] == "2"
    assert parsed.envelope["Reviewed head SHA"] == PUSHED_SHA


def test_a_paragraph_field_runs_to_the_next_label():
    parsed = parse(
        _block(
            "Round: 2",
            f"Reviewed head SHA: {PUSHED_SHA}",
            "Recommendation: approved",
            "Finding ID: F1",
            "Resolution: RESOLVED",
            "Evidence: the handler raises now",
            "  and this indented Problem: line is content",
            "Reason: none needed",
        )
    )

    evidence = parsed.resolutions[0].fields["Evidence"]
    assert "indented Problem: line is content" in evidence
    assert parsed.resolutions[0].fields["Reason"] == "none needed"


def test_no_block_at_all_is_a_parse_error():
    with pytest.raises(ReReviewParseError, match="contains no"):
        parse("I looked at the fix and it seems fine.")


def test_empty_output_is_a_parse_error():
    with pytest.raises(ReReviewParseError, match="no output"):
        parse("   \n")


def test_two_blocks_are_undecidable():
    with pytest.raises(ReReviewParseError, match="more than one re-review block"):
        parse(rereview_text() + rereview_text())


def test_an_end_before_a_begin_is_a_parse_error():
    with pytest.raises(ReReviewParseError, match="ends before it begins"):
        parse(f"{RE_REVIEW_END}\nRound: 2\n{RE_REVIEW_BEGIN}\n")


def test_an_unknown_label_is_an_error_never_content():
    with pytest.raises(ReReviewParseError, match="unknown label 'Resolutionn'"):
        parse(
            _block(
                "Round: 2",
                f"Reviewed head SHA: {PUSHED_SHA}",
                "Recommendation: approved",
                "Finding ID: F1",
                "Resolutionn: RESOLVED",
            )
        )


def test_a_stray_line_outside_every_field_is_an_error():
    with pytest.raises(ReReviewParseError, match="not part of any field"):
        parse(_block("this line belongs to nothing", "Round: 2"))


def test_an_envelope_label_after_a_block_began_is_an_error():
    with pytest.raises(ReReviewParseError, match="belongs to the re-review envelope"):
        parse(
            _block(
                "Round: 2",
                f"Reviewed head SHA: {PUSHED_SHA}",
                "Finding ID: F1",
                "Resolution: RESOLVED",
                "Evidence: fixed",
                "Recommendation: approved",
            )
        )


def test_a_finding_field_before_any_opener_is_an_error():
    with pytest.raises(ReReviewParseError, match="no 'Finding ID' or 'Fresh finding ID'"):
        parse(
            _block(
                "Round: 2",
                f"Reviewed head SHA: {PUSHED_SHA}",
                "Recommendation: approved",
                "Resolution: RESOLVED",
            )
        )


def test_a_fresh_finding_field_inside_a_resolution_is_an_error():
    with pytest.raises(ReReviewParseError, match="belongs to a fresh finding"):
        parse(
            _block(
                "Round: 2",
                f"Reviewed head SHA: {PUSHED_SHA}",
                "Recommendation: approved",
                "Finding ID: F1",
                "Resolution: RESOLVED",
                "Severity: Major",
            )
        )


def test_a_resolution_field_inside_a_fresh_finding_is_an_error():
    with pytest.raises(ReReviewParseError, match="belongs to a resolution"):
        parse(
            _block(
                "Round: 2",
                f"Reviewed head SHA: {PUSHED_SHA}",
                "Recommendation: approved",
                "Fresh finding ID: R2.F1",
                "Resolution: RESOLVED",
            )
        )


def test_a_resolution_after_the_fresh_section_began_is_an_error():
    with pytest.raises(ReReviewParseError, match="fresh-findings section has already"):
        parse(
            _block(
                "Round: 2",
                f"Reviewed head SHA: {PUSHED_SHA}",
                "Recommendation: changes_requested",
                *fresh_block(),
                *resolution_block("F1"),
            )
        )


def test_a_repeated_label_in_one_block_is_an_error():
    with pytest.raises(ReReviewParseError, match="appears more than once"):
        parse(
            _block(
                "Round: 2",
                f"Reviewed head SHA: {PUSHED_SHA}",
                "Recommendation: approved",
                "Finding ID: F1",
                "Resolution: RESOLVED",
                "Resolution: UNRESOLVED",
            )
        )


def test_the_same_label_in_two_blocks_is_fine():
    parsed = parse(
        rereview_text(
            resolutions=(resolution_block("F1"), resolution_block("F2")),
        )
    )

    assert [b.fields["Resolution"] for b in parsed.resolutions] == [
        "RESOLVED",
        "RESOLVED",
    ]
