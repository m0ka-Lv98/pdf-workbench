from __future__ import annotations

from collections.abc import Sequence
from numbers import Integral
from pathlib import Path

from pdf_workbench.domain.command_history import CommandChange, DocumentCommand
from pdf_workbench.domain.mutation import WorkingCopyMutationResult
from pdf_workbench.services.pdf_page_mutation import (
    PageRotationState,
    PdfPageMutationService,
)


class RotatePagesCommand(DocumentCommand):
    requires_document_reload = True
    mutates_working_copy = True

    def __init__(
        self,
        working_copy_path: Path,
        page_indexes: Sequence[int],
        mutation_service: PdfPageMutationService,
        *,
        degrees: int = 90,
    ) -> None:
        unique_page_indexes = self._normalize_page_indexes(page_indexes)
        if not unique_page_indexes:
            raise ValueError("page_indexes must not be empty")
        if degrees != 90:
            raise ValueError("only clockwise 90-degree rotation is supported")
        self.description = (
            "1ページを時計回りに回転"
            if len(unique_page_indexes) == 1
            else f"{len(unique_page_indexes)}ページを時計回りに回転"
        )
        self.affected_pages = frozenset(unique_page_indexes)
        self._working_copy_path = working_copy_path.expanduser().resolve()
        self._page_indexes = unique_page_indexes
        self._mutation_service = mutation_service
        self._degrees = degrees
        self._original_states: tuple[PageRotationState, ...] | None = None
        self._rotated_states: tuple[PageRotationState, ...] | None = None
        self.last_mutation_result: WorkingCopyMutationResult | None = None

    def execute(self) -> CommandChange:
        self._original_states = self._mutation_service.read_rotation_states(
            self._working_copy_path,
            self._page_indexes,
        )
        self._rotated_states = tuple(
            PageRotationState(
                page_index=state.page_index,
                direct_rotate_present=True,
                direct_rotate_value=(state.effective_rotation + self._degrees) % 360,
                effective_rotation=(state.effective_rotation + self._degrees) % 360,
            )
            for state in self._original_states
        )
        self.last_mutation_result = self._mutation_service.apply_rotation_states(
            self._working_copy_path,
            self._rotated_states,
        )
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        if self._original_states is None:
            raise RuntimeError("RotatePagesCommand has not been executed")
        self.last_mutation_result = self._mutation_service.apply_rotation_states(
            self._working_copy_path,
            self._original_states,
        )
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        if self._rotated_states is None:
            raise RuntimeError("RotatePagesCommand has not been executed")
        self.last_mutation_result = self._mutation_service.apply_rotation_states(
            self._working_copy_path,
            self._rotated_states,
        )
        return CommandChange.from_command(self)

    @staticmethod
    def _normalize_page_indexes(page_indexes: Sequence[int]) -> tuple[int, ...]:
        normalized: list[int] = []
        for page_index in page_indexes:
            if isinstance(page_index, bool) or not isinstance(page_index, Integral):
                raise TypeError("page indexes must be integers")
            typed_page_index = int(page_index)
            if typed_page_index < 0:
                raise ValueError("page indexes must be non-negative")
            normalized.append(typed_page_index)
        return tuple(sorted(set(normalized)))
