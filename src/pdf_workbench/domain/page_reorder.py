from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Integral


class PageReorderPlanError(ValueError):
    """Raised when page reordering inputs cannot produce a valid plan."""


class PageReorderNoOpError(PageReorderPlanError):
    """Raised when page reordering would keep the existing page order."""


def _require_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError(f"{label} must be an integer")
    return int(value)


def _normalize_source_page_indexes(
    page_indexes: Sequence[object],
    *,
    page_count: int,
) -> tuple[int, ...]:
    normalized: list[int] = []
    for page_index in page_indexes:
        typed_page_index = _require_int(page_index, label="source_page_indexes")
        if typed_page_index < 0:
            raise ValueError("source_page_indexes must be non-negative")
        if typed_page_index >= page_count:
            raise ValueError("source_page_indexes must stay within the page range")
        normalized.append(typed_page_index)
    unique_sorted = tuple(sorted(set(normalized)))
    if not unique_sorted:
        raise ValueError("source_page_indexes must not be empty")
    return unique_sorted


def _inverse_permutation(order: tuple[int, ...]) -> tuple[int, ...]:
    inverse = [0] * len(order)
    for new_index, old_index in enumerate(order):
        inverse[old_index] = new_index
    return tuple(inverse)


@dataclass(frozen=True, slots=True)
class PageReorderPlan:
    page_count: int
    source_page_indexes: tuple[int, ...]
    insertion_slot: int
    target_order: tuple[int, ...]
    old_to_new: tuple[int, ...]
    new_to_old: tuple[int, ...]
    moved_page_indexes_after: tuple[int, ...]

    def __post_init__(self) -> None:
        page_count = _require_int(self.page_count, label="page_count")
        if page_count < 0:
            raise ValueError("page_count must be non-negative")
        source_page_indexes = _normalize_source_page_indexes(
            self.source_page_indexes,
            page_count=page_count,
        )
        insertion_slot = _require_int(self.insertion_slot, label="insertion_slot")
        if not 0 <= insertion_slot <= page_count:
            raise ValueError("insertion_slot must stay within 0..page_count")
        if len(self.target_order) != page_count:
            raise ValueError("target_order length must match page_count")
        if len(self.old_to_new) != page_count:
            raise ValueError("old_to_new length must match page_count")
        if len(self.new_to_old) != page_count:
            raise ValueError("new_to_old length must match page_count")

        expected_identity = tuple(range(page_count))
        expected_set = set(expected_identity)
        if set(self.target_order) != expected_set or len(set(self.target_order)) != page_count:
            raise ValueError("target_order must be a valid permutation")
        if set(self.new_to_old) != expected_set or len(set(self.new_to_old)) != page_count:
            raise ValueError("new_to_old must be a valid permutation")
        if set(self.old_to_new) != expected_set or len(set(self.old_to_new)) != page_count:
            raise ValueError("old_to_new must be a valid permutation")
        if tuple(self.target_order) != tuple(self.new_to_old):
            raise ValueError("target_order and new_to_old must match")

        selected_set = set(source_page_indexes)
        survivors = tuple(
            page_index for page_index in range(page_count) if page_index not in selected_set
        )
        adjusted_slot = insertion_slot - sum(
            1 for source_index in source_page_indexes if source_index < insertion_slot
        )
        expected_target_order = (
            survivors[:adjusted_slot] + source_page_indexes + survivors[adjusted_slot:]
        )
        if tuple(self.target_order) != expected_target_order:
            raise ValueError("target_order does not match the requested page move")
        expected_old_to_new = _inverse_permutation(expected_target_order)
        if tuple(self.old_to_new) != expected_old_to_new:
            raise ValueError("old_to_new does not match target_order")

        moved_page_indexes_after = tuple(
            _require_int(value, label="moved_page_indexes_after")
            for value in self.moved_page_indexes_after
        )
        if moved_page_indexes_after != tuple(
            range(adjusted_slot, adjusted_slot + len(source_page_indexes))
        ):
            raise ValueError("moved_page_indexes_after does not match the expected moved block")
        if tuple(self.target_order) == expected_identity:
            raise PageReorderNoOpError("page reorder is a no-op")


def build_page_reorder_plan(
    page_count: object,
    source_page_indexes: Sequence[object],
    insertion_slot: object,
) -> PageReorderPlan:
    normalized_page_count = _require_int(page_count, label="page_count")
    if normalized_page_count < 0:
        raise ValueError("page_count must be non-negative")
    normalized_source_page_indexes = _normalize_source_page_indexes(
        source_page_indexes,
        page_count=normalized_page_count,
    )
    normalized_insertion_slot = _require_int(insertion_slot, label="insertion_slot")
    if not 0 <= normalized_insertion_slot <= normalized_page_count:
        raise ValueError("insertion_slot must stay within 0..page_count")

    selected_set = set(normalized_source_page_indexes)
    survivors = tuple(
        page_index for page_index in range(normalized_page_count) if page_index not in selected_set
    )
    adjusted_slot = normalized_insertion_slot - sum(
        1
        for source_index in normalized_source_page_indexes
        if source_index < normalized_insertion_slot
    )
    target_order = (
        survivors[:adjusted_slot] + normalized_source_page_indexes + survivors[adjusted_slot:]
    )
    old_to_new = _inverse_permutation(target_order)
    moved_page_indexes_after = tuple(
        range(adjusted_slot, adjusted_slot + len(normalized_source_page_indexes))
    )
    return PageReorderPlan(
        page_count=normalized_page_count,
        source_page_indexes=normalized_source_page_indexes,
        insertion_slot=normalized_insertion_slot,
        target_order=target_order,
        old_to_new=old_to_new,
        new_to_old=target_order,
        moved_page_indexes_after=moved_page_indexes_after,
    )
