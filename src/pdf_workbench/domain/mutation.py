from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pdf_workbench.services.pdf_renderer import DocumentRevision


@dataclass(frozen=True, slots=True)
class PageIndexTransition:
    old_page_count: int
    new_page_count: int
    cache_old_to_new: tuple[int | None, ...]
    current_page_old_to_new: tuple[int | None, ...]

    def __post_init__(self) -> None:
        if self.old_page_count < 0 or self.new_page_count < 0:
            raise ValueError("page counts must be non-negative")
        if len(self.cache_old_to_new) != self.old_page_count:
            raise ValueError("cache_old_to_new length must match old_page_count")
        if len(self.current_page_old_to_new) != self.old_page_count:
            raise ValueError("current_page_old_to_new length must match old_page_count")
        self._validate_mapping(self.cache_old_to_new, unique=True, label="cache_old_to_new")
        self._validate_mapping(
            self.current_page_old_to_new,
            unique=False,
            label="current_page_old_to_new",
        )

    def _validate_mapping(
        self,
        values: tuple[int | None, ...],
        *,
        unique: bool,
        label: str,
    ) -> None:
        seen: set[int] = set()
        for value in values:
            if value is None:
                continue
            if not 0 <= value < self.new_page_count:
                raise ValueError(f"{label} values must be within the new page range")
            if unique:
                if value in seen:
                    raise ValueError(f"{label} values must be unique")
                seen.add(value)


@dataclass(frozen=True, slots=True)
class WorkingCopyMutationResult:
    old_revision: DocumentRevision
    new_revision: DocumentRevision
    page_count: int
    affected_pages: frozenset[int]
    page_index_transition: PageIndexTransition | None = None
