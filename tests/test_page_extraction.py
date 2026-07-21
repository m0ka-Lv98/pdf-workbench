from __future__ import annotations

import pytest

from pdf_workbench.domain.page_extraction import (
    PageExtractionPlan,
    build_page_extraction_plan,
    build_selected_page_extraction_plan,
    parse_page_range_extraction_plan,
)


def test_selected_indexes_plan_sorts_and_deduplicates_selection() -> None:
    plan = build_selected_page_extraction_plan(6, (4, 1, 4, 2))

    assert plan.page_count == 6
    assert plan.source_page_indexes == (1, 2, 4)
    assert plan.output_page_count == 3
    assert plan.source_to_output == ((1, 0), (2, 1), (4, 2))


def test_range_parser_accepts_whitespace_and_overlapping_ranges() -> None:
    plan = parse_page_range_extraction_plan(10, " 1 - 3, 3, 5, 8 -10 ")

    assert plan.source_page_indexes == (0, 1, 2, 4, 7, 8, 9)


@pytest.mark.parametrize(
    ("text", "message"),
    (
        ("", "ページ範囲を入力してください"),
        ("0", "ページ番号は1以上"),
        ("-1", "形式が不正"),
        ("6", "ページ数を超えています"),
        ("5-3", "昇順"),
        ("1,,2", "空のページ範囲"),
        ("a", "形式が不正"),
        ("\uff11", "半角数字"),
    ),
)
def test_range_parser_rejects_invalid_input(text: str, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        parse_page_range_extraction_plan(5, text)


def test_range_parser_rejects_bool_page_count() -> None:
    with pytest.raises(TypeError):
        parse_page_range_extraction_plan(True, "1")


def test_raw_plan_rejects_unsorted_duplicate_or_invalid_indexes() -> None:
    with pytest.raises(ValueError, match="sorted"):
        build_page_extraction_plan(5, (2, 1))
    with pytest.raises(ValueError, match="unique"):
        build_page_extraction_plan(5, (1, 1))
    with pytest.raises(ValueError, match="range"):
        build_page_extraction_plan(5, (5,))


def test_raw_plan_validates_source_to_output_mapping() -> None:
    with pytest.raises(ValueError, match="source_to_output"):
        PageExtractionPlan(
            page_count=5,
            source_page_indexes=(1, 3),
            source_to_output=((1, 0), (3, 9)),
        )


def test_first_last_and_full_range() -> None:
    assert parse_page_range_extraction_plan(4, "1,4").source_page_indexes == (0, 3)
    assert parse_page_range_extraction_plan(4, "1-4").source_page_indexes == (0, 1, 2, 3)
