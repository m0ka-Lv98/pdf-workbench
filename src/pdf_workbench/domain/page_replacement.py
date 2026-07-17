from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pdf_workbench.domain.page_insertion import _require_int


def _normalize_page_indexes(
    values: Sequence[object],
    *,
    label: str,
    page_count: int,
) -> tuple[int, ...]:
    normalized = tuple(_require_int(value, label=label) for value in values)
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    if tuple(sorted(normalized)) != normalized:
        raise ValueError(f"{label} must be ascending")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must be unique")
    for page_index in normalized:
        if not 0 <= page_index < page_count:
            raise ValueError(f"{label} must stay within the page range")
    return normalized


@dataclass(frozen=True, slots=True)
class PageReplacementPlan:
    target_page_count: int
    source_page_count: int
    target_page_indexes: tuple[int, ...]
    source_page_indexes: tuple[int, ...]
    replacement_pairs: tuple[tuple[int, int], ...]
    replaced_page_indexes_after: tuple[int, ...]
    execute_cache_old_to_new: tuple[int | None, ...]
    execute_current_page_old_to_new: tuple[int, ...]

    def __post_init__(self) -> None:
        target_page_count = _require_int(self.target_page_count, label="target_page_count")
        source_page_count = _require_int(self.source_page_count, label="source_page_count")
        if target_page_count <= 0:
            raise ValueError("target_page_count must be positive")
        if source_page_count <= 0:
            raise ValueError("source_page_count must be positive")
        target_page_indexes = _normalize_page_indexes(
            self.target_page_indexes,
            label="target_page_indexes",
            page_count=target_page_count,
        )
        source_page_indexes = _normalize_page_indexes(
            self.source_page_indexes,
            label="source_page_indexes",
            page_count=source_page_count,
        )
        if len(target_page_indexes) != len(source_page_indexes):
            raise ValueError("target and source page selections must be the same length")
        replacement_pairs = tuple(zip(target_page_indexes, source_page_indexes, strict=True))
        if tuple(tuple(pair) for pair in self.replacement_pairs) != replacement_pairs:
            raise ValueError("replacement_pairs does not match the replacement selections")
        replaced_page_indexes_after = tuple(
            _require_int(value, label="replaced_page_indexes_after")
            for value in self.replaced_page_indexes_after
        )
        if replaced_page_indexes_after != target_page_indexes:
            raise ValueError("replaced_page_indexes_after does not match target_page_indexes")
        expected_cache_mapping = tuple(
            None if page_index in set(target_page_indexes) else page_index
            for page_index in range(target_page_count)
        )
        cache_mapping = tuple(
            None if value is None else _require_int(value, label="execute_cache_old_to_new")
            for value in self.execute_cache_old_to_new
        )
        if cache_mapping != expected_cache_mapping:
            raise ValueError("execute_cache_old_to_new does not match the replacement selection")
        expected_current_mapping = tuple(range(target_page_count))
        current_mapping = tuple(
            _require_int(value, label="execute_current_page_old_to_new")
            for value in self.execute_current_page_old_to_new
        )
        if current_mapping != expected_current_mapping:
            raise ValueError(
                "execute_current_page_old_to_new does not preserve current page indexes"
            )
        object.__setattr__(self, "target_page_count", target_page_count)
        object.__setattr__(self, "source_page_count", source_page_count)
        object.__setattr__(self, "target_page_indexes", target_page_indexes)
        object.__setattr__(self, "source_page_indexes", source_page_indexes)
        object.__setattr__(self, "replacement_pairs", replacement_pairs)
        object.__setattr__(self, "replaced_page_indexes_after", target_page_indexes)
        object.__setattr__(self, "execute_cache_old_to_new", expected_cache_mapping)
        object.__setattr__(self, "execute_current_page_old_to_new", expected_current_mapping)


def build_page_replacement_plan(
    target_page_count: object,
    source_page_count: object,
    target_page_indexes: Sequence[object],
    source_page_indexes: Sequence[object],
) -> PageReplacementPlan:
    normalized_target_page_count = _require_int(target_page_count, label="target_page_count")
    normalized_source_page_count = _require_int(source_page_count, label="source_page_count")
    normalized_target_page_indexes = tuple(
        _require_int(page_index, label="target_page_indexes") for page_index in target_page_indexes
    )
    normalized_source_page_indexes = tuple(
        _require_int(page_index, label="source_page_indexes") for page_index in source_page_indexes
    )
    return PageReplacementPlan(
        target_page_count=normalized_target_page_count,
        source_page_count=normalized_source_page_count,
        target_page_indexes=normalized_target_page_indexes,
        source_page_indexes=normalized_source_page_indexes,
        replacement_pairs=tuple(
            zip(normalized_target_page_indexes, normalized_source_page_indexes, strict=False)
        ),
        replaced_page_indexes_after=normalized_target_page_indexes,
        execute_cache_old_to_new=tuple(
            None if page_index in set(normalized_target_page_indexes) else page_index
            for page_index in range(normalized_target_page_count)
        ),
        execute_current_page_old_to_new=tuple(range(normalized_target_page_count)),
    )
