from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pdf_workbench.services.pdf_renderer import DocumentRevision


@dataclass(frozen=True, slots=True)
class WorkingCopyMutationResult:
    old_revision: DocumentRevision
    new_revision: DocumentRevision
    page_count: int
    affected_pages: frozenset[int]
