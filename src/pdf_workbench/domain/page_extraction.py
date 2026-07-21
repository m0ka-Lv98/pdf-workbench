from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Integral

_PAGE_TOKEN_RE = re.compile(r"^([0-9]+)(?:\s*-\s*([0-9]+))?$")


def _require_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError(f"{label} must be an integer")
    return int(value)


def _validate_page_count(value: object) -> int:
    page_count = _require_int(value, label="page_count")
    if page_count <= 0:
        raise ValueError("page_count must be positive")
    return page_count


def _validate_page_indexes(page_count: int, values: Sequence[object]) -> tuple[int, ...]:
    indexes = tuple(_require_int(value, label="source_page_indexes") for value in values)
    if not indexes:
        raise ValueError("source_page_indexes must not be empty")
    if tuple(sorted(indexes)) != indexes:
        raise ValueError("source_page_indexes must be sorted")
    if len(set(indexes)) != len(indexes):
        raise ValueError("source_page_indexes must be unique")
    for page_index in indexes:
        if not 0 <= page_index < page_count:
            raise ValueError("source_page_indexes must stay within the page range")
    return indexes


@dataclass(frozen=True, slots=True)
class PageExtractionPlan:
    page_count: int
    source_page_indexes: tuple[int, ...]
    source_to_output: tuple[tuple[int, int], ...]

    def __post_init__(self) -> None:
        page_count = _validate_page_count(self.page_count)
        source_page_indexes = _validate_page_indexes(page_count, self.source_page_indexes)
        expected_mapping = tuple(
            (source_page_index, output_page_index)
            for output_page_index, source_page_index in enumerate(source_page_indexes)
        )
        normalized_mapping = tuple(
            (
                _require_int(source_page_index, label="source_to_output.source"),
                _require_int(output_page_index, label="source_to_output.output"),
            )
            for source_page_index, output_page_index in self.source_to_output
        )
        if normalized_mapping != expected_mapping:
            raise ValueError("source_to_output must match source_page_indexes")
        object.__setattr__(self, "page_count", page_count)
        object.__setattr__(self, "source_page_indexes", source_page_indexes)
        object.__setattr__(self, "source_to_output", expected_mapping)

    @property
    def output_page_count(self) -> int:
        return len(self.source_page_indexes)


def build_page_extraction_plan(
    page_count: object,
    source_page_indexes: Sequence[object],
) -> PageExtractionPlan:
    normalized_page_count = _validate_page_count(page_count)
    normalized_indexes = _validate_page_indexes(normalized_page_count, source_page_indexes)
    return PageExtractionPlan(
        page_count=normalized_page_count,
        source_page_indexes=normalized_indexes,
        source_to_output=tuple(
            (source_page_index, output_page_index)
            for output_page_index, source_page_index in enumerate(normalized_indexes)
        ),
    )


def build_selected_page_extraction_plan(
    page_count: object,
    selected_page_indexes: Sequence[object],
) -> PageExtractionPlan:
    normalized_page_count = _validate_page_count(page_count)
    selected = sorted(
        set(_require_int(value, label="selected_page_indexes") for value in selected_page_indexes)
    )
    return build_page_extraction_plan(normalized_page_count, tuple(selected))


def parse_page_range_extraction_plan(
    page_count: object,
    range_text: object,
) -> PageExtractionPlan:
    normalized_page_count = _validate_page_count(page_count)
    if not isinstance(range_text, str):
        raise TypeError("ページ範囲は文字列で入力してください")
    if not range_text.isascii():
        raise ValueError("ページ範囲には半角数字を使用してください")
    text = range_text.strip()
    if not text:
        raise ValueError("ページ範囲を入力してください")
    indexes: set[int] = set()
    for raw_token in text.split(","):
        token = raw_token.strip()
        if not token:
            raise ValueError("空のページ範囲があります")
        match = _PAGE_TOKEN_RE.fullmatch(token)
        if match is None:
            raise ValueError(f"ページ範囲の形式が不正です: {token}")
        start_text, end_text = match.groups()
        if end_text is None:
            indexes.add(_page_number_to_index(int(start_text), normalized_page_count))
            continue
        start_index = _page_number_to_index(int(start_text), normalized_page_count)
        end_index = _page_number_to_index(int(end_text), normalized_page_count)
        if end_index < start_index:
            raise ValueError("ページ範囲は昇順で指定してください")
        indexes.update(range(start_index, end_index + 1))
    return build_page_extraction_plan(normalized_page_count, tuple(sorted(indexes)))


def _page_number_to_index(page_number: int, page_count: int) -> int:
    if page_number <= 0:
        raise ValueError("ページ番号は1以上で指定してください")
    page_index = page_number - 1
    if page_index >= page_count:
        raise ValueError("ページ番号が文書のページ数を超えています")
    return page_index
