from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from pdf_workbench.domain.command_history import CommandChange, DocumentCommand
from pdf_workbench.services.pdf_page_mutation import (
    PageMutationResult,
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
        prepare_mutation: Callable[[], None] | None = None,
    ) -> None:
        unique_page_indexes = tuple(sorted(set(int(page_index) for page_index in page_indexes)))
        if not unique_page_indexes:
            raise ValueError("page_indexes must not be empty")
        if any(page_index < 0 for page_index in unique_page_indexes):
            raise ValueError("page indexes must be non-negative")
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
        self._prepare_mutation = prepare_mutation if prepare_mutation is not None else lambda: None
        self._original_states: tuple[PageRotationState, ...] | None = None
        self._rotated_states: tuple[PageRotationState, ...] | None = None
        self.last_mutation_result: PageMutationResult | None = None

    def execute(self) -> CommandChange:
        self._prepare_mutation()
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
        self._prepare_mutation()
        self.last_mutation_result = self._mutation_service.apply_rotation_states(
            self._working_copy_path,
            self._original_states,
        )
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        if self._rotated_states is None:
            raise RuntimeError("RotatePagesCommand has not been executed")
        self._prepare_mutation()
        self.last_mutation_result = self._mutation_service.apply_rotation_states(
            self._working_copy_path,
            self._rotated_states,
        )
        return CommandChange.from_command(self)
