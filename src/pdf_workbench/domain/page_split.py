from __future__ import annotations

import re
from dataclasses import dataclass
from numbers import Integral
from pathlib import Path

from pdf_workbench.domain.page_extraction import PageExtractionPlan, build_page_extraction_plan

_SPLIT_RANGE_RE = re.compile(r"^([0-9]+)(?:\s*-\s*([0-9]+))?$")


def _require_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError(f"{label} must be an integer")
    return int(value)


def _validate_page_count(value: object) -> int:
    page_count = _require_int(value, label="page_count")
    if page_count <= 1:
        raise ValueError("分割には2ページ以上のPDFが必要です")
    return page_count


@dataclass(frozen=True, slots=True)
class PageSplitChunk:
    chunk_index: int
    source_start_index: int
    source_end_index: int
    extraction_plan: PageExtractionPlan
    display_range: str
    filename: str

    def __post_init__(self) -> None:
        chunk_index = _require_int(self.chunk_index, label="chunk_index")
        source_start_index = _require_int(self.source_start_index, label="source_start_index")
        source_end_index = _require_int(self.source_end_index, label="source_end_index")
        if chunk_index < 0:
            raise ValueError("chunk_index must be non-negative")
        if source_start_index < 0 or source_end_index < source_start_index:
            raise ValueError("chunk range must be non-empty and sorted")
        if not self.display_range:
            raise ValueError("display_range must not be empty")
        if not self.filename:
            raise ValueError("filename must not be empty")
        expected_indexes = tuple(range(source_start_index, source_end_index + 1))
        if self.extraction_plan.source_page_indexes != expected_indexes:
            raise ValueError("chunk extraction plan must match the chunk range")
        object.__setattr__(self, "chunk_index", chunk_index)
        object.__setattr__(self, "source_start_index", source_start_index)
        object.__setattr__(self, "source_end_index", source_end_index)

    @property
    def output_page_count(self) -> int:
        return self.source_end_index - self.source_start_index + 1


@dataclass(frozen=True, slots=True)
class PageSplitPlan:
    source_page_count: int
    chunks: tuple[PageSplitChunk, ...]

    def __post_init__(self) -> None:
        source_page_count = _validate_page_count(self.source_page_count)
        chunks = tuple(self.chunks)
        if len(chunks) < 2:
            raise ValueError("分割結果は2ファイル以上になる必要があります")
        filenames = [chunk.filename for chunk in chunks]
        if len(set(filenames)) != len(filenames):
            raise ValueError("分割後のファイル名が重複しています")
        expected_start = 0
        covered: list[int] = []
        for expected_index, chunk in enumerate(chunks):
            if chunk.chunk_index != expected_index:
                raise ValueError("chunk indexes must be contiguous")
            if chunk.extraction_plan.page_count != source_page_count:
                raise ValueError("chunk page count must match source_page_count")
            if chunk.source_start_index != expected_start:
                raise ValueError("分割範囲には重複または抜けがあります")
            covered.extend(chunk.extraction_plan.source_page_indexes)
            expected_start = chunk.source_end_index + 1
        if tuple(covered) != tuple(range(source_page_count)):
            raise ValueError("分割範囲は全ページをちょうど1回ずつ含む必要があります")
        object.__setattr__(self, "source_page_count", source_page_count)
        object.__setattr__(self, "chunks", chunks)

    @property
    def output_count(self) -> int:
        return len(self.chunks)


def build_page_range_split_plan(
    page_count: object,
    range_text: object,
    *,
    source_stem: str,
) -> PageSplitPlan:
    normalized_page_count = _validate_page_count(page_count)
    if not isinstance(range_text, str):
        raise TypeError("分割範囲は文字列で入力してください")
    if not range_text.isascii():
        raise ValueError("分割範囲には半角数字を使用してください")
    ranges: list[tuple[int, int]] = []
    for line_number, raw_line in enumerate(range_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = _SPLIT_RANGE_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"{line_number}行目の形式が不正です: {line}")
        start_text, end_text = match.groups()
        start_index = _page_number_to_index(int(start_text), normalized_page_count)
        end_index = (
            start_index
            if end_text is None
            else _page_number_to_index(int(end_text), normalized_page_count)
        )
        if end_index < start_index:
            raise ValueError("分割範囲は昇順で指定してください")
        ranges.append((start_index, end_index))
    if not ranges:
        raise ValueError("分割範囲を入力してください")
    return _build_split_plan(normalized_page_count, ranges, source_stem=source_stem)


def build_max_pages_split_plan(
    page_count: object,
    max_pages_per_output: object,
    *,
    source_stem: str,
) -> PageSplitPlan:
    normalized_page_count = _validate_page_count(page_count)
    max_pages = _require_int(max_pages_per_output, label="max_pages_per_output")
    if max_pages <= 0:
        raise ValueError("1ファイルあたりの最大ページ数は1以上で指定してください")
    if max_pages >= normalized_page_count:
        raise ValueError("1ファイルだけになる分割は実行できません")
    ranges = [
        (start_index, min(start_index + max_pages - 1, normalized_page_count - 1))
        for start_index in range(0, normalized_page_count, max_pages)
    ]
    return _build_split_plan(normalized_page_count, ranges, source_stem=source_stem)


def build_split_target_path(output_directory: Path, chunk: PageSplitChunk) -> Path:
    return output_directory.expanduser().resolve() / chunk.filename


def _build_split_plan(
    page_count: int,
    ranges: list[tuple[int, int]],
    *,
    source_stem: str,
) -> PageSplitPlan:
    if not source_stem:
        raise ValueError("source_stem must not be empty")
    expected_start = 0
    chunks: list[PageSplitChunk] = []
    width = max(4, len(str(page_count)))
    for chunk_index, (start_index, end_index) in enumerate(ranges):
        if start_index != expected_start:
            raise ValueError("分割範囲は昇順で、重複や抜けなく指定してください")
        display_range = f"{start_index + 1}-{end_index + 1}"
        filename = f"{source_stem}_pages_{start_index + 1:0{width}d}-{end_index + 1:0{width}d}.pdf"
        chunks.append(
            PageSplitChunk(
                chunk_index=chunk_index,
                source_start_index=start_index,
                source_end_index=end_index,
                extraction_plan=build_page_extraction_plan(
                    page_count,
                    tuple(range(start_index, end_index + 1)),
                ),
                display_range=display_range,
                filename=filename,
            )
        )
        expected_start = end_index + 1
    return PageSplitPlan(source_page_count=page_count, chunks=tuple(chunks))


def _page_number_to_index(page_number: int, page_count: int) -> int:
    if page_number <= 0:
        raise ValueError("ページ番号は1以上で指定してください")
    page_index = page_number - 1
    if page_index >= page_count:
        raise ValueError("ページ番号が文書のページ数を超えています")
    return page_index
