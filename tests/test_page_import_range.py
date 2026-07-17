from __future__ import annotations

import pytest

from pdf_workbench.domain.page_insertion import (
    SourcePageSelection,
    parse_source_page_selection,
)


def test_parse_source_page_selection_supports_all_single_list_range_and_mixed() -> None:
    assert parse_source_page_selection(5, "all").page_indexes == (0, 1, 2, 3, 4)
    assert parse_source_page_selection(5, "1").page_indexes == (0,)
    assert parse_source_page_selection(5, "1,3,5").page_indexes == (0, 2, 4)
    assert parse_source_page_selection(6, "2-6").page_indexes == (1, 2, 3, 4, 5)
    assert parse_source_page_selection(8, " 1, 3-5 , 8 ").page_indexes == (0, 2, 3, 4, 7)


@pytest.mark.parametrize(
    "raw_value",
    [
        "1,1",
        "3-5,4",
        "5-2",
        "0",
        "-1",
        "6",
        "1,",
        "1,,2",
        "1.5",
        "True",
        "\uff11",
    ],
)
def test_parse_source_page_selection_rejects_invalid_input(raw_value: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        parse_source_page_selection(5, raw_value)


def test_source_page_selection_constructor_enforces_canonical_invariants() -> None:
    selection = SourcePageSelection(source_page_count=5, page_indexes=(0, 2, 3))
    assert selection.page_indexes == (0, 2, 3)

    with pytest.raises(ValueError, match="ascending"):
        SourcePageSelection(source_page_count=5, page_indexes=(2, 0))
    with pytest.raises(ValueError, match="unique"):
        SourcePageSelection(source_page_count=5, page_indexes=(0, 0))
    with pytest.raises(ValueError, match="range"):
        SourcePageSelection(source_page_count=5, page_indexes=(5,))
