from __future__ import annotations

from collections.abc import Sequence
from numbers import Integral
from pathlib import Path

from pdf_workbench.domain.command_history import CommandChange, DocumentCommand
from pdf_workbench.domain.mutation import WorkingCopyMutationResult
from pdf_workbench.services.pdf_page_mutation import (
    PageDuplicationMutation,
    PageDuplicationReceipt,
    PageRotationState,
    PdfPageMutationService,
)


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
        unique_page_indexes = _normalize_page_indexes(page_indexes)
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
        self.last_selected_page_indexes_after: tuple[int, ...] | None = None

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
        self.last_selected_page_indexes_after = None
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        if self._original_states is None:
            raise RuntimeError("RotatePagesCommand has not been executed")
        self.last_mutation_result = self._mutation_service.apply_rotation_states(
            self._working_copy_path,
            self._original_states,
        )
        self.last_selected_page_indexes_after = None
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        if self._rotated_states is None:
            raise RuntimeError("RotatePagesCommand has not been executed")
        self.last_mutation_result = self._mutation_service.apply_rotation_states(
            self._working_copy_path,
            self._rotated_states,
        )
        self.last_selected_page_indexes_after = None
        return CommandChange.from_command(self)


class DuplicatePagesCommand(DocumentCommand):
    requires_document_reload = True
    mutates_working_copy = True

    def __init__(
        self,
        working_copy_path: Path,
        page_indexes: Sequence[int],
        mutation_service: PdfPageMutationService,
    ) -> None:
        unique_page_indexes = _normalize_page_indexes(page_indexes)
        if not unique_page_indexes:
            raise ValueError("page_indexes must not be empty")
        self.description = (
            "1ページを複製"
            if len(unique_page_indexes) == 1
            else f"{len(unique_page_indexes)}ページを複製"
        )
        self.affected_pages = frozenset(unique_page_indexes)
        self._working_copy_path = working_copy_path.expanduser().resolve()
        self._page_indexes = unique_page_indexes
        self._mutation_service = mutation_service
        self._receipt: PageDuplicationReceipt | None = None
        self.last_mutation_result: WorkingCopyMutationResult | None = None
        self.last_selected_page_indexes_after: tuple[int, ...] | None = None

    def execute(self) -> CommandChange:
        mutation = self._duplicate()
        self._receipt = mutation.receipt
        self.last_mutation_result = mutation.mutation_result
        self.last_selected_page_indexes_after = mutation.receipt.duplicate_page_indexes
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        receipt = self._receipt
        if receipt is None:
            raise RuntimeError("DuplicatePagesCommand has not been executed")
        mutation_result = self._mutation_service.undo_page_duplication(
            self._working_copy_path,
            receipt,
        )
        self.last_mutation_result = mutation_result
        self.last_selected_page_indexes_after = receipt.source_page_indexes
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        receipt = self._receipt
        if receipt is None:
            raise RuntimeError("DuplicatePagesCommand has not been executed")
        mutation = self._duplicate()
        if mutation.receipt.duplicate_page_indexes != receipt.duplicate_page_indexes:
            raise RuntimeError("duplicate page indexes changed during redo")
        if mutation.receipt.original_page_indexes_after != receipt.original_page_indexes_after:
            raise RuntimeError("original page indexes changed during redo")
        self._receipt = mutation.receipt
        self.last_mutation_result = mutation.mutation_result
        self.last_selected_page_indexes_after = mutation.receipt.duplicate_page_indexes
        return CommandChange.from_command(self)

    def _duplicate(self) -> PageDuplicationMutation:
        return self._mutation_service.duplicate_pages(
            self._working_copy_path,
            self._page_indexes,
        )
