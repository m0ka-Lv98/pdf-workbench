from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class PdfMergeMetadataPolicy(StrEnum):
    NONE = "none"
    SELECTED_SOURCE = "selected_source"


class PdfMergeBookmarkPolicy(StrEnum):
    NONE = "none"
    GROUPED_BY_SOURCE = "grouped_by_source"


@dataclass(frozen=True, slots=True)
class PdfMergeInput:
    path: Path
    page_count: int
    label: str

    def __post_init__(self) -> None:
        resolved_path = self.path.expanduser().resolve()
        if self.path != resolved_path:
            raise ValueError("merge input path must be resolved")
        if not isinstance(self.page_count, int) or isinstance(self.page_count, bool):
            raise ValueError("page_count must be a positive integer")
        if self.page_count <= 0:
            raise ValueError("page_count must be positive")
        if not self.label.strip():
            raise ValueError("merge input label must not be empty")


@dataclass(frozen=True, slots=True)
class PdfMergeOutputRange:
    input_path: Path
    label: str
    source_page_count: int
    output_start_index: int
    output_end_index: int

    @property
    def display_range(self) -> str:
        return f"{self.output_start_index + 1}-{self.output_end_index + 1}"


@dataclass(frozen=True, slots=True)
class PdfMergePlan:
    inputs: tuple[PdfMergeInput, ...]
    output_path: Path
    metadata_policy: PdfMergeMetadataPolicy
    metadata_source_path: Path | None
    bookmark_policy: PdfMergeBookmarkPolicy
    output_ranges: tuple[PdfMergeOutputRange, ...]
    total_page_count: int

    def __post_init__(self) -> None:
        resolved_output = self.output_path.expanduser().resolve()
        if self.output_path != resolved_output:
            raise ValueError("output path must be resolved")
        if len(self.inputs) < 2:
            raise ValueError("at least two input PDFs are required")
        seen_paths: set[Path] = set()
        for item in self.inputs:
            if item.path in seen_paths:
                raise ValueError("input PDFs must be unique")
            seen_paths.add(item.path)
            if item.path == self.output_path:
                raise ValueError("output path must not match an input PDF")
        if not isinstance(self.metadata_policy, PdfMergeMetadataPolicy):
            raise ValueError("metadata_policy must be PdfMergeMetadataPolicy")
        if not isinstance(self.bookmark_policy, PdfMergeBookmarkPolicy):
            raise ValueError("bookmark_policy must be PdfMergeBookmarkPolicy")
        if self.metadata_policy is PdfMergeMetadataPolicy.NONE:
            if self.metadata_source_path is not None:
                raise ValueError("metadata source must be omitted when metadata is not copied")
        elif self.metadata_source_path not in seen_paths:
            raise ValueError("metadata source must be one of the merge inputs")
        if len(self.output_ranges) != len(self.inputs):
            raise ValueError("output range mapping must match inputs")
        cursor = 0
        for item, output_range in zip(self.inputs, self.output_ranges, strict=True):
            if output_range.input_path != item.path:
                raise ValueError("output range input order must match merge inputs")
            if output_range.label != item.label:
                raise ValueError("output range label must match merge input")
            if output_range.source_page_count != item.page_count:
                raise ValueError("output range page count must match merge input")
            if output_range.output_start_index != cursor:
                raise ValueError("output ranges must be continuous")
            expected_end = cursor + item.page_count - 1
            if output_range.output_end_index != expected_end:
                raise ValueError("output range end must match page count")
            cursor = expected_end + 1
        if self.total_page_count != cursor:
            raise ValueError("total_page_count must equal input page sum")


def build_pdf_merge_plan(
    inputs: tuple[PdfMergeInput, ...],
    output_path: Path,
    *,
    metadata_policy: PdfMergeMetadataPolicy = PdfMergeMetadataPolicy.NONE,
    metadata_source_path: Path | None = None,
    bookmark_policy: PdfMergeBookmarkPolicy = PdfMergeBookmarkPolicy.NONE,
) -> PdfMergePlan:
    resolved_output = output_path.expanduser().resolve()
    resolved_metadata_source = (
        metadata_source_path.expanduser().resolve() if metadata_source_path is not None else None
    )
    ranges: list[PdfMergeOutputRange] = []
    cursor = 0
    for item in inputs:
        start = cursor
        end = start + item.page_count - 1
        ranges.append(
            PdfMergeOutputRange(
                input_path=item.path,
                label=item.label,
                source_page_count=item.page_count,
                output_start_index=start,
                output_end_index=end,
            )
        )
        cursor = end + 1
    return PdfMergePlan(
        inputs=inputs,
        output_path=resolved_output,
        metadata_policy=metadata_policy,
        metadata_source_path=resolved_metadata_source,
        bookmark_policy=bookmark_policy,
        output_ranges=tuple(ranges),
        total_page_count=cursor,
    )
