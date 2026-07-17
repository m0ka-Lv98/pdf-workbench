from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Integral

_PAGE_TOKEN_RE = re.compile(r"^[0-9]+(?:-[0-9]+)?$")


def _require_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError(f"{label} must be an integer")
    return int(value)


def _validate_strict_ascii_digits(text: str) -> None:
    if not text.isascii():
        raise ValueError("page range must use ASCII digits")


@dataclass(frozen=True, slots=True)
class SourcePageSelection:
    source_page_count: int
    page_indexes: tuple[int, ...]

    def __post_init__(self) -> None:
        source_page_count = _require_int(self.source_page_count, label="source_page_count")
        if source_page_count <= 0:
            raise ValueError("source_page_count must be positive")
        page_indexes = tuple(
            _require_int(value, label="page_indexes")
            for value in self.page_indexes
        )
        if not page_indexes:
            raise ValueError("page_indexes must not be empty")
        if tuple(sorted(page_indexes)) != page_indexes:
            raise ValueError("page_indexes must be ascending")
        if len(set(page_indexes)) != len(page_indexes):
            raise ValueError("page_indexes must be unique")
        for page_index in page_indexes:
            if not 0 <= page_index < source_page_count:
                raise ValueError("page_indexes must stay within the page range")
        object.__setattr__(self, "source_page_count", source_page_count)
        object.__setattr__(self, "page_indexes", page_indexes)


def parse_source_page_selection(
    source_page_count: object,
    raw_value: object,
) -> SourcePageSelection:
    normalized_source_page_count = _require_int(
        source_page_count,
        label="source_page_count",
    )
    if normalized_source_page_count <= 0:
        raise ValueError("source_page_count must be positive")
    if not isinstance(raw_value, str):
        raise TypeError("page range must be a string")
    _validate_strict_ascii_digits(raw_value)
    text = raw_value.strip()
    if not text:
        raise ValueError("page range must not be empty")
    if text.lower() == "all":
        return SourcePageSelection(
            source_page_count=normalized_source_page_count,
            page_indexes=tuple(range(normalized_source_page_count)),
        )
    tokens = [token.strip() for token in text.split(",")]
    if any(token == "" for token in tokens):
        raise ValueError("page range contains an empty token")
    indexes: list[int] = []
    seen: set[int] = set()
    for token in tokens:
        if not _PAGE_TOKEN_RE.fullmatch(token):
            raise ValueError("page range token is invalid")
        if "-" not in token:
            page_number = int(token)
            page_index = _validate_page_number(page_number, normalized_source_page_count)
            if page_index in seen:
                raise ValueError("page range must not contain duplicate pages")
            seen.add(page_index)
            indexes.append(page_index)
            continue
        start_text, end_text = token.split("-", maxsplit=1)
        start_page = _validate_page_number(int(start_text), normalized_source_page_count)
        end_page = _validate_page_number(int(end_text), normalized_source_page_count)
        if end_page < start_page:
            raise ValueError("page ranges must be ascending")
        for page_index in range(start_page, end_page + 1):
            if page_index in seen:
                raise ValueError("page range must not contain duplicate pages")
            seen.add(page_index)
            indexes.append(page_index)
    if indexes != sorted(indexes):
        raise ValueError("page range must be ascending")
    return SourcePageSelection(
        source_page_count=normalized_source_page_count,
        page_indexes=tuple(indexes),
    )


def _validate_page_number(page_number: int, page_count: int) -> int:
    if page_number <= 0:
        raise ValueError("page numbers must be 1 or greater")
    page_index = page_number - 1
    if page_index >= page_count:
        raise ValueError("page number is out of range")
    return page_index


@dataclass(frozen=True, slots=True)
class PageInsertionPlan:
    target_page_count: int
    source_page_count: int
    source_page_indexes: tuple[int, ...]
    insertion_slot: int
    inserted_page_indexes_after: tuple[int, ...]
    target_old_to_new: tuple[int, ...]

    def __post_init__(self) -> None:
        target_page_count = _require_int(self.target_page_count, label="target_page_count")
        source_page_count = _require_int(self.source_page_count, label="source_page_count")
        insertion_slot = _require_int(self.insertion_slot, label="insertion_slot")
        if target_page_count <= 0:
            raise ValueError("target_page_count must be positive")
        if source_page_count <= 0:
            raise ValueError("source_page_count must be positive")
        selection = SourcePageSelection(
            source_page_count=source_page_count,
            page_indexes=self.source_page_indexes,
        )
        if not 0 <= insertion_slot <= target_page_count:
            raise ValueError("insertion_slot must stay within 0..target_page_count")
        inserted_page_indexes_after = tuple(
            _require_int(value, label="inserted_page_indexes_after")
            for value in self.inserted_page_indexes_after
        )
        expected_inserted_indexes_after = tuple(
            range(insertion_slot, insertion_slot + len(selection.page_indexes))
        )
        if inserted_page_indexes_after != expected_inserted_indexes_after:
            raise ValueError(
                "inserted_page_indexes_after does not match the requested insertion slot",
            )
        target_old_to_new = tuple(
            _require_int(value, label="target_old_to_new")
            for value in self.target_old_to_new
        )
        if len(target_old_to_new) != target_page_count:
            raise ValueError("target_old_to_new length must match target_page_count")
        expected_target_old_to_new = tuple(
            page_index if page_index < insertion_slot else page_index + len(selection.page_indexes)
            for page_index in range(target_page_count)
        )
        if target_old_to_new != expected_target_old_to_new:
            raise ValueError("target_old_to_new does not match the requested insertion")
        object.__setattr__(self, "target_page_count", target_page_count)
        object.__setattr__(self, "source_page_count", source_page_count)
        object.__setattr__(self, "source_page_indexes", selection.page_indexes)
        object.__setattr__(self, "insertion_slot", insertion_slot)
        object.__setattr__(self, "inserted_page_indexes_after", inserted_page_indexes_after)
        object.__setattr__(self, "target_old_to_new", target_old_to_new)


def build_page_insertion_plan(
    target_page_count: object,
    source_page_count: object,
    source_page_indexes: Sequence[object],
    insertion_slot: object,
) -> PageInsertionPlan:
    normalized_target_page_count = _require_int(target_page_count, label="target_page_count")
    normalized_source_page_count = _require_int(source_page_count, label="source_page_count")
    selection = SourcePageSelection(
        source_page_count=normalized_source_page_count,
        page_indexes=tuple(
            _require_int(page_index, label="source_page_indexes")
            for page_index in source_page_indexes
        ),
    )
    normalized_insertion_slot = _require_int(insertion_slot, label="insertion_slot")
    return PageInsertionPlan(
        target_page_count=normalized_target_page_count,
        source_page_count=normalized_source_page_count,
        source_page_indexes=selection.page_indexes,
        insertion_slot=normalized_insertion_slot,
        inserted_page_indexes_after=tuple(
            range(
                normalized_insertion_slot,
                normalized_insertion_slot + len(selection.page_indexes),
            )
        ),
        target_old_to_new=tuple(
            page_index
            if page_index < normalized_insertion_slot
            else page_index + len(selection.page_indexes)
            for page_index in range(normalized_target_page_count)
        ),
    )
