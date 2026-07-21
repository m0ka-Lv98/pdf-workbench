from __future__ import annotations

import pytest

from pdf_workbench.domain.page_split import (
    PageSplitChunk,
    PageSplitPlan,
    build_max_pages_split_plan,
    build_page_range_split_plan,
    build_split_output_filename,
)


def test_build_page_range_split_plan_accepts_valid_manual_ranges() -> None:
    plan = build_page_range_split_plan(
        10,
        " 1-3\n\n4\n5-10 ",
        source_stem="report",
    )

    assert plan.output_count == 3
    assert [chunk.display_range for chunk in plan.chunks] == ["1-3", "4-4", "5-10"]
    assert [chunk.extraction_plan.source_page_indexes for chunk in plan.chunks] == [
        (0, 1, 2),
        (3,),
        (4, 5, 6, 7, 8, 9),
    ]
    assert [chunk.filename for chunk in plan.chunks] == [
        "report_pages_0001-0003.pdf",
        "report_pages_0004-0004.pdf",
        "report_pages_0005-0010.pdf",
    ]


def test_build_page_range_split_plan_rejects_invalid_inputs() -> None:
    cases = [
        "",
        "1-2\n2-3",
        "1-2\n4-5",
        "3-4\n1-2",
        "1-3\n4-2",
        "0-1\n2-3",
        "-1\n2-3",
        "1-a\n2-3",
        "1-2",
        "1-5",
    ]
    for text in cases:
        with pytest.raises((TypeError, ValueError)):
            build_page_range_split_plan(5, text, source_stem="source")


def test_build_page_range_split_plan_rejects_non_ascii_and_bool() -> None:
    with pytest.raises(ValueError, match="半角数字"):
        build_page_range_split_plan(2, "\uff11-\uff12", source_stem="source")
    with pytest.raises(TypeError):
        build_page_range_split_plan(True, "1\n2", source_stem="source")


def test_build_max_pages_split_plan_handles_exact_remainder_and_one_page_chunks() -> None:
    exact = build_max_pages_split_plan(10, 5, source_stem="report")
    remainder = build_max_pages_split_plan(11, 5, source_stem="report")
    single_pages = build_max_pages_split_plan(3, 1, source_stem="report")

    assert [chunk.display_range for chunk in exact.chunks] == ["1-5", "6-10"]
    assert [chunk.display_range for chunk in remainder.chunks] == ["1-5", "6-10", "11-11"]
    assert [chunk.display_range for chunk in single_pages.chunks] == ["1-1", "2-2", "3-3"]
    assert exact.chunks[0].output_page_count == 5


def test_build_max_pages_split_plan_rejects_invalid_values() -> None:
    for value in (True, 0, -1, 5):
        with pytest.raises((TypeError, ValueError)):
            build_max_pages_split_plan(5, value, source_stem="report")
    with pytest.raises(ValueError):
        build_max_pages_split_plan(1, 1, source_stem="report")


def test_split_filename_padding_and_stem_handling() -> None:
    plan = build_max_pages_split_plan(10001, 10000, source_stem="report.v1")

    assert [chunk.filename for chunk in plan.chunks] == [
        "report.v1_pages_00001-10000.pdf",
        "report.v1_pages_10001-10001.pdf",
    ]


def test_build_split_output_filename_rejects_invalid_ranges_and_stems() -> None:
    assert build_split_output_filename("source", 12, 0, 1) == "source_pages_0001-0002.pdf"
    for source_stem in ("", "   ", ".", "..", "../source", "dir/source", "dir\\source"):
        with pytest.raises((TypeError, ValueError)):
            build_split_output_filename(source_stem, 12, 0, 1)
    for start_index, end_index in ((-1, 1), (2, 1), (0, 12)):
        with pytest.raises(ValueError):
            build_split_output_filename("source", 12, start_index, end_index)


def test_page_split_plan_rejects_tampered_chunk_invariants() -> None:
    plan = build_max_pages_split_plan(4, 2, source_stem="source")
    first, second = plan.chunks

    with pytest.raises(ValueError):
        PageSplitChunk(
            chunk_index=0,
            source_start_index=0,
            source_end_index=1,
            extraction_plan=second.extraction_plan,
            display_range="1-2",
            filename="source_pages_0001-0002.pdf",
        )
    with pytest.raises(ValueError):
        PageSplitPlan(source_page_count=4, source_stem="source", chunks=(second, first))


def test_page_split_chunk_rejects_unsafe_filename_and_display_range() -> None:
    plan = build_max_pages_split_plan(4, 2, source_stem="source")
    first = plan.chunks[0]
    for filename in (
        "",
        "   ",
        ".",
        "..",
        "/tmp/out.pdf",
        "nested/out.pdf",
        "nested\\out.pdf",
        "out.txt",
        " out.pdf",
    ):
        with pytest.raises((TypeError, ValueError)):
            PageSplitChunk(
                chunk_index=0,
                source_start_index=0,
                source_end_index=1,
                extraction_plan=first.extraction_plan,
                display_range="1-2",
                filename=filename,
            )
    with pytest.raises(ValueError, match="display_range"):
        PageSplitChunk(
            chunk_index=0,
            source_start_index=0,
            source_end_index=1,
            extraction_plan=first.extraction_plan,
            display_range="1-3",
            filename="source_pages_0001-0002.pdf",
        )


def test_page_split_plan_rejects_non_deterministic_filename() -> None:
    plan = build_max_pages_split_plan(4, 2, source_stem="source")
    first = PageSplitChunk(
        chunk_index=0,
        source_start_index=0,
        source_end_index=1,
        extraction_plan=plan.chunks[0].extraction_plan,
        display_range="1-2",
        filename="other_pages_0001-0002.pdf",
    )

    with pytest.raises(ValueError, match="filename"):
        PageSplitPlan(source_page_count=4, source_stem="source", chunks=(first, plan.chunks[1]))
