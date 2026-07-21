from __future__ import annotations

from pathlib import Path

import pytest

from pdf_workbench.domain.pdf_merge import (
    PdfMergeBookmarkPolicy,
    PdfMergeInput,
    PdfMergeMetadataPolicy,
    PdfMergeOutputRange,
    PdfMergePlan,
    build_pdf_merge_plan,
)


def merge_input(path: Path, page_count: int = 2) -> PdfMergeInput:
    return PdfMergeInput(path=path.resolve(), page_count=page_count, label=path.name)


def test_merge_plan_builds_continuous_output_ranges(tmp_path: Path) -> None:
    first = merge_input(tmp_path / "a.pdf", 2)
    second = merge_input(tmp_path / "b.pdf", 3)

    plan = build_pdf_merge_plan((first, second), tmp_path / "merged.pdf")

    assert plan.total_page_count == 5
    assert [item.display_range for item in plan.output_ranges] == ["1-2", "3-5"]
    assert [item.input_path for item in plan.output_ranges] == [first.path, second.path]


def test_merge_plan_rejects_duplicate_canonical_inputs(tmp_path: Path) -> None:
    first = merge_input(tmp_path / "same.pdf", 1)

    with pytest.raises(ValueError, match="unique"):
        build_pdf_merge_plan((first, first), tmp_path / "merged.pdf")


def test_merge_plan_rejects_output_matching_input(tmp_path: Path) -> None:
    first = merge_input(tmp_path / "a.pdf", 1)
    second = merge_input(tmp_path / "b.pdf", 1)

    with pytest.raises(ValueError, match="output path"):
        build_pdf_merge_plan((first, second), first.path)


def test_merge_plan_requires_selected_metadata_source_to_be_input(tmp_path: Path) -> None:
    first = merge_input(tmp_path / "a.pdf", 1)
    second = merge_input(tmp_path / "b.pdf", 1)

    with pytest.raises(ValueError, match="metadata source"):
        build_pdf_merge_plan(
            (first, second),
            tmp_path / "merged.pdf",
            metadata_policy=PdfMergeMetadataPolicy.SELECTED_SOURCE,
            metadata_source_path=tmp_path / "other.pdf",
        )


def test_merge_input_rejects_bool_page_count(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        PdfMergeInput(path=(tmp_path / "a.pdf").resolve(), page_count=True, label="a.pdf")


def test_merge_input_rejects_unresolved_nonpositive_and_empty_label(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="resolved"):
        PdfMergeInput(path=Path("relative.pdf"), page_count=1, label="relative.pdf")
    with pytest.raises(ValueError, match="positive"):
        PdfMergeInput(path=(tmp_path / "a.pdf").resolve(), page_count=0, label="a.pdf")
    with pytest.raises(ValueError, match="label"):
        PdfMergeInput(path=(tmp_path / "a.pdf").resolve(), page_count=1, label=" ")


def test_merge_plan_rejects_tampered_output_mapping(tmp_path: Path) -> None:
    first = merge_input(tmp_path / "a.pdf", 2)
    second = merge_input(tmp_path / "b.pdf", 1)
    valid = build_pdf_merge_plan((first, second), tmp_path / "merged.pdf")
    tampered_range = PdfMergeOutputRange(
        input_path=second.path,
        label=second.label,
        source_page_count=second.page_count,
        output_start_index=2,
        output_end_index=2,
    )

    with pytest.raises(ValueError, match="input order"):
        PdfMergePlan(
            inputs=(first, second),
            output_path=valid.output_path,
            metadata_policy=valid.metadata_policy,
            metadata_source_path=valid.metadata_source_path,
            bookmark_policy=valid.bookmark_policy,
            output_ranges=(tampered_range, valid.output_ranges[1]),
            total_page_count=valid.total_page_count,
        )


def test_merge_plan_accepts_bookmark_policy_enum(tmp_path: Path) -> None:
    first = merge_input(tmp_path / "a.pdf", 1)
    second = merge_input(tmp_path / "b.pdf", 1)

    plan = build_pdf_merge_plan(
        (first, second),
        tmp_path / "merged.pdf",
        bookmark_policy=PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE,
    )

    assert plan.bookmark_policy is PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE
