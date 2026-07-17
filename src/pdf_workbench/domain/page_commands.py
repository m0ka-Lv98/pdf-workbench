from __future__ import annotations

from collections.abc import Sequence
from numbers import Integral
from pathlib import Path

from pdf_workbench.domain.command_history import CommandChange, DocumentCommand
from pdf_workbench.domain.mutation import WorkingCopyMutationResult
from pdf_workbench.domain.page_insertion import PageInsertionPlan
from pdf_workbench.domain.page_reorder import PageReorderPlan
from pdf_workbench.services.pdf_page_mutation import (
    PageDeletionMutation,
    PageDeletionReceipt,
    PageDuplicationMutation,
    PageDuplicationReceipt,
    PageInsertionMutation,
    PageInsertionReceipt,
    PageReorderMutation,
    PageReorderReceipt,
    PageRotationState,
    PdfDocumentStructureSnapshot,
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
        self.last_current_page_index_after: int | None = None

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
        self.last_current_page_index_after = None
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        if self._original_states is None:
            raise RuntimeError("RotatePagesCommand has not been executed")
        self.last_mutation_result = self._mutation_service.apply_rotation_states(
            self._working_copy_path,
            self._original_states,
        )
        self.last_selected_page_indexes_after = None
        self.last_current_page_index_after = None
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        if self._rotated_states is None:
            raise RuntimeError("RotatePagesCommand has not been executed")
        self.last_mutation_result = self._mutation_service.apply_rotation_states(
            self._working_copy_path,
            self._rotated_states,
        )
        self.last_selected_page_indexes_after = None
        self.last_current_page_index_after = None
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
        self.last_current_page_index_after: int | None = None

    def execute(self) -> CommandChange:
        mutation = self._duplicate()
        self._receipt = mutation.receipt
        self.last_mutation_result = mutation.mutation_result
        self.last_selected_page_indexes_after = mutation.receipt.duplicate_page_indexes
        self.last_current_page_index_after = None
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
        self.last_current_page_index_after = None
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        receipt = self._receipt
        if receipt is None:
            raise RuntimeError("DuplicatePagesCommand has not been executed")
        self._mutation_service.validate_duplication_redo_precondition(
            self._working_copy_path,
            receipt,
        )
        mutation = self._mutation_service.duplicate_pages(
            self._working_copy_path,
            self._page_indexes,
            expected_before_snapshot=receipt.before_snapshot,
        )
        self._receipt = mutation.receipt
        self.last_mutation_result = mutation.mutation_result
        self.last_selected_page_indexes_after = mutation.receipt.duplicate_page_indexes
        self.last_current_page_index_after = None
        return CommandChange.from_command(self)

    def _duplicate(self) -> PageDuplicationMutation:
        return self._mutation_service.duplicate_pages(
            self._working_copy_path,
            self._page_indexes,
        )


class DeletePagesCommand(DocumentCommand):
    requires_document_reload = True
    mutates_working_copy = True

    def __init__(
        self,
        working_copy_path: Path,
        page_indexes: Sequence[int],
        current_page_index: int,
        mutation_service: PdfPageMutationService,
    ) -> None:
        unique_page_indexes = _normalize_page_indexes(page_indexes)
        if not unique_page_indexes:
            raise ValueError("page_indexes must not be empty")
        self.description = (
            "1ページを削除"
            if len(unique_page_indexes) == 1
            else f"{len(unique_page_indexes)}ページを削除"
        )
        self.affected_pages = frozenset()
        self._working_copy_path = working_copy_path.expanduser().resolve()
        self._page_indexes = unique_page_indexes
        self._requested_current_page_index = current_page_index
        self._mutation_service = mutation_service
        self._receipt: PageDeletionReceipt | None = None
        self._disposed = False
        self.last_mutation_result: WorkingCopyMutationResult | None = None
        self.last_selected_page_indexes_after: tuple[int, ...] | None = None
        self.last_current_page_index_after: int | None = None

    def execute(self) -> CommandChange:
        self._require_not_disposed()
        mutation = self._delete()
        self._receipt = mutation.receipt
        self.last_mutation_result = mutation.mutation_result
        self.last_selected_page_indexes_after = ()
        self.last_current_page_index_after = None
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        self._require_not_disposed()
        receipt = self._require_receipt()
        mutation_result = self._mutation_service.undo_page_deletion(
            self._working_copy_path,
            receipt,
        )
        self.last_mutation_result = mutation_result
        self.last_selected_page_indexes_after = receipt.deleted_page_indexes
        self.last_current_page_index_after = receipt.original_current_page_index
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        self._require_not_disposed()
        receipt = self._require_receipt()
        self._mutation_service.validate_deletion_redo_precondition(
            self._working_copy_path,
            receipt,
        )
        mutation_result = self._mutation_service.redo_page_deletion(
            self._working_copy_path,
            receipt,
        )
        self.last_mutation_result = mutation_result
        self.last_selected_page_indexes_after = ()
        self.last_current_page_index_after = None
        return CommandChange.from_command(self)

    def dispose(self) -> None:
        if self._disposed:
            return
        receipt = self._receipt
        if receipt is not None:
            self._mutation_service.discard_page_deletion_receipt(
                self._working_copy_path,
                receipt,
            )
            self._receipt = None
        self._disposed = True

    def _delete(self) -> PageDeletionMutation:
        return self._mutation_service.delete_pages(
            self._working_copy_path,
            self._page_indexes,
            current_page_index=self._requested_current_page_index,
        )

    def _require_receipt(self) -> PageDeletionReceipt:
        if self._receipt is None:
            raise RuntimeError("DeletePagesCommand has not been executed")
        return self._receipt

    def _require_not_disposed(self) -> None:
        if self._disposed:
            raise RuntimeError("DeletePagesCommand has been disposed")


class ReorderPagesCommand(DocumentCommand):
    requires_document_reload = True
    mutates_working_copy = True

    def __init__(
        self,
        working_copy_path: Path,
        plan: PageReorderPlan,
        mutation_service: PdfPageMutationService,
    ) -> None:
        moved_page_count = len(plan.source_page_indexes)
        self.description = (
            "1ページを移動" if moved_page_count == 1 else f"{moved_page_count}ページを移動"
        )
        self.affected_pages = frozenset(
            page_index
            for page_index, mapped_page_index in enumerate(plan.old_to_new)
            if mapped_page_index != page_index
        )
        self._working_copy_path = working_copy_path.expanduser().resolve()
        self._plan = plan
        self._mutation_service = mutation_service
        self._receipt: PageReorderReceipt | None = None
        self.last_mutation_result: WorkingCopyMutationResult | None = None
        self.last_selected_page_indexes_after: tuple[int, ...] | None = None
        self.last_current_page_index_after: int | None = None

    def execute(self) -> CommandChange:
        mutation = self._reorder()
        self._receipt = mutation.receipt
        self.last_mutation_result = mutation.mutation_result
        self.last_selected_page_indexes_after = mutation.receipt.moved_page_indexes_after
        self.last_current_page_index_after = None
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        receipt = self._require_receipt()
        mutation_result = self._mutation_service.undo_page_reordering(
            self._working_copy_path,
            receipt,
        )
        self.last_mutation_result = mutation_result
        self.last_selected_page_indexes_after = receipt.source_page_indexes
        self.last_current_page_index_after = None
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        receipt = self._require_receipt()
        self._mutation_service.validate_reordering_redo_precondition(
            self._working_copy_path,
            receipt,
        )
        mutation_result = self._mutation_service.redo_page_reordering(
            self._working_copy_path,
            receipt,
        )
        self.last_mutation_result = mutation_result
        self.last_selected_page_indexes_after = receipt.moved_page_indexes_after
        self.last_current_page_index_after = None
        return CommandChange.from_command(self)

    def _reorder(self) -> PageReorderMutation:
        return self._mutation_service.reorder_pages(
            self._working_copy_path,
            self._plan.source_page_indexes,
            self._plan.insertion_slot,
        )

    def _require_receipt(self) -> PageReorderReceipt:
        if self._receipt is None:
            raise RuntimeError("ReorderPagesCommand has not been executed")
        return self._receipt


class InsertPagesCommand(DocumentCommand):
    requires_document_reload = True
    mutates_working_copy = True

    def __init__(
        self,
        working_copy_path: Path,
        source_pdf_path: Path,
        plan: PageInsertionPlan,
        mutation_service: PdfPageMutationService,
        *,
        current_page_index_before: int,
        selected_page_indexes_before: Sequence[int],
        expected_target_snapshot: PdfDocumentStructureSnapshot | None = None,
    ) -> None:
        inserted_count = len(plan.source_page_indexes)
        self.description = (
            "1ページを挿入" if inserted_count == 1 else f"{inserted_count}ページを挿入"
        )
        self.affected_pages = frozenset(plan.inserted_page_indexes_after)
        self._working_copy_path = working_copy_path.expanduser().resolve()
        self._source_pdf_path = source_pdf_path.expanduser().resolve()
        self._plan = plan
        self._mutation_service = mutation_service
        self._current_page_index_before = current_page_index_before
        self._selected_page_indexes_before = _normalize_page_indexes(selected_page_indexes_before)
        self._expected_target_snapshot = expected_target_snapshot
        self._receipt: PageInsertionReceipt | None = None
        self._disposed = False
        self.last_mutation_result: WorkingCopyMutationResult | None = None
        self.last_selected_page_indexes_after: tuple[int, ...] | None = None
        self.last_current_page_index_after: int | None = None

    def execute(self) -> CommandChange:
        self._require_not_disposed()
        mutation = self._insert()
        self._receipt = mutation.receipt
        self.last_mutation_result = mutation.mutation_result
        self.last_selected_page_indexes_after = mutation.receipt.inserted_page_indexes
        self.last_current_page_index_after = mutation.receipt.inserted_page_indexes[0]
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        self._require_not_disposed()
        receipt = self._require_receipt()
        mutation_result = self._mutation_service.undo_page_insertion(
            self._working_copy_path,
            receipt,
        )
        self.last_mutation_result = mutation_result
        self.last_selected_page_indexes_after = self._selected_page_indexes_before
        self.last_current_page_index_after = self._current_page_index_before
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        self._require_not_disposed()
        receipt = self._require_receipt()
        mutation_result = self._mutation_service.redo_page_insertion(
            self._working_copy_path,
            receipt,
        )
        self.last_mutation_result = mutation_result
        self.last_selected_page_indexes_after = receipt.inserted_page_indexes
        self.last_current_page_index_after = receipt.inserted_page_indexes[0]
        return CommandChange.from_command(self)

    def dispose(self) -> None:
        if self._disposed:
            return
        receipt = self._receipt
        if receipt is not None:
            self._mutation_service.discard_page_insertion_receipt(
                self._working_copy_path,
                receipt,
            )
            self._receipt = None
        self._disposed = True

    def _insert(self) -> PageInsertionMutation:
        return self._mutation_service.insert_pages_from_pdf(
            self._working_copy_path,
            self._source_pdf_path,
            self._plan.source_page_indexes,
            self._plan.insertion_slot,
            expected_target_snapshot=self._expected_target_snapshot,
        )

    def _require_receipt(self) -> PageInsertionReceipt:
        if self._receipt is None:
            raise RuntimeError("InsertPagesCommand has not been executed")
        return self._receipt

    def _require_not_disposed(self) -> None:
        if self._disposed:
            raise RuntimeError("InsertPagesCommand has been disposed")
