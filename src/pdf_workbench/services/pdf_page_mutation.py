from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from numbers import Integral
from pathlib import Path
from typing import Any, cast

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, FloatObject, NameObject, NumberObject

from pdf_workbench.domain.document_session import FileFingerprint
from pdf_workbench.domain.mutation import PageIndexTransition, WorkingCopyMutationResult
from pdf_workbench.domain.page_crop import (
    _EPSILON,
    _MINIMUM_EXTENT,
    PageCropPlan,
    PageCropTarget,
)
from pdf_workbench.domain.page_crop import (
    PageCropState as CropPageState,
)
from pdf_workbench.domain.page_insertion import PageInsertionPlan, build_page_insertion_plan
from pdf_workbench.domain.page_reorder import PageReorderPlan, build_page_reorder_plan
from pdf_workbench.domain.page_replacement import (
    PageReplacementPlan,
    build_page_replacement_plan,
)
from pdf_workbench.services.page_coordinates import PdfRect
from pdf_workbench.services.pdf_document_validator import (
    PdfDocumentValidationError,
    PdfDocumentValidator,
)
from pdf_workbench.services.pdf_renderer import DocumentRevision

logger = logging.getLogger(__name__)

_VOLATILE_STREAM_KEYS = frozenset({"/Length", "/Filter", "/DecodeParms", "/DL"})
SUPPORTED_IMPORTED_ANNOTATION_SUBTYPES = frozenset(
    {
        "/Text",
        "/Square",
        "/Circle",
        "/Highlight",
        "/Underline",
        "/StrikeOut",
        "/Stamp",
        "/Ink",
    }
)
_PROHIBITED_IMPORTED_ANNOTATION_KEYS = (
    "/A",
    "/AA",
    "/Dest",
    "/FS",
    "/RichMediaContent",
    "/RichMediaSettings",
    "/3DD",
    "/3DV",
    "/Sound",
    "/Movie",
)
_REPLACEMENT_ALLOWED_PAGE_KEYS = frozenset(
    {
        "/Type",
        "/Parent",
        "/Contents",
        "/Resources",
        "/MediaBox",
        "/CropBox",
        "/TrimBox",
        "/BleedBox",
        "/ArtBox",
        "/Rotate",
        "/Annots",
    }
)
_REPLACEMENT_MATERIALIZED_PAGE_KEYS = (
    "/Contents",
    "/Resources",
    "/MediaBox",
    "/CropBox",
    "/TrimBox",
    "/BleedBox",
    "/ArtBox",
    "/Rotate",
    "/Annots",
)
_PAGE_ENTRY_FINGERPRINT_EXCLUDED_KEYS = frozenset(
    _REPLACEMENT_ALLOWED_PAGE_KEYS | {"/Parent", "/Type"}
)
_NORMALIZE_OBJECT_UNHANDLED = object()


def _require_strict_int(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{label} must contain only integers")
    return int(value)


def _validate_sorted_unique_page_indexes(
    values: tuple[int, ...],
    *,
    label: str,
    page_count: int,
) -> tuple[int, ...]:
    normalized = tuple(_require_strict_int(value, label=label) for value in values)
    if tuple(sorted(normalized)) != normalized:
        raise ValueError(f"{label} must be sorted")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} must be unique")
    for page_index in normalized:
        if page_index < 0 or page_index >= page_count:
            raise ValueError(f"{label} must stay within the page range")
    return normalized


def _all_original_indexes_after(
    original_page_count: int,
    source_page_indexes: tuple[int, ...],
) -> tuple[int, ...]:
    return tuple(
        page_index + sum(1 for source_index in source_page_indexes if source_index < page_index)
        for page_index in range(original_page_count)
    )


class PdfPageMutationError(RuntimeError):
    """Raised when a working-copy PDF page mutation fails."""


class PdfPageRotationValidationError(PdfPageMutationError):
    """Raised when a page rotation value is invalid."""


@dataclass(frozen=True, slots=True)
class PageRotationState:
    page_index: int
    direct_rotate_present: bool
    direct_rotate_value: int | None
    effective_rotation: int


@dataclass(frozen=True, slots=True)
class PageCropReceipt:
    working_copy_path: Path
    before_snapshot: PdfDocumentStructureSnapshot
    after_snapshot: PdfDocumentStructureSnapshot
    original_crop_states: tuple[CropPageState, ...]
    target_crop_states: tuple[PageCropTarget, ...]
    changed_page_indexes: tuple[int, ...]
    execute_transition: PageIndexTransition
    undo_transition: PageIndexTransition

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "working_copy_path",
            _canonical_working_copy_pdf_path(self.working_copy_path),
        )
        _validate_structure_snapshot_shape(self.before_snapshot, label="before_snapshot")
        _validate_structure_snapshot_shape(self.after_snapshot, label="after_snapshot")
        if self.before_snapshot.page_count != self.after_snapshot.page_count:
            raise ValueError("before and after snapshots must keep the same page count")
        normalized_indexes = _validate_sorted_unique_page_indexes(
            self.changed_page_indexes,
            label="changed_page_indexes",
            page_count=self.before_snapshot.page_count,
        )
        if not normalized_indexes:
            raise ValueError("changed_page_indexes must not be empty")
        if len(self.original_crop_states) != len(normalized_indexes):
            raise ValueError("original_crop_states length must match changed_page_indexes")
        if len(self.target_crop_states) != len(normalized_indexes):
            raise ValueError("target_crop_states length must match changed_page_indexes")
        for expected_page_index, state in zip(
            normalized_indexes,
            self.original_crop_states,
            strict=True,
        ):
            CropPageState(
                page_index=state.page_index,
                direct_crop_box_present=state.direct_crop_box_present,
                direct_crop_box_value=state.direct_crop_box_value,
                effective_crop_box=state.effective_crop_box,
                effective_media_box=state.effective_media_box,
                effective_rotation=state.effective_rotation,
                crop_box_inherited=state.crop_box_inherited,
                crop_box_falls_back_to_media_box=state.crop_box_falls_back_to_media_box,
            )
            if state.page_index != expected_page_index:
                raise ValueError("original crop state page index must match changed_page_indexes")
        for expected_page_index, target in zip(
            normalized_indexes,
            self.target_crop_states,
            strict=True,
        ):
            PageCropTarget(
                page_index=target.page_index,
                crop_box=target.crop_box,
            )
            if target.page_index != expected_page_index:
                raise ValueError("target crop state page index must match changed_page_indexes")
        if self.execute_transition.old_page_count != self.before_snapshot.page_count:
            raise ValueError("execute_transition old_page_count is invalid")
        if self.execute_transition.new_page_count != self.before_snapshot.page_count:
            raise ValueError("execute_transition new_page_count is invalid")
        if self.undo_transition.old_page_count != self.before_snapshot.page_count:
            raise ValueError("undo_transition old_page_count is invalid")
        if self.undo_transition.new_page_count != self.before_snapshot.page_count:
            raise ValueError("undo_transition new_page_count is invalid")
        expected_cache_mapping = tuple(
            None if page_index in set(normalized_indexes) else page_index
            for page_index in range(self.before_snapshot.page_count)
        )
        expected_identity_mapping = tuple(range(self.before_snapshot.page_count))
        if self.execute_transition.cache_old_to_new != expected_cache_mapping:
            raise ValueError("execute_transition cache_old_to_new is invalid")
        if self.execute_transition.current_page_old_to_new != expected_identity_mapping:
            raise ValueError("execute_transition current_page_old_to_new is invalid")
        if self.undo_transition.cache_old_to_new != expected_cache_mapping:
            raise ValueError("undo_transition cache_old_to_new is invalid")
        if self.undo_transition.current_page_old_to_new != expected_identity_mapping:
            raise ValueError("undo_transition current_page_old_to_new is invalid")
        _validate_crop_snapshot_transition(
            self.before_snapshot,
            self.after_snapshot,
            normalized_indexes,
            self.original_crop_states,
            self.target_crop_states,
        )


@dataclass(frozen=True, slots=True)
class PageCropMutation:
    mutation_result: WorkingCopyMutationResult
    receipt: PageCropReceipt


@dataclass(frozen=True, slots=True)
class PageBoxState:
    media_box_direct_present: bool
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    crop_box_direct_present: bool
    crop_box_direct_value: tuple[float, float, float, float] | None
    crop_box_inherited: bool
    crop_box_falls_back_to_media_box: bool
    trim_box: tuple[float, float, float, float] | None
    bleed_box: tuple[float, float, float, float] | None
    art_box: tuple[float, float, float, float] | None

    def __post_init__(self) -> None:
        if self.crop_box_direct_present:
            if self.crop_box_direct_value is None:
                raise ValueError(
                    "crop_box_direct_value is required when crop_box_direct_present is true"
                )
            object.__setattr__(
                self,
                "crop_box_direct_value",
                _require_snapshot_raw_box(
                    self.crop_box_direct_value,
                    label="crop_box_direct_value",
                ),
            )
        elif self.crop_box_direct_value is not None:
            raise ValueError(
                "crop_box_direct_value must be None when crop_box_direct_present is false"
            )


class AnnotationParentState(StrEnum):
    ABSENT = "absent"
    POINTS_TO_OWN_PAGE = "points_to_own_page"
    POINTS_TO_OTHER_PAGE = "points_to_other_page"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class PdfAnnotationStructureSnapshot:
    subtype: str
    rect: tuple[float, float, float, float]
    has_appearance: bool
    appearance_fingerprint: str | None
    parent_state: AnnotationParentState
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PdfPageStructureSnapshot:
    content_fingerprint: str
    boxes: PageBoxState
    direct_resources_present: bool
    resources_fingerprint: str
    direct_rotate_present: bool
    direct_rotate_value: int | None
    effective_rotation: int
    annotations: tuple[PdfAnnotationStructureSnapshot, ...]
    direct_page_keys: tuple[str, ...]
    extra_page_entries_fingerprint: str


@dataclass(frozen=True, slots=True)
class SourcePdfRevision:
    resolved_path: Path
    fingerprint: FileFingerprint
    sha256: str
    page_count: int

    def __post_init__(self) -> None:
        if not self.resolved_path.is_absolute():
            raise ValueError("resolved_path must be absolute")
        if self.resolved_path.suffix.lower() != ".pdf":
            raise ValueError("resolved_path must refer to a PDF")
        if isinstance(self.page_count, bool) or not isinstance(self.page_count, Integral):
            raise ValueError("page_count must be an integer")
        if self.page_count <= 0:
            raise ValueError("page_count must be positive")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("sha256 must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class PdfOutlineItemSnapshot:
    title: str
    destination_page_index: int | None
    action_type: str | None
    children: tuple[PdfOutlineItemSnapshot, ...]


@dataclass(frozen=True, slots=True)
class PdfNamedDestinationSnapshot:
    name: str
    destination_page_index: int


@dataclass(frozen=True, slots=True)
class PdfDocumentStructureSnapshot:
    page_count: int
    pages: tuple[PdfPageStructureSnapshot, ...]
    metadata_fingerprint: str
    outlines: tuple[PdfOutlineItemSnapshot, ...]
    named_destinations: tuple[PdfNamedDestinationSnapshot, ...]
    attachments_fingerprint: str


def _canonical_working_copy_pdf_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_absolute():
        raise ValueError("working_copy_path must be absolute")
    if resolved.suffix.lower() != ".pdf":
        raise ValueError("working_copy_path must point to a PDF")
    return resolved


def _validate_structure_snapshot_shape(
    snapshot: PdfDocumentStructureSnapshot,
    *,
    label: str,
) -> None:
    if isinstance(snapshot.page_count, bool) or not isinstance(snapshot.page_count, Integral):
        raise ValueError(f"{label}.page_count must be an integer")
    if snapshot.page_count <= 0:
        raise ValueError(f"{label}.page_count must be positive")
    if len(snapshot.pages) != snapshot.page_count:
        raise ValueError(f"{label}.pages must match page_count")


def _crop_direct_page_keys_after_transition(
    before_keys: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(sorted(set(before_keys) | {"/CropBox"}))


def _validate_crop_target_inside_media_box(
    target_box: tuple[float, float, float, float],
    media_box: tuple[float, float, float, float],
) -> None:
    target_rect = PdfRect.from_tuple(target_box)
    media_rect = PdfRect.from_tuple(media_box)
    if target_rect.left < media_rect.left - _EPSILON:
        raise ValueError("target CropBox must stay within the original MediaBox")
    if target_rect.bottom < media_rect.bottom - _EPSILON:
        raise ValueError("target CropBox must stay within the original MediaBox")
    if target_rect.right > media_rect.right + _EPSILON:
        raise ValueError("target CropBox must stay within the original MediaBox")
    if target_rect.top > media_rect.top + _EPSILON:
        raise ValueError("target CropBox must stay within the original MediaBox")
    if target_rect.width < _MINIMUM_EXTENT - _EPSILON:
        raise ValueError("target CropBox width must be at least 1 point")
    if target_rect.height < _MINIMUM_EXTENT - _EPSILON:
        raise ValueError("target CropBox height must be at least 1 point")


def _require_snapshot_raw_box(
    value: object,
    *,
    label: str,
) -> tuple[float, float, float, float]:
    raw_value: object = tuple(value) if isinstance(value, pikepdf.Array) else value
    if not isinstance(raw_value, (tuple, list)):
        raise ValueError(f"{label} is invalid")
    if len(raw_value) != 4:
        raise ValueError(f"{label} is invalid")
    normalized: list[float] = []
    for component in raw_value:
        if isinstance(component, bool) or not isinstance(component, (Integral, float, Decimal)):
            raise ValueError(f"{label} is invalid")
        numeric_value = float(component)
        if numeric_value != numeric_value or numeric_value in {float("inf"), float("-inf")}:
            raise ValueError(f"{label} is invalid")
        normalized.append(numeric_value)
    return tuple(normalized)  # type: ignore[return-value]


def _validate_crop_snapshot_transition(
    before_snapshot: PdfDocumentStructureSnapshot,
    after_snapshot: PdfDocumentStructureSnapshot,
    changed_page_indexes: tuple[int, ...],
    original_crop_states: tuple[CropPageState, ...],
    target_crop_states: tuple[PageCropTarget, ...],
) -> None:
    if before_snapshot.metadata_fingerprint != after_snapshot.metadata_fingerprint:
        raise ValueError("metadata must stay unchanged across crop transitions")
    if before_snapshot.outlines != after_snapshot.outlines:
        raise ValueError("outlines must stay unchanged across crop transitions")
    if before_snapshot.named_destinations != after_snapshot.named_destinations:
        raise ValueError("named destinations must stay unchanged across crop transitions")
    if before_snapshot.attachments_fingerprint != after_snapshot.attachments_fingerprint:
        raise ValueError("attachments must stay unchanged across crop transitions")

    original_by_index = {state.page_index: state for state in original_crop_states}
    target_by_index = {target.page_index: target for target in target_crop_states}
    changed_index_set = set(changed_page_indexes)
    for page_index, before_page in enumerate(before_snapshot.pages):
        after_page = after_snapshot.pages[page_index]
        if page_index not in changed_index_set:
            if after_page != before_page:
                raise ValueError("untouched pages must remain identical across crop transitions")
            continue

        original_state = original_by_index[page_index]
        target_state = target_by_index[page_index]
        if original_state.effective_media_box != before_page.boxes.media_box:
            raise ValueError("original crop state MediaBox must match before snapshot")
        if original_state.effective_crop_box != before_page.boxes.crop_box:
            raise ValueError("original crop state CropBox must match before snapshot")
        if original_state.direct_crop_box_present != before_page.boxes.crop_box_direct_present:
            raise ValueError("original crop state direct CropBox flag must match before snapshot")
        if original_state.direct_crop_box_value != before_page.boxes.crop_box_direct_value:
            raise ValueError("original crop state raw direct CropBox must match before snapshot")
        if original_state.crop_box_inherited != before_page.boxes.crop_box_inherited:
            raise ValueError(
                "original crop state inherited CropBox flag must match before snapshot"
            )
        if (
            original_state.crop_box_falls_back_to_media_box
            != before_page.boxes.crop_box_falls_back_to_media_box
        ):
            raise ValueError("original crop state fallback flag must match before snapshot")
        if original_state.effective_rotation != before_page.effective_rotation:
            raise ValueError("original crop state rotation must match before snapshot")
        if target_state.crop_box == original_state.effective_crop_box:
            raise ValueError("target CropBox must differ from the original effective CropBox")
        _validate_crop_target_inside_media_box(target_state.crop_box, before_page.boxes.media_box)
        if target_state.crop_box != after_page.boxes.crop_box:
            raise ValueError("target CropBox must match after snapshot")
        if after_page.boxes.media_box != before_page.boxes.media_box:
            raise ValueError("MediaBox must stay unchanged across crop transitions")
        if after_page.boxes.trim_box != before_page.boxes.trim_box:
            raise ValueError("TrimBox must stay unchanged across crop transitions")
        if after_page.boxes.bleed_box != before_page.boxes.bleed_box:
            raise ValueError("BleedBox must stay unchanged across crop transitions")
        if after_page.boxes.art_box != before_page.boxes.art_box:
            raise ValueError("ArtBox must stay unchanged across crop transitions")
        if after_page.content_fingerprint != before_page.content_fingerprint:
            raise ValueError("page Contents must stay unchanged across crop transitions")
        if after_page.direct_resources_present != before_page.direct_resources_present:
            raise ValueError("page Resources presence must stay unchanged across crop transitions")
        if after_page.resources_fingerprint != before_page.resources_fingerprint:
            raise ValueError("page Resources must stay unchanged across crop transitions")
        if after_page.direct_rotate_present != before_page.direct_rotate_present:
            raise ValueError("page Rotate presence must stay unchanged across crop transitions")
        if after_page.direct_rotate_value != before_page.direct_rotate_value:
            raise ValueError("page Rotate value must stay unchanged across crop transitions")
        if after_page.effective_rotation != before_page.effective_rotation:
            raise ValueError("page rotation must stay unchanged across crop transitions")
        if after_page.annotations != before_page.annotations:
            raise ValueError("page annotations must stay unchanged across crop transitions")
        if after_page.extra_page_entries_fingerprint != before_page.extra_page_entries_fingerprint:
            raise ValueError("extra page entries must stay unchanged across crop transitions")
        if after_page.boxes.crop_box_direct_present is not True:
            raise ValueError("changed pages must materialize a direct CropBox in after snapshot")
        if after_page.boxes.crop_box_direct_value != target_state.crop_box:
            raise ValueError("changed pages must materialize the target raw CropBox")
        if after_page.boxes.crop_box_inherited is not False:
            raise ValueError("changed pages must not keep inherited CropBox in after snapshot")
        if after_page.boxes.crop_box_falls_back_to_media_box is not False:
            raise ValueError("changed pages must not keep MediaBox fallback in after snapshot")
        if after_page.direct_page_keys != _crop_direct_page_keys_after_transition(
            before_page.direct_page_keys
        ):
            raise ValueError("direct page keys must differ only by CropBox materialization")


@dataclass(frozen=True, slots=True)
class PageDuplicationReceipt:
    original_page_count: int
    source_page_indexes: tuple[int, ...]
    original_page_indexes_after: tuple[int, ...]
    duplicate_page_indexes: tuple[int, ...]
    before_snapshot: PdfDocumentStructureSnapshot

    def __post_init__(self) -> None:
        if isinstance(self.original_page_count, bool) or not isinstance(
            self.original_page_count,
            Integral,
        ):
            raise ValueError("original_page_count must be an integer")
        if self.original_page_count <= 0:
            raise ValueError("original_page_count must be positive")
        if self.before_snapshot.page_count != self.original_page_count:
            raise ValueError("before_snapshot page count must match original_page_count")
        source_indexes = _validate_sorted_unique_page_indexes(
            self.source_page_indexes,
            label="source_page_indexes",
            page_count=self.original_page_count,
        )
        if not source_indexes:
            raise ValueError("source_page_indexes must not be empty")

        new_page_count = self.original_page_count + len(source_indexes)
        expected_original_indexes_after = tuple(
            _all_original_indexes_after(self.original_page_count, source_indexes)[source_index]
            for source_index in source_indexes
        )
        expected_duplicate_indexes = tuple(
            page_index + 1 for page_index in expected_original_indexes_after
        )
        actual_original_indexes_after = tuple(
            _require_strict_int(value, label="original_page_indexes_after")
            for value in self.original_page_indexes_after
        )
        actual_duplicate_indexes = tuple(
            _require_strict_int(value, label="duplicate_page_indexes")
            for value in self.duplicate_page_indexes
        )
        if len(actual_original_indexes_after) != len(source_indexes):
            raise ValueError("original_page_indexes_after length must match source_page_indexes")
        if len(actual_duplicate_indexes) != len(source_indexes):
            raise ValueError("duplicate_page_indexes length must match source_page_indexes")
        if actual_original_indexes_after != expected_original_indexes_after:
            raise ValueError("original_page_indexes_after does not match the expected mapping")
        if actual_duplicate_indexes != expected_duplicate_indexes:
            raise ValueError("duplicate_page_indexes does not match the expected mapping")
        if len(set(actual_duplicate_indexes)) != len(actual_duplicate_indexes):
            raise ValueError("duplicate_page_indexes must be unique")
        for duplicate_page_index in actual_duplicate_indexes:
            if duplicate_page_index < 0 or duplicate_page_index >= new_page_count:
                raise ValueError("duplicate_page_indexes must stay within the new page range")


@dataclass(frozen=True, slots=True)
class PageDuplicationMutation:
    mutation_result: WorkingCopyMutationResult
    receipt: PageDuplicationReceipt


@dataclass(frozen=True, slots=True)
class PageDeletionReceipt:
    working_copy_path: Path
    original_page_count: int
    original_current_page_index: int
    deleted_page_indexes: tuple[int, ...]
    survivor_original_indexes: tuple[int, ...]
    before_snapshot: PdfDocumentStructureSnapshot
    after_snapshot: PdfDocumentStructureSnapshot
    undo_snapshot_path: Path
    undo_snapshot_sha256: str

    def __post_init__(self) -> None:
        if not self.working_copy_path.is_absolute():
            raise ValueError("working_copy_path must be absolute")
        if self.working_copy_path.suffix.lower() != ".pdf":
            raise ValueError("working_copy_path must point to a PDF")
        if isinstance(self.original_page_count, bool) or not isinstance(
            self.original_page_count,
            Integral,
        ):
            raise ValueError("original_page_count must be an integer")
        if self.original_page_count <= 0:
            raise ValueError("original_page_count must be positive")
        if self.before_snapshot.page_count != self.original_page_count:
            raise ValueError("before_snapshot page count must match original_page_count")
        if isinstance(self.original_current_page_index, bool) or not isinstance(
            self.original_current_page_index,
            Integral,
        ):
            raise ValueError("original_current_page_index must be an integer")
        if not 0 <= int(self.original_current_page_index) < self.original_page_count:
            raise ValueError("original_current_page_index must stay within the page range")
        deleted_indexes = _validate_sorted_unique_page_indexes(
            self.deleted_page_indexes,
            label="deleted_page_indexes",
            page_count=self.original_page_count,
        )
        if not deleted_indexes:
            raise ValueError("deleted_page_indexes must not be empty")
        if len(deleted_indexes) >= self.original_page_count:
            raise ValueError("at least one page must remain after deletion")
        expected_survivors = tuple(
            index for index in range(self.original_page_count) if index not in set(deleted_indexes)
        )
        survivor_original_indexes = tuple(
            _require_strict_int(value, label="survivor_original_indexes")
            for value in self.survivor_original_indexes
        )
        if survivor_original_indexes != expected_survivors:
            raise ValueError("survivor_original_indexes does not match the deleted pages")
        expected_after_page_count = self.original_page_count - len(deleted_indexes)
        if self.after_snapshot.page_count != expected_after_page_count:
            raise ValueError("after_snapshot page count does not match the deletion result")
        if self.undo_snapshot_path == self.working_copy_path:
            raise ValueError("undo_snapshot_path must differ from working_copy_path")
        if not self.undo_snapshot_path.is_absolute():
            raise ValueError("undo_snapshot_path must be absolute")
        if len(self.undo_snapshot_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.undo_snapshot_sha256
        ):
            raise ValueError("undo_snapshot_sha256 must be a lowercase SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class PageDeletionMutation:
    mutation_result: WorkingCopyMutationResult
    receipt: PageDeletionReceipt


@dataclass(frozen=True, slots=True)
class PageInsertionReceipt:
    working_copy_path: Path
    target_page_count_before: int
    source_snapshot_path: Path
    source_snapshot_sha256: str
    source_snapshot_page_count: int
    target_undo_snapshot_path: Path
    target_undo_snapshot_sha256: str
    target_before_snapshot: PdfDocumentStructureSnapshot
    target_after_snapshot: PdfDocumentStructureSnapshot
    source_selected_page_snapshots: tuple[PdfPageStructureSnapshot, ...]
    source_page_indexes: tuple[int, ...]
    insertion_slot: int
    inserted_page_indexes: tuple[int, ...]
    execute_transition: PageIndexTransition
    undo_transition: PageIndexTransition

    def __post_init__(self) -> None:
        if not self.working_copy_path.is_absolute():
            raise ValueError("working_copy_path must be absolute")
        if self.working_copy_path.suffix.lower() != ".pdf":
            raise ValueError("working_copy_path must point to a PDF")
        if isinstance(self.target_page_count_before, bool) or not isinstance(
            self.target_page_count_before,
            Integral,
        ):
            raise ValueError("target_page_count_before must be an integer")
        if self.target_page_count_before <= 0:
            raise ValueError("target_page_count_before must be positive")
        if self.target_before_snapshot.page_count != self.target_page_count_before:
            raise ValueError(
                "target_before_snapshot page count must match target_page_count_before"
            )
        if not self.source_snapshot_path.is_absolute():
            raise ValueError("source_snapshot_path must be absolute")
        if not self.target_undo_snapshot_path.is_absolute():
            raise ValueError("target_undo_snapshot_path must be absolute")
        if self.source_snapshot_path == self.working_copy_path:
            raise ValueError("source_snapshot_path must differ from working_copy_path")
        if self.target_undo_snapshot_path == self.working_copy_path:
            raise ValueError("target_undo_snapshot_path must differ from working_copy_path")
        if self.source_snapshot_path == self.target_undo_snapshot_path:
            raise ValueError("snapshot paths must differ")
        if len(self.source_snapshot_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_snapshot_sha256
        ):
            raise ValueError("source_snapshot_sha256 must be a lowercase SHA-256 hex digest")
        if len(self.target_undo_snapshot_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.target_undo_snapshot_sha256
        ):
            raise ValueError("target_undo_snapshot_sha256 must be a lowercase SHA-256 hex digest")
        plan = PageInsertionPlan(
            target_page_count=self.target_page_count_before,
            source_page_count=self.source_snapshot_page_count,
            source_page_indexes=self.source_page_indexes,
            insertion_slot=self.insertion_slot,
            inserted_page_indexes_after=self.inserted_page_indexes,
            target_old_to_new=self.execute_transition.cache_old_to_new,
        )
        if self.inserted_page_indexes != plan.inserted_page_indexes_after:
            raise ValueError("inserted_page_indexes does not match the insertion plan")
        if len(self.source_selected_page_snapshots) != len(plan.source_page_indexes):
            raise ValueError("source_selected_page_snapshots length must match source_page_indexes")
        if self.target_after_snapshot.page_count != (
            self.target_page_count_before + len(plan.source_page_indexes)
        ):
            raise ValueError("target_after_snapshot page count does not match the insertion result")
        if self.execute_transition.old_page_count != self.target_page_count_before:
            raise ValueError("execute_transition old_page_count is invalid")
        if self.execute_transition.new_page_count != self.target_after_snapshot.page_count:
            raise ValueError("execute_transition new_page_count is invalid")
        if self.execute_transition.cache_old_to_new != plan.target_old_to_new:
            raise ValueError("execute_transition cache_old_to_new is invalid")
        if self.execute_transition.current_page_old_to_new != plan.target_old_to_new:
            raise ValueError("execute_transition current_page_old_to_new is invalid")
        if self.undo_transition.old_page_count != self.target_after_snapshot.page_count:
            raise ValueError("undo_transition old_page_count is invalid")
        if self.undo_transition.new_page_count != self.target_page_count_before:
            raise ValueError("undo_transition new_page_count is invalid")
        expected_undo_mapping = tuple(
            None
            if page_index in set(plan.inserted_page_indexes_after)
            else (
                page_index
                if page_index < plan.insertion_slot
                else page_index - len(plan.source_page_indexes)
            )
            for page_index in range(self.target_after_snapshot.page_count)
        )
        if self.undo_transition.cache_old_to_new != expected_undo_mapping:
            raise ValueError("undo_transition cache_old_to_new is invalid")
        if self.undo_transition.current_page_old_to_new != expected_undo_mapping:
            raise ValueError("undo_transition current_page_old_to_new is invalid")
        object.__setattr__(self, "target_page_count_before", int(self.target_page_count_before))
        object.__setattr__(self, "source_page_indexes", plan.source_page_indexes)
        object.__setattr__(self, "insertion_slot", plan.insertion_slot)
        object.__setattr__(self, "inserted_page_indexes", plan.inserted_page_indexes_after)


@dataclass(frozen=True, slots=True)
class PageInsertionMutation:
    mutation_result: WorkingCopyMutationResult
    receipt: PageInsertionReceipt


@dataclass(frozen=True, slots=True)
class PageReplacementReceipt:
    working_copy_path: Path
    target_page_count_before: int
    source_snapshot_path: Path
    source_snapshot_sha256: str
    source_snapshot_page_count: int
    target_undo_snapshot_path: Path
    target_undo_snapshot_sha256: str
    target_before_snapshot: PdfDocumentStructureSnapshot
    target_after_snapshot: PdfDocumentStructureSnapshot
    source_selected_page_snapshots: tuple[PdfPageStructureSnapshot, ...]
    target_page_indexes: tuple[int, ...]
    source_page_indexes: tuple[int, ...]
    replacement_pairs: tuple[tuple[int, int], ...]
    replaced_page_indexes_after: tuple[int, ...]
    execute_transition: PageIndexTransition
    undo_transition: PageIndexTransition

    def __post_init__(self) -> None:
        if not self.working_copy_path.is_absolute():
            raise ValueError("working_copy_path must be absolute")
        if self.working_copy_path.suffix.lower() != ".pdf":
            raise ValueError("working_copy_path must point to a PDF")
        if isinstance(self.target_page_count_before, bool) or not isinstance(
            self.target_page_count_before,
            Integral,
        ):
            raise ValueError("target_page_count_before must be an integer")
        if self.target_page_count_before <= 0:
            raise ValueError("target_page_count_before must be positive")
        if self.target_before_snapshot.page_count != self.target_page_count_before:
            raise ValueError(
                "target_before_snapshot page count must match target_page_count_before"
            )
        if self.target_after_snapshot.page_count != self.target_page_count_before:
            raise ValueError("target_after_snapshot page count must stay unchanged")
        if not self.source_snapshot_path.is_absolute():
            raise ValueError("source_snapshot_path must be absolute")
        if not self.target_undo_snapshot_path.is_absolute():
            raise ValueError("target_undo_snapshot_path must be absolute")
        if self.source_snapshot_path == self.working_copy_path:
            raise ValueError("source_snapshot_path must differ from working_copy_path")
        if self.target_undo_snapshot_path == self.working_copy_path:
            raise ValueError("target_undo_snapshot_path must differ from working_copy_path")
        if self.source_snapshot_path == self.target_undo_snapshot_path:
            raise ValueError("snapshot paths must differ")
        if len(self.source_snapshot_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_snapshot_sha256
        ):
            raise ValueError("source_snapshot_sha256 must be a lowercase SHA-256 hex digest")
        if len(self.target_undo_snapshot_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.target_undo_snapshot_sha256
        ):
            raise ValueError("target_undo_snapshot_sha256 must be a lowercase SHA-256 hex digest")
        if isinstance(self.source_snapshot_page_count, bool) or not isinstance(
            self.source_snapshot_page_count,
            Integral,
        ):
            raise ValueError("source_snapshot_page_count must be an integer")
        if self.source_snapshot_page_count <= 0:
            raise ValueError("source_snapshot_page_count must be positive")
        plan = build_page_replacement_plan(
            self.target_page_count_before,
            self.source_snapshot_page_count,
            self.target_page_indexes,
            self.source_page_indexes,
        )
        if len(self.source_selected_page_snapshots) != len(plan.source_page_indexes):
            raise ValueError("source_selected_page_snapshots length must match source_page_indexes")
        if tuple(self.replacement_pairs) != plan.replacement_pairs:
            raise ValueError("replacement_pairs does not match the replacement plan")
        if tuple(self.replaced_page_indexes_after) != plan.replaced_page_indexes_after:
            raise ValueError("replaced_page_indexes_after does not match the replacement plan")
        if self.execute_transition.old_page_count != self.target_page_count_before:
            raise ValueError("execute_transition old_page_count is invalid")
        if self.execute_transition.new_page_count != self.target_page_count_before:
            raise ValueError("execute_transition new_page_count is invalid")
        if self.execute_transition.cache_old_to_new != plan.execute_cache_old_to_new:
            raise ValueError("execute_transition cache_old_to_new is invalid")
        if self.execute_transition.current_page_old_to_new != plan.execute_current_page_old_to_new:
            raise ValueError("execute_transition current_page_old_to_new is invalid")
        if self.undo_transition.old_page_count != self.target_page_count_before:
            raise ValueError("undo_transition old_page_count is invalid")
        if self.undo_transition.new_page_count != self.target_page_count_before:
            raise ValueError("undo_transition new_page_count is invalid")
        if self.undo_transition.cache_old_to_new != plan.execute_cache_old_to_new:
            raise ValueError("undo_transition cache_old_to_new is invalid")
        if self.undo_transition.current_page_old_to_new != plan.execute_current_page_old_to_new:
            raise ValueError("undo_transition current_page_old_to_new is invalid")
        object.__setattr__(self, "target_page_count_before", int(self.target_page_count_before))
        object.__setattr__(self, "source_snapshot_page_count", int(self.source_snapshot_page_count))
        object.__setattr__(self, "target_page_indexes", plan.target_page_indexes)
        object.__setattr__(self, "source_page_indexes", plan.source_page_indexes)
        object.__setattr__(self, "replacement_pairs", plan.replacement_pairs)
        object.__setattr__(self, "replaced_page_indexes_after", plan.replaced_page_indexes_after)


@dataclass(frozen=True, slots=True)
class PageReplacementMutation:
    mutation_result: WorkingCopyMutationResult
    receipt: PageReplacementReceipt


@dataclass(frozen=True, slots=True)
class PageReorderReceipt:
    original_page_count: int
    source_page_indexes: tuple[int, ...]
    insertion_slot: int
    target_order: tuple[int, ...]
    old_to_new: tuple[int, ...]
    moved_page_indexes_after: tuple[int, ...]
    before_snapshot: PdfDocumentStructureSnapshot
    after_snapshot: PdfDocumentStructureSnapshot

    def __post_init__(self) -> None:
        if isinstance(self.original_page_count, bool) or not isinstance(
            self.original_page_count,
            Integral,
        ):
            raise ValueError("original_page_count must be an integer")
        original_page_count = int(self.original_page_count)
        if original_page_count <= 0:
            raise ValueError("original_page_count must be positive")
        if self.before_snapshot.page_count != original_page_count:
            raise ValueError("before_snapshot page count must match original_page_count")
        if self.after_snapshot.page_count != original_page_count:
            raise ValueError("after_snapshot page count must match original_page_count")
        plan = PageReorderPlan(
            page_count=original_page_count,
            source_page_indexes=self.source_page_indexes,
            insertion_slot=self.insertion_slot,
            target_order=self.target_order,
            old_to_new=self.old_to_new,
            new_to_old=self.target_order,
            moved_page_indexes_after=self.moved_page_indexes_after,
        )
        object.__setattr__(self, "original_page_count", original_page_count)
        object.__setattr__(self, "source_page_indexes", plan.source_page_indexes)
        object.__setattr__(self, "insertion_slot", plan.insertion_slot)
        object.__setattr__(self, "target_order", plan.target_order)
        object.__setattr__(self, "old_to_new", plan.old_to_new)
        object.__setattr__(self, "moved_page_indexes_after", plan.moved_page_indexes_after)
        for new_page_index, original_page_index in enumerate(plan.target_order):
            if (
                self.after_snapshot.pages[new_page_index]
                != self.before_snapshot.pages[original_page_index]
            ):
                raise ValueError("after_snapshot does not match the expected reordered page order")


@dataclass(frozen=True, slots=True)
class PageReorderMutation:
    mutation_result: WorkingCopyMutationResult
    receipt: PageReorderReceipt


class PdfPageMutationService:
    def __init__(self, validator: PdfDocumentValidator | None = None) -> None:
        self._validator = validator if validator is not None else PdfDocumentValidator()

    def snapshot_document_structure(self, path: Path) -> PdfDocumentStructureSnapshot:
        return self._snapshot_document_structure(path)

    def read_page_count(self, path: Path) -> int:
        resolved_path = path.expanduser().resolve()
        try:
            with pikepdf.open(resolved_path) as pdf:
                page_count = len(pdf.pages)
        except Exception as exc:
            raise PdfPageMutationError("PDFのページ数を取得できませんでした") from exc
        if page_count <= 0:
            raise PdfPageMutationError("0ページのPDFは扱えません")
        return page_count

    def read_rotation_states(
        self,
        path: Path,
        page_indexes: tuple[int, ...],
    ) -> tuple[PageRotationState, ...]:
        resolved_path = path.expanduser().resolve()
        try:
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                root = cast(Any, reader.trailer["/Root"])
                root_pages = cast(Any, root["/Pages"]).get_object()
                page_objects = self._collect_page_objects(root_pages)
                validated_indexes = self._validate_page_indexes(page_indexes, len(page_objects))
                return tuple(
                    self._rotation_state_for_page(page_objects[page_index], page_index)
                    for page_index in validated_indexes
                )
        except PdfPageMutationError:
            raise
        except ValueError:
            raise
        except OSError as exc:
            raise PdfPageMutationError("PDFの回転状態を読み取れませんでした") from exc
        except Exception as exc:
            raise PdfPageMutationError("PDFの回転状態を読み取れませんでした") from exc

    def apply_rotation_states(
        self,
        path: Path,
        states: tuple[PageRotationState, ...],
    ) -> WorkingCopyMutationResult:
        if not states:
            raise ValueError("states must not be empty")
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            with pikepdf.open(resolved_path) as pdf:
                pages = list(pdf.pages)
                page_count = len(pages)
                self._validate_page_indexes(
                    tuple(state.page_index for state in states),
                    page_count,
                )
                state_map = {state.page_index: state for state in states}
                self._validate_states(states)
                rotation_snapshot = self.read_rotation_states(
                    resolved_path,
                    tuple(range(page_count)),
                )
                box_snapshot = tuple(self._page_box_state(page) for page in pages)
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                writer = PdfWriter(clone_from=reader)
                root_pages = cast(Any, writer.root_object["/Pages"]).get_object()
                page_objects = self._collect_page_objects(root_pages)
                candidate_path = self._create_candidate_path(resolved_path)
                for page_index, original_state in enumerate(rotation_snapshot):
                    state = state_map.get(page_index, original_state)
                    page_object = cast(dict[object, object], page_objects[page_index])
                    if state.direct_rotate_present:
                        if state.direct_rotate_value is None:
                            raise PdfPageMutationError("回転値が不正です")
                        page_object[NameObject("/Rotate")] = NumberObject(state.direct_rotate_value)
                    else:
                        page_object.pop(NameObject("/Rotate"), None)
                self._write_candidate(writer, candidate_path)

            self._validate_rotation_candidate(
                candidate_path,
                expected_page_count=page_count,
                expected_states=states,
                original_rotation_snapshot=rotation_snapshot,
                original_box_snapshot=box_snapshot,
            )
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=page_count,
                affected_pages=frozenset(state.page_index for state in states),
            )
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def read_crop_states(
        self,
        path: Path,
        page_indexes: Sequence[object],
    ) -> tuple[CropPageState, ...]:
        normalized_indexes = tuple(
            _require_strict_int(page_index, label="page_indexes") for page_index in page_indexes
        )
        resolved_path = path.expanduser().resolve()
        try:
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                root = cast(Any, reader.trailer["/Root"])
                root_pages = cast(Any, root["/Pages"]).get_object()
                page_objects = self._collect_page_objects(root_pages)
                validated_indexes = self._validate_page_indexes(
                    normalized_indexes,
                    len(page_objects),
                )
                return tuple(
                    self._crop_state_for_page_object(page_objects[page_index], page_index)
                    for page_index in validated_indexes
                )
        except PdfPageMutationError:
            raise
        except ValueError:
            raise
        except Exception as exc:
            raise PdfPageMutationError("PDFのトリミング状態を読み取れませんでした") from exc

    def crop_pages(
        self,
        path: Path,
        plan: PageCropPlan,
        *,
        expected_before_snapshot: PdfDocumentStructureSnapshot | None = None,
    ) -> PageCropMutation:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            before_snapshot = self._snapshot_document_structure(resolved_path)
            if expected_before_snapshot is not None and before_snapshot != expected_before_snapshot:
                raise PdfPageMutationError("トリミング対象ページの前提状態が変化しました")
            original_crop_states = self.read_crop_states(resolved_path, plan.page_indexes)
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                writer = PdfWriter(clone_from=reader)
                root_pages = cast(Any, writer.root_object["/Pages"]).get_object()
                page_objects = self._collect_page_objects(root_pages)
                candidate_path = self._create_candidate_path(resolved_path)
                targeted_page_indexes = frozenset(plan.page_indexes)
                self._restore_inherited_crop_boxes_for_untouched_pages(
                    page_objects,
                    reference_snapshot=before_snapshot,
                    changed_page_indexes=targeted_page_indexes,
                )
                for target in plan.targets:
                    page_object = cast(dict[object, object], page_objects[target.page_index])
                    page_object[NameObject("/CropBox")] = self._crop_box_array_object(
                        target.crop_box
                    )
                self._write_candidate(writer, candidate_path)
            after_snapshot = self._validate_page_crop_candidate(
                candidate_path,
                before_snapshot=before_snapshot,
                original_crop_states=original_crop_states,
                target_crop_states=plan.targets,
            )
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            transition = self._build_crop_transition(before_snapshot.page_count, plan.page_indexes)
            receipt = PageCropReceipt(
                working_copy_path=resolved_path,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                original_crop_states=original_crop_states,
                target_crop_states=plan.targets,
                changed_page_indexes=plan.page_indexes,
                execute_transition=transition,
                undo_transition=transition,
            )
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=before_snapshot.page_count,
                affected_pages=frozenset(plan.page_indexes),
                page_index_transition=transition,
            )
            prepared = PageCropMutation(mutation_result=mutation_result, receipt=receipt)
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return prepared
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except TypeError as exc:
            primary_error = exc
            raise
        except ValueError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def undo_page_crop(
        self,
        path: Path,
        receipt: PageCropReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None
        try:
            self._validate_crop_receipt_ownership(resolved_path, receipt)
            old_revision = DocumentRevision.from_path(resolved_path)
            self._validate_current_crop_state(resolved_path, receipt.after_snapshot)
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                writer = PdfWriter(clone_from=reader)
                root_pages = cast(Any, writer.root_object["/Pages"]).get_object()
                page_objects = self._collect_page_objects(root_pages)
                candidate_path = self._create_candidate_path(resolved_path)
                changed_page_indexes = frozenset(receipt.changed_page_indexes)
                self._restore_inherited_crop_boxes_for_untouched_pages(
                    page_objects,
                    reference_snapshot=receipt.before_snapshot,
                    changed_page_indexes=changed_page_indexes,
                )
                for state in receipt.original_crop_states:
                    page_object = cast(dict[object, object], page_objects[state.page_index])
                    if state.direct_crop_box_present:
                        direct_value = state.direct_crop_box_value
                        if direct_value is None:
                            raise PdfPageMutationError("元のCropBox状態が不正です")
                        page_object[NameObject("/CropBox")] = self._crop_box_array_object(
                            direct_value
                        )
                    else:
                        page_object.pop(NameObject("/CropBox"), None)
                self._write_candidate(writer, candidate_path)
            self._validate_undo_page_crop_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.before_snapshot.page_count,
                affected_pages=frozenset(receipt.changed_page_indexes),
                page_index_transition=receipt.undo_transition,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def redo_page_crop(
        self,
        path: Path,
        receipt: PageCropReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None
        try:
            self._validate_crop_receipt_ownership(resolved_path, receipt)
            old_revision = DocumentRevision.from_path(resolved_path)
            self._validate_current_crop_redo_state(resolved_path, receipt)
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                writer = PdfWriter(clone_from=reader)
                root_pages = cast(Any, writer.root_object["/Pages"]).get_object()
                page_objects = self._collect_page_objects(root_pages)
                candidate_path = self._create_candidate_path(resolved_path)
                changed_page_indexes = frozenset(receipt.changed_page_indexes)
                self._restore_inherited_crop_boxes_for_untouched_pages(
                    page_objects,
                    reference_snapshot=receipt.after_snapshot,
                    changed_page_indexes=changed_page_indexes,
                )
                for target in receipt.target_crop_states:
                    page_object = cast(dict[object, object], page_objects[target.page_index])
                    page_object[NameObject("/CropBox")] = self._crop_box_array_object(
                        target.crop_box
                    )
                self._write_candidate(writer, candidate_path)
            self._validate_redo_page_crop_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.after_snapshot.page_count,
                affected_pages=frozenset(receipt.changed_page_indexes),
                page_index_transition=receipt.execute_transition,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def duplicate_pages(
        self,
        path: Path,
        page_indexes: tuple[int, ...],
        *,
        expected_before_snapshot: PdfDocumentStructureSnapshot | None = None,
    ) -> PageDuplicationMutation:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            before_snapshot = self._snapshot_document_structure(resolved_path)
            if expected_before_snapshot is not None and before_snapshot != expected_before_snapshot:
                raise PdfPageMutationError("複製対象ページの前提状態が変化しました")
            normalized_page_indexes = self._normalize_duplicate_page_indexes(
                page_indexes,
                before_snapshot.page_count,
            )
            self._reject_unsupported_forms(resolved_path, normalized_page_indexes)
            original_indexes_after = _all_original_indexes_after(
                before_snapshot.page_count,
                normalized_page_indexes,
            )
            duplicate_page_indexes = tuple(
                original_indexes_after[source_index] + 1 for source_index in normalized_page_indexes
            )
            receipt = PageDuplicationReceipt(
                original_page_count=before_snapshot.page_count,
                source_page_indexes=normalized_page_indexes,
                original_page_indexes_after=tuple(
                    original_indexes_after[source_index] for source_index in normalized_page_indexes
                ),
                duplicate_page_indexes=duplicate_page_indexes,
                before_snapshot=before_snapshot,
            )
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                writer = PdfWriter(clone_from=reader)
                self._apply_page_duplication_to_writer(writer, reader, receipt)
                candidate_path = self._create_candidate_path(resolved_path)
                self._write_candidate(writer, candidate_path)
            self._validate_page_duplication_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return PageDuplicationMutation(
                mutation_result=WorkingCopyMutationResult(
                    old_revision=old_revision,
                    new_revision=new_revision,
                    page_count=before_snapshot.page_count + len(normalized_page_indexes),
                    affected_pages=frozenset(duplicate_page_indexes),
                    page_index_transition=self._build_execute_transition(
                        before_snapshot.page_count,
                        normalized_page_indexes,
                    ),
                ),
                receipt=receipt,
            )
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except TypeError:
            raise
        except ValueError:
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def undo_page_duplication(
        self,
        path: Path,
        receipt: PageDuplicationReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            self._validate_current_duplication_state(resolved_path, receipt)
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                writer = PdfWriter(clone_from=reader)
                for page_index in sorted(receipt.duplicate_page_indexes, reverse=True):
                    writer.remove_page(page_index)
                candidate_path = self._create_candidate_path(resolved_path)
                self._write_candidate(writer, candidate_path)
            self._validate_undo_duplication_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.original_page_count,
                affected_pages=frozenset(receipt.source_page_indexes),
                page_index_transition=self._build_undo_transition(receipt),
            )
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def insert_pages_from_pdf(
        self,
        target_path: Path,
        source_path: Path,
        source_page_indexes: Sequence[object],
        insertion_slot: object,
        *,
        expected_target_snapshot: PdfDocumentStructureSnapshot | None = None,
    ) -> PageInsertionMutation:
        resolved_target_path = target_path.expanduser().resolve()
        resolved_source_path = source_path.expanduser().resolve()
        candidate_path: Path | None = None
        source_snapshot_path: Path | None = None
        target_undo_snapshot_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_target_path)
            target_before_snapshot = self._snapshot_document_structure(resolved_target_path)
            if (
                expected_target_snapshot is not None
                and target_before_snapshot != expected_target_snapshot
            ):
                raise PdfPageMutationError("挿入対象PDFの前提状態が変化しました")
            self._reject_unsupported_page_insertion_target_structures(resolved_target_path)

            source_original_fingerprint = self._source_snapshot_fingerprint(resolved_source_path)
            source_original_sha256 = self._sha256_file(resolved_source_path)
            source_snapshot_path = self._create_insert_source_snapshot_path(resolved_target_path)
            self._copy_named_snapshot(
                resolved_source_path,
                source_snapshot_path,
                create_error="挿入元スナップショットPDFの作成に失敗しました",
                fsync_error="挿入元スナップショットPDFの同期に失敗しました",
            )
            source_snapshot_sha256 = self._sha256_file(source_snapshot_path)
            if source_snapshot_sha256 != source_original_sha256:
                raise PdfPageMutationError("挿入元スナップショットPDFの整合性検証に失敗しました")
            source_current_fingerprint = self._source_snapshot_fingerprint(resolved_source_path)
            source_current_sha256 = self._sha256_file(resolved_source_path)
            if (
                source_current_fingerprint != source_original_fingerprint
                or source_current_sha256 != source_original_sha256
            ):
                raise PdfPageMutationError("挿入元PDFが読み取り中に変更されました")

            source_snapshot_page_count = self.read_page_count(source_snapshot_path)
            normalized_source_page_indexes = tuple(
                _require_strict_int(page_index, label="source_page_indexes")
                for page_index in source_page_indexes
            )
            source_selection = _validate_sorted_unique_page_indexes(
                normalized_source_page_indexes,
                label="source_page_indexes",
                page_count=source_snapshot_page_count,
            )
            plan = build_page_insertion_plan(
                target_before_snapshot.page_count,
                source_snapshot_page_count,
                source_selection,
                insertion_slot,
            )
            self._validate_insert_source_snapshot_path(
                source_snapshot_path,
                expected_page_count=source_snapshot_page_count,
                render_page_indexes=plan.source_page_indexes,
            )
            self._reject_unsupported_import_source_structures(
                source_snapshot_path,
                plan.source_page_indexes,
                operation_label="挿入元",
            )
            source_selected_page_snapshots = self._snapshot_selected_source_pages(
                source_snapshot_path,
                plan.source_page_indexes,
            )

            target_undo_snapshot_path = self._create_insert_undo_snapshot(
                resolved_target_path,
                expected_page_count=target_before_snapshot.page_count,
                render_page_indexes=self._target_snapshot_validation_page_indexes(
                    target_before_snapshot.page_count,
                    plan.insertion_slot,
                ),
            )
            target_undo_snapshot_sha256 = self._sha256_file(target_undo_snapshot_path)

            with (
                pikepdf.open(resolved_target_path) as target_pdf,
                pikepdf.open(source_snapshot_path) as source_pdf,
            ):
                for offset, source_page_index in enumerate(plan.source_page_indexes):
                    target_pdf.pages.insert(
                        plan.insertion_slot + offset,
                        source_pdf.pages[source_page_index],
                    )
                for offset, source_page_snapshot in enumerate(source_selected_page_snapshots):
                    source_page = source_pdf.pages[plan.source_page_indexes[offset]]
                    inserted_page = target_pdf.pages[plan.insertion_slot + offset]
                    self._materialize_imported_page_structure(
                        inserted_page,
                        source_page_snapshot,
                    )
                    self._copy_imported_page_annotations(
                        target_pdf,
                        source_pdf,
                        source_page=source_page,
                        inserted_page=inserted_page,
                    )
                candidate_path = self._create_candidate_path(resolved_target_path)
                self._write_pikepdf_candidate(target_pdf, candidate_path)

            target_after_snapshot = self._validate_page_insertion_candidate(
                candidate_path,
                source_snapshot_path=source_snapshot_path,
                target_before_snapshot=target_before_snapshot,
                source_selected_page_snapshots=source_selected_page_snapshots,
                plan=plan,
            )
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_target_path)
            execute_transition = self._build_insert_execute_transition(plan)
            undo_transition = self._build_insert_undo_transition(plan)
            receipt = PageInsertionReceipt(
                working_copy_path=resolved_target_path,
                target_page_count_before=target_before_snapshot.page_count,
                source_snapshot_path=source_snapshot_path,
                source_snapshot_sha256=source_snapshot_sha256,
                source_snapshot_page_count=source_snapshot_page_count,
                target_undo_snapshot_path=target_undo_snapshot_path,
                target_undo_snapshot_sha256=target_undo_snapshot_sha256,
                target_before_snapshot=target_before_snapshot,
                target_after_snapshot=target_after_snapshot,
                source_selected_page_snapshots=source_selected_page_snapshots,
                source_page_indexes=plan.source_page_indexes,
                insertion_slot=plan.insertion_slot,
                inserted_page_indexes=plan.inserted_page_indexes_after,
                execute_transition=execute_transition,
                undo_transition=undo_transition,
            )
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=target_after_snapshot.page_count,
                affected_pages=frozenset(plan.inserted_page_indexes_after),
                page_index_transition=execute_transition,
            )
            prepared_result = PageInsertionMutation(
                mutation_result=mutation_result,
                receipt=receipt,
            )
            self._replace_atomically(candidate_path, resolved_target_path)
            self._fsync_parent_directory(resolved_target_path.parent)
            return prepared_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except TypeError as exc:
            primary_error = exc
            raise
        except ValueError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)
            if primary_error is not None:
                self._cleanup_insert_snapshot(source_snapshot_path, primary_error=primary_error)
                self._cleanup_insert_snapshot(
                    target_undo_snapshot_path,
                    primary_error=primary_error,
                )

    def undo_page_insertion(
        self,
        path: Path,
        receipt: PageInsertionReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None
        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            self._validate_current_insertion_state(resolved_path, receipt)
            self._validate_insert_target_undo_snapshot(resolved_path, receipt)
            candidate_path = self._create_candidate_path(resolved_path)
            self._copy_named_snapshot(
                receipt.target_undo_snapshot_path,
                candidate_path,
                create_error="挿入前スナップショットPDFの復元に失敗しました",
                fsync_error="挿入前スナップショットPDFの復元に失敗しました",
            )
            self._validate_undo_page_insertion_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.target_page_count_before,
                affected_pages=frozenset(),
                page_index_transition=receipt.undo_transition,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return mutation_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def validate_insertion_redo_precondition(
        self,
        path: Path,
        receipt: PageInsertionReceipt,
    ) -> None:
        resolved_path = path.expanduser().resolve()
        current_snapshot = self._snapshot_document_structure(resolved_path)
        if current_snapshot != receipt.target_before_snapshot:
            raise PdfPageMutationError("挿入対象PDFの前提状態が変化しました")
        self._validate_insert_source_snapshot(resolved_path, receipt)
        self._validate_insert_target_undo_snapshot(resolved_path, receipt)
        self._reject_unsupported_page_insertion_target_structures(resolved_path)

    def redo_page_insertion(
        self,
        path: Path,
        receipt: PageInsertionReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None
        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            self.validate_insertion_redo_precondition(resolved_path, receipt)
            plan = build_page_insertion_plan(
                receipt.target_page_count_before,
                receipt.source_snapshot_page_count,
                receipt.source_page_indexes,
                receipt.insertion_slot,
            )
            with (
                pikepdf.open(resolved_path) as target_pdf,
                pikepdf.open(receipt.source_snapshot_path) as source_pdf,
            ):
                for offset, source_page_index in enumerate(plan.source_page_indexes):
                    target_pdf.pages.insert(
                        plan.insertion_slot + offset,
                        source_pdf.pages[source_page_index],
                    )
                for offset, source_page_snapshot in enumerate(
                    receipt.source_selected_page_snapshots
                ):
                    source_page = source_pdf.pages[plan.source_page_indexes[offset]]
                    inserted_page = target_pdf.pages[plan.insertion_slot + offset]
                    self._materialize_imported_page_structure(
                        inserted_page,
                        source_page_snapshot,
                    )
                    self._copy_imported_page_annotations(
                        target_pdf,
                        source_pdf,
                        source_page=source_page,
                        inserted_page=inserted_page,
                    )
                candidate_path = self._create_candidate_path(resolved_path)
                self._write_pikepdf_candidate(target_pdf, candidate_path)
            self._validate_redo_page_insertion_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.target_after_snapshot.page_count,
                affected_pages=frozenset(receipt.inserted_page_indexes),
                page_index_transition=receipt.execute_transition,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return mutation_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def discard_page_insertion_receipt(
        self,
        working_copy_path: Path,
        receipt: PageInsertionReceipt,
    ) -> None:
        source_error: PdfPageMutationError | None = None
        undo_error: PdfPageMutationError | None = None
        try:
            snapshot_path = self._validate_insert_source_snapshot_ownership(
                working_copy_path,
                receipt,
                require_exists=False,
            )
            if snapshot_path.exists():
                snapshot_path.unlink()
        except PdfPageMutationError as exc:
            source_error = exc
        except OSError as exc:
            source_error = PdfPageMutationError("挿入元スナップショットPDFの削除に失敗しました")
            source_error.__cause__ = exc
        try:
            snapshot_path = self._validate_insert_target_undo_snapshot_ownership(
                working_copy_path,
                receipt,
                require_exists=False,
            )
            if snapshot_path.exists():
                snapshot_path.unlink()
        except PdfPageMutationError as exc:
            undo_error = exc
        except OSError as exc:
            undo_error = PdfPageMutationError("挿入前スナップショットPDFの削除に失敗しました")
            undo_error.__cause__ = exc
        if source_error is not None:
            raise source_error
        if undo_error is not None:
            raise undo_error

    def replace_pages_from_pdf(
        self,
        target_path: Path,
        source_path: Path,
        target_page_indexes: Sequence[object],
        source_page_indexes: Sequence[object],
        *,
        expected_target_snapshot: PdfDocumentStructureSnapshot | None = None,
        expected_source_revision: SourcePdfRevision | None = None,
    ) -> PageReplacementMutation:
        resolved_target_path = target_path.expanduser().resolve()
        resolved_source_path = source_path.expanduser().resolve()
        candidate_path: Path | None = None
        source_snapshot_path: Path | None = None
        target_undo_snapshot_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_target_path)
            target_before_snapshot = self._snapshot_document_structure(resolved_target_path)
            if (
                expected_target_snapshot is not None
                and target_before_snapshot != expected_target_snapshot
            ):
                raise PdfPageMutationError("置換対象PDFの前提状態が変化しました")
            self._reject_unsupported_page_insertion_target_structures(resolved_target_path)

            if expected_source_revision is not None:
                self._validate_expected_source_revision(
                    resolved_source_path,
                    expected_source_revision,
                    operation_label="置換元",
                )
                source_original_fingerprint = expected_source_revision.fingerprint
                source_original_sha256 = expected_source_revision.sha256
            else:
                source_original_fingerprint = self._source_snapshot_fingerprint(
                    resolved_source_path,
                    operation_label="置換元",
                )
                source_original_sha256 = self._sha256_file(resolved_source_path)
            source_snapshot_path = self._create_replace_source_snapshot_path(resolved_target_path)
            self._copy_named_snapshot(
                resolved_source_path,
                source_snapshot_path,
                create_error="置換元スナップショットPDFの作成に失敗しました",
                fsync_error="置換元スナップショットPDFの同期に失敗しました",
            )
            source_snapshot_sha256 = self._sha256_file(source_snapshot_path)
            if source_snapshot_sha256 != source_original_sha256:
                raise PdfPageMutationError("置換元スナップショットPDFの整合性検証に失敗しました")
            source_current_fingerprint = self._source_snapshot_fingerprint(
                resolved_source_path,
                operation_label="置換元",
            )
            source_current_sha256 = self._sha256_file(resolved_source_path)
            if (
                source_current_fingerprint != source_original_fingerprint
                or source_current_sha256 != source_original_sha256
            ):
                raise PdfPageMutationError("置換元PDFが読み取り中に変更されました")

            source_snapshot_page_count = self.read_page_count(source_snapshot_path)
            if expected_source_revision is not None:
                if source_snapshot_sha256 != expected_source_revision.sha256:
                    raise PdfPageMutationError(
                        "置換元スナップショットPDFの整合性検証に失敗しました"
                    )
                if source_snapshot_page_count != expected_source_revision.page_count:
                    raise PdfPageMutationError("置換元PDFのページ数が変化しました")
            plan = build_page_replacement_plan(
                target_before_snapshot.page_count,
                source_snapshot_page_count,
                tuple(
                    _require_strict_int(page_index, label="target_page_indexes")
                    for page_index in target_page_indexes
                ),
                tuple(
                    _require_strict_int(page_index, label="source_page_indexes")
                    for page_index in source_page_indexes
                ),
            )
            self._validate_replace_source_snapshot_path(
                source_snapshot_path,
                expected_page_count=source_snapshot_page_count,
                render_page_indexes=plan.source_page_indexes,
            )
            self._reject_unsupported_import_source_structures(
                source_snapshot_path,
                plan.source_page_indexes,
                operation_label="置換元",
            )
            source_selected_page_snapshots = self._snapshot_selected_source_pages(
                source_snapshot_path,
                plan.source_page_indexes,
            )
            target_undo_snapshot_path = self._create_replace_undo_snapshot(
                resolved_target_path,
                expected_page_count=target_before_snapshot.page_count,
                render_page_indexes=plan.target_page_indexes,
            )
            target_undo_snapshot_sha256 = self._sha256_file(target_undo_snapshot_path)

            with (
                pikepdf.open(resolved_target_path) as target_pdf,
                pikepdf.open(source_snapshot_path) as source_pdf,
            ):
                self._apply_page_replacement_pairs(
                    target_pdf,
                    source_pdf,
                    plan=plan,
                    source_selected_page_snapshots=source_selected_page_snapshots,
                )
                candidate_path = self._create_candidate_path(resolved_target_path)
                self._write_pikepdf_candidate(target_pdf, candidate_path)

            target_after_snapshot = self._validate_page_replacement_candidate(
                candidate_path,
                source_snapshot_path=source_snapshot_path,
                target_before_snapshot=target_before_snapshot,
                source_selected_page_snapshots=source_selected_page_snapshots,
                plan=plan,
            )
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_target_path)
            execute_transition = self._build_replace_execute_transition(plan)
            undo_transition = self._build_replace_undo_transition(plan)
            receipt = PageReplacementReceipt(
                working_copy_path=resolved_target_path,
                target_page_count_before=target_before_snapshot.page_count,
                source_snapshot_path=source_snapshot_path,
                source_snapshot_sha256=source_snapshot_sha256,
                source_snapshot_page_count=source_snapshot_page_count,
                target_undo_snapshot_path=target_undo_snapshot_path,
                target_undo_snapshot_sha256=target_undo_snapshot_sha256,
                target_before_snapshot=target_before_snapshot,
                target_after_snapshot=target_after_snapshot,
                source_selected_page_snapshots=source_selected_page_snapshots,
                target_page_indexes=plan.target_page_indexes,
                source_page_indexes=plan.source_page_indexes,
                replacement_pairs=plan.replacement_pairs,
                replaced_page_indexes_after=plan.replaced_page_indexes_after,
                execute_transition=execute_transition,
                undo_transition=undo_transition,
            )
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=target_after_snapshot.page_count,
                affected_pages=frozenset(plan.replaced_page_indexes_after),
                page_index_transition=execute_transition,
            )
            prepared_result = PageReplacementMutation(
                mutation_result=mutation_result,
                receipt=receipt,
            )
            self._replace_atomically(candidate_path, resolved_target_path)
            self._fsync_parent_directory(resolved_target_path.parent)
            return prepared_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except TypeError as exc:
            primary_error = exc
            raise
        except ValueError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)
            if primary_error is not None:
                self._cleanup_insert_snapshot(source_snapshot_path, primary_error=primary_error)
                self._cleanup_insert_snapshot(
                    target_undo_snapshot_path,
                    primary_error=primary_error,
                )

    def undo_page_replacement(
        self,
        path: Path,
        receipt: PageReplacementReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None
        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            self._validate_current_replacement_state(resolved_path, receipt)
            self._validate_replace_target_undo_snapshot(resolved_path, receipt)
            candidate_path = self._create_candidate_path(resolved_path)
            self._copy_named_snapshot(
                receipt.target_undo_snapshot_path,
                candidate_path,
                create_error="置換前スナップショットPDFの復元に失敗しました",
                fsync_error="置換前スナップショットPDFの復元に失敗しました",
            )
            self._validate_undo_page_replacement_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.target_page_count_before,
                affected_pages=frozenset(receipt.target_page_indexes),
                page_index_transition=receipt.undo_transition,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return mutation_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def validate_replacement_redo_precondition(
        self,
        path: Path,
        receipt: PageReplacementReceipt,
    ) -> None:
        resolved_path = path.expanduser().resolve()
        current_snapshot = self._snapshot_document_structure(resolved_path)
        if current_snapshot != receipt.target_before_snapshot:
            raise PdfPageMutationError("置換対象PDFの前提状態が変化しました")
        self._validate_replace_source_snapshot(resolved_path, receipt)
        self._validate_replace_target_undo_snapshot(resolved_path, receipt)
        self._reject_unsupported_page_insertion_target_structures(resolved_path)

    def redo_page_replacement(
        self,
        path: Path,
        receipt: PageReplacementReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None
        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            self.validate_replacement_redo_precondition(resolved_path, receipt)
            plan = build_page_replacement_plan(
                receipt.target_page_count_before,
                receipt.source_snapshot_page_count,
                receipt.target_page_indexes,
                receipt.source_page_indexes,
            )
            with (
                pikepdf.open(resolved_path) as target_pdf,
                pikepdf.open(receipt.source_snapshot_path) as source_pdf,
            ):
                self._apply_page_replacement_pairs(
                    target_pdf,
                    source_pdf,
                    plan=plan,
                    source_selected_page_snapshots=receipt.source_selected_page_snapshots,
                )
                candidate_path = self._create_candidate_path(resolved_path)
                self._write_pikepdf_candidate(target_pdf, candidate_path)
            self._validate_redo_page_replacement_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.target_after_snapshot.page_count,
                affected_pages=frozenset(receipt.target_page_indexes),
                page_index_transition=receipt.execute_transition,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return mutation_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def discard_page_replacement_receipt(
        self,
        working_copy_path: Path,
        receipt: PageReplacementReceipt,
    ) -> None:
        source_error: PdfPageMutationError | None = None
        undo_error: PdfPageMutationError | None = None
        try:
            snapshot_path = self._validate_replace_source_snapshot_ownership(
                working_copy_path,
                receipt,
                require_exists=False,
            )
            if snapshot_path.exists():
                snapshot_path.unlink()
        except PdfPageMutationError as exc:
            source_error = exc
        except OSError as exc:
            source_error = PdfPageMutationError("置換元スナップショットPDFの削除に失敗しました")
            source_error.__cause__ = exc
        try:
            snapshot_path = self._validate_replace_target_undo_snapshot_ownership(
                working_copy_path,
                receipt,
                require_exists=False,
            )
            if snapshot_path.exists():
                snapshot_path.unlink()
        except PdfPageMutationError as exc:
            undo_error = exc
        except OSError as exc:
            undo_error = PdfPageMutationError("置換前スナップショットPDFの削除に失敗しました")
            undo_error.__cause__ = exc
        if source_error is not None:
            raise source_error
        if undo_error is not None:
            raise undo_error

    def reorder_pages(
        self,
        path: Path,
        source_page_indexes: Sequence[object],
        insertion_slot: object,
        *,
        expected_before_snapshot: PdfDocumentStructureSnapshot | None = None,
    ) -> PageReorderMutation:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            before_snapshot = self._snapshot_document_structure(resolved_path)
            if expected_before_snapshot is not None and before_snapshot != expected_before_snapshot:
                raise PdfPageMutationError("並べ替え対象ページの前提状態が変化しました")
            plan = build_page_reorder_plan(
                before_snapshot.page_count,
                source_page_indexes,
                insertion_slot,
            )
            self._reject_unsupported_page_reordering_structures(resolved_path)
            with pikepdf.open(resolved_path) as pdf:
                self._apply_page_order_to_pdf(pdf, plan.target_order)
                candidate_path = self._create_candidate_path(resolved_path)
                self._write_pikepdf_candidate(pdf, candidate_path)
            after_snapshot = self._validate_page_reordering_candidate(
                candidate_path,
                before_snapshot=before_snapshot,
                plan=plan,
            )
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=before_snapshot.page_count,
                affected_pages=self._reorder_affected_pages(plan),
                page_index_transition=self._build_reorder_execute_transition(plan),
            )
            receipt = PageReorderReceipt(
                original_page_count=before_snapshot.page_count,
                source_page_indexes=plan.source_page_indexes,
                insertion_slot=plan.insertion_slot,
                target_order=plan.target_order,
                old_to_new=plan.old_to_new,
                moved_page_indexes_after=plan.moved_page_indexes_after,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
            )
            prepared_result = PageReorderMutation(
                mutation_result=mutation_result,
                receipt=receipt,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return prepared_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except TypeError as exc:
            primary_error = exc
            raise
        except ValueError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def undo_page_reordering(
        self,
        path: Path,
        receipt: PageReorderReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            self._validate_current_reordering_state(resolved_path, receipt)
            plan = self._reorder_plan_from_receipt(receipt)
            with pikepdf.open(resolved_path) as pdf:
                self._apply_page_order_to_pdf(pdf, plan.old_to_new)
                candidate_path = self._create_candidate_path(resolved_path)
                self._write_pikepdf_candidate(pdf, candidate_path)
            self._validate_undo_page_reordering_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.original_page_count,
                affected_pages=self._reorder_affected_pages(plan),
                page_index_transition=self._build_reorder_undo_transition(receipt),
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return mutation_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def validate_reordering_redo_precondition(
        self,
        path: Path,
        receipt: PageReorderReceipt,
    ) -> None:
        resolved_path = path.expanduser().resolve()
        current_snapshot = self._snapshot_document_structure(resolved_path)
        if current_snapshot != receipt.before_snapshot:
            raise PdfPageMutationError("並べ替え対象ページの前提状態が変化しました")
        self._reject_unsupported_page_reordering_structures(resolved_path)

    def redo_page_reordering(
        self,
        path: Path,
        receipt: PageReorderReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            self.validate_reordering_redo_precondition(resolved_path, receipt)
            plan = self._reorder_plan_from_receipt(receipt)
            with pikepdf.open(resolved_path) as pdf:
                self._apply_page_order_to_pdf(pdf, plan.target_order)
                candidate_path = self._create_candidate_path(resolved_path)
                self._write_pikepdf_candidate(pdf, candidate_path)
            self._validate_redo_page_reordering_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.original_page_count,
                affected_pages=self._reorder_affected_pages(plan),
                page_index_transition=self._build_reorder_execute_transition(plan),
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return mutation_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def delete_pages(
        self,
        path: Path,
        page_indexes: tuple[int, ...],
        *,
        current_page_index: object,
    ) -> PageDeletionMutation:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        undo_snapshot_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            before_snapshot = self._snapshot_document_structure(resolved_path)
            validated_current_page_index = self._validate_delete_current_page_index(
                current_page_index,
                page_count=before_snapshot.page_count,
            )
            deleted_page_indexes = self._normalize_delete_page_indexes(
                page_indexes,
                before_snapshot.page_count,
            )
            self._reject_unsupported_page_deletion_structures(resolved_path, deleted_page_indexes)
            undo_snapshot_path, undo_snapshot_sha256 = self._create_delete_undo_snapshot(
                resolved_path,
                expected_page_count=before_snapshot.page_count,
            )
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                writer = PdfWriter(clone_from=reader)
                for page_index in sorted(deleted_page_indexes, reverse=True):
                    writer.remove_page(page_index)
                candidate_path = self._create_candidate_path(resolved_path)
                self._write_candidate(writer, candidate_path)
            after_snapshot = self._validate_page_deletion_candidate(
                candidate_path,
                before_snapshot=before_snapshot,
                deleted_page_indexes=deleted_page_indexes,
            )
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            survivor_original_indexes = self._survivor_original_indexes(
                before_snapshot.page_count,
                deleted_page_indexes,
            )
            transition = self._build_delete_execute_transition(
                before_snapshot.page_count,
                deleted_page_indexes,
            )
            receipt = PageDeletionReceipt(
                working_copy_path=resolved_path,
                original_page_count=before_snapshot.page_count,
                original_current_page_index=validated_current_page_index,
                deleted_page_indexes=deleted_page_indexes,
                survivor_original_indexes=survivor_original_indexes,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                undo_snapshot_path=undo_snapshot_path,
                undo_snapshot_sha256=undo_snapshot_sha256,
            )
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=after_snapshot.page_count,
                affected_pages=frozenset(),
                page_index_transition=transition,
            )
            prepared_result = PageDeletionMutation(
                mutation_result=mutation_result,
                receipt=receipt,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return prepared_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except TypeError as exc:
            primary_error = exc
            raise
        except ValueError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)
            if primary_error is not None:
                self._cleanup_delete_undo_snapshot(
                    undo_snapshot_path,
                    primary_error=primary_error,
                )

    def undo_page_deletion(
        self,
        path: Path,
        receipt: PageDeletionReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            self._validate_current_deletion_state(resolved_path, receipt)
            self._validate_delete_undo_snapshot(
                resolved_path,
                receipt,
            )
            candidate_path = self._create_candidate_path(resolved_path)
            self._copy_delete_undo_snapshot(receipt.undo_snapshot_path, candidate_path)
            self._validate_undo_page_deletion_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            transition = self._build_delete_undo_transition(receipt)
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.original_page_count,
                affected_pages=frozenset(),
                page_index_transition=transition,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return mutation_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def validate_deletion_redo_precondition(
        self,
        path: Path,
        receipt: PageDeletionReceipt,
    ) -> None:
        resolved_path = path.expanduser().resolve()
        current_snapshot = self._snapshot_document_structure(resolved_path)
        if current_snapshot != receipt.before_snapshot:
            raise PdfPageMutationError("削除対象ページの前提状態が変化しました")
        self._reject_unsupported_page_deletion_structures(
            resolved_path,
            receipt.deleted_page_indexes,
        )
        self._validate_delete_undo_snapshot(
            resolved_path,
            receipt,
        )

    def redo_page_deletion(
        self,
        path: Path,
        receipt: PageDeletionReceipt,
    ) -> WorkingCopyMutationResult:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            self.validate_deletion_redo_precondition(resolved_path, receipt)
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                writer = PdfWriter(clone_from=reader)
                for page_index in sorted(receipt.deleted_page_indexes, reverse=True):
                    writer.remove_page(page_index)
                candidate_path = self._create_candidate_path(resolved_path)
                self._write_candidate(writer, candidate_path)
            self._validate_redo_page_deletion_candidate(candidate_path, receipt)
            new_revision = self._build_revision_from_candidate(candidate_path, resolved_path)
            transition = self._build_delete_execute_transition(
                receipt.original_page_count,
                receipt.deleted_page_indexes,
            )
            mutation_result = WorkingCopyMutationResult(
                old_revision=old_revision,
                new_revision=new_revision,
                page_count=receipt.after_snapshot.page_count,
                affected_pages=frozenset(),
                page_index_transition=transition,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            return mutation_result
        except PdfPageMutationError as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        except Exception as exc:
            primary_error = exc
            raise PdfPageMutationError("作業コピーPDFの更新に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def discard_page_deletion_receipt(
        self,
        working_copy_path: Path,
        receipt: PageDeletionReceipt,
    ) -> None:
        snapshot_path = self._validate_delete_undo_snapshot_ownership(
            working_copy_path,
            receipt,
            require_exists=False,
        )
        if not snapshot_path.exists():
            return
        try:
            snapshot_path.unlink()
        except OSError as exc:
            raise PdfPageMutationError("削除前スナップショットPDFの削除に失敗しました") from exc

    @staticmethod
    def _validate_page_indexes(page_indexes: tuple[int, ...], page_count: int) -> tuple[int, ...]:
        validated = tuple(page_indexes)
        if not validated:
            raise ValueError("page_indexes must not be empty")
        for page_index in validated:
            if isinstance(page_index, bool) or not isinstance(page_index, Integral):
                raise TypeError("page indexes must be integers")
            if page_index < 0:
                raise ValueError("page indexes must be non-negative")
            if page_index >= page_count:
                raise ValueError("page index is out of range")
        return validated

    def _normalize_duplicate_page_indexes(
        self,
        page_indexes: tuple[int, ...],
        page_count: int,
    ) -> tuple[int, ...]:
        validated_indexes = self._validate_page_indexes(page_indexes, page_count)
        return tuple(sorted(set(int(page_index) for page_index in validated_indexes)))

    def _normalize_delete_page_indexes(
        self,
        page_indexes: tuple[int, ...],
        page_count: int,
    ) -> tuple[int, ...]:
        validated_indexes = self._validate_page_indexes(page_indexes, page_count)
        normalized = tuple(sorted(set(int(page_index) for page_index in validated_indexes)))
        if len(normalized) >= page_count:
            raise PdfPageMutationError("少なくとも1ページは残す必要があります")
        return normalized

    @staticmethod
    def _survivor_original_indexes(
        original_page_count: int,
        deleted_page_indexes: tuple[int, ...],
    ) -> tuple[int, ...]:
        deleted_set = set(deleted_page_indexes)
        return tuple(index for index in range(original_page_count) if index not in deleted_set)

    @staticmethod
    def _validate_delete_current_page_index(
        current_page_index: object,
        *,
        page_count: int,
    ) -> int:
        if isinstance(current_page_index, bool) or not isinstance(current_page_index, Integral):
            raise TypeError("current_page_index must be an integer")
        validated_current_page_index = int(current_page_index)
        if not 0 <= validated_current_page_index < page_count:
            raise ValueError("current_page_index must stay within the page range")
        return validated_current_page_index

    def _rotation_state_for_page(self, page_object: Any, page_index: int) -> PageRotationState:
        rotate_key = NameObject("/Rotate")
        if rotate_key in page_object:
            direct_rotate = page_object[rotate_key]
            direct_rotate_value = self._parse_raw_rotation(
                direct_rotate,
                page_index=page_index,
                label="direct",
            )
            return PageRotationState(
                page_index=page_index,
                direct_rotate_present=True,
                direct_rotate_value=direct_rotate_value,
                effective_rotation=self._normalize_effective_rotation(
                    direct_rotate_value,
                    page_index=page_index,
                    label="direct",
                ),
            )
        inherited_rotate = self._resolve_inherited_rotation(page_object, page_index=page_index)
        return PageRotationState(
            page_index=page_index,
            direct_rotate_present=False,
            direct_rotate_value=None,
            effective_rotation=inherited_rotate,
        )

    def _resolve_inherited_rotation(self, page_object: Any, *, page_index: int) -> int:
        current: Any = page_object
        visited: set[tuple[int, int]] = set()
        while current is not None:
            parent_ref = current.get("/Parent", None)
            if parent_ref is None:
                return 0
            parent = self._dereference(parent_ref)
            objgen = getattr(parent, "objgen", None)
            if objgen in visited:
                raise PdfPageRotationValidationError("ページツリーの回転継承を解決できません")
            if objgen is not None:
                visited.add(objgen)
            rotate_key = NameObject("/Rotate")
            if rotate_key in parent:
                rotate = parent[rotate_key]
                raw_rotation = self._parse_raw_rotation(
                    rotate,
                    page_index=page_index,
                    label="inherited",
                )
                return self._normalize_effective_rotation(
                    raw_rotation,
                    page_index=page_index,
                    label="inherited",
                )
            current = parent
        return 0

    def _collect_page_objects(self, pages_node: Any) -> list[Any]:
        page_objects: list[Any] = []
        self._append_page_objects(pages_node, page_objects)
        return page_objects

    def _append_page_objects(self, node: Any, page_objects: list[Any]) -> None:
        node_type = str(node.get(NameObject("/Type"), ""))
        if node_type == "/Page":
            page_objects.append(node)
            return
        for kid in node.get(NameObject("/Kids"), []):
            self._append_page_objects(self._dereference(kid), page_objects)

    def _parse_raw_rotation(
        self,
        value: object,
        *,
        page_index: int,
        label: str,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise PdfPageRotationValidationError(
                f"{page_index + 1}ページ目の{label}回転値が不正です"
            )
        raw_rotation = int(value)
        if raw_rotation % 90 != 0:
            raise PdfPageRotationValidationError(
                f"{page_index + 1}ページ目の{label}回転値は90度単位である必要があります"
            )
        return int(raw_rotation)

    def _normalize_effective_rotation(
        self,
        raw_rotation: int,
        *,
        page_index: int,
        label: str,
    ) -> int:
        normalized = raw_rotation % 360
        if normalized not in {0, 90, 180, 270}:
            raise PdfPageRotationValidationError(
                f"{page_index + 1}ページ目の{label}回転値が不正です"
            )
        return int(normalized)

    def _validate_states(self, states: tuple[PageRotationState, ...]) -> None:
        for state in states:
            if state.page_index < 0:
                raise ValueError("page indexes must be non-negative")
            if state.effective_rotation not in {0, 90, 180, 270}:
                raise PdfPageRotationValidationError("回転値が不正です")
            if state.direct_rotate_present:
                if state.direct_rotate_value is None:
                    raise PdfPageRotationValidationError("回転値が不正です")
                self._normalize_effective_rotation(
                    state.direct_rotate_value,
                    page_index=state.page_index,
                    label="direct",
                )

    def _crop_state_for_page_object(self, page_object: Any, page_index: int) -> CropPageState:
        direct_crop_value = page_object.get("/CropBox", None)
        inherited_crop_value = (
            None
            if direct_crop_value is not None
            else self._resolve_inherited_value(page_object, "/CropBox")
        )
        effective_media_box = self._normalized_effective_box(
            self._require_effective_box(page_object, "/MediaBox", None),
            label="MediaBox",
            page_index=page_index,
        )
        effective_crop_source = direct_crop_value
        if effective_crop_source is None:
            effective_crop_source = inherited_crop_value
        if effective_crop_source is None:
            effective_crop_source = effective_media_box
        effective_crop_box = self._normalized_effective_box(
            effective_crop_source,
            label="CropBox",
            page_index=page_index,
        )
        self._ensure_box_within_media_box(
            crop_box=effective_crop_box,
            media_box=effective_media_box,
            page_index=page_index,
        )
        return CropPageState(
            page_index=page_index,
            direct_crop_box_present=direct_crop_value is not None,
            direct_crop_box_value=(
                None
                if direct_crop_value is None
                else self._raw_box_tuple(direct_crop_value, label="CropBox", page_index=page_index)
            ),
            effective_crop_box=effective_crop_box,
            effective_media_box=effective_media_box,
            effective_rotation=self._page_effective_rotation(page_object, page_index),
            crop_box_inherited=direct_crop_value is None and inherited_crop_value is not None,
            crop_box_falls_back_to_media_box=(
                direct_crop_value is None and inherited_crop_value is None
            ),
        )

    def _page_effective_rotation(self, page_object: Any, page_index: int) -> int:
        direct_rotate = page_object.get("/Rotate", None)
        inherited_rotate = (
            None
            if direct_rotate is not None
            else self._resolve_inherited_value(page_object, "/Rotate")
        )
        raw_rotation: object
        if direct_rotate is None and inherited_rotate is None:
            raw_rotation = 0
        elif direct_rotate is not None:
            raw_rotation = direct_rotate
        else:
            raw_rotation = inherited_rotate
        return self._normalize_effective_rotation(
            cast(int, raw_rotation),
            page_index=page_index,
            label="effective",
        )

    def _require_effective_box(self, page_object: Any, key: str, fallback: object | None) -> object:
        direct_value = page_object.get(key, None)
        if direct_value is not None:
            return direct_value
        inherited_value = self._resolve_inherited_value(page_object, key)
        if inherited_value is not None:
            return inherited_value
        if key == "/CropBox" and fallback is not None:
            return fallback
        raise PdfPageMutationError("ページボックスが不正です")

    def _raw_box_tuple(
        self,
        value: object,
        *,
        label: str,
        page_index: int,
    ) -> tuple[float, float, float, float]:
        if not isinstance(value, (pikepdf.Array, list, tuple)):
            raise PdfPageMutationError(f"{page_index + 1}ページ目の{label}が不正です")
        values = tuple(value)
        if len(values) != 4:
            raise PdfPageMutationError(f"{page_index + 1}ページ目の{label}が不正です")
        normalized_values: list[float] = []
        for raw_value in values:
            if isinstance(raw_value, bool) or not isinstance(raw_value, (Integral, float, Decimal)):
                raise PdfPageMutationError(f"{page_index + 1}ページ目の{label}が不正です")
            numeric_value = float(raw_value)
            if not numeric_value == numeric_value or numeric_value in {float("inf"), float("-inf")}:
                raise PdfPageMutationError(f"{page_index + 1}ページ目の{label}が不正です")
            normalized_values.append(numeric_value)
        return tuple(normalized_values)  # type: ignore[return-value]

    def _normalized_effective_box(
        self,
        value: object,
        *,
        label: str,
        page_index: int,
    ) -> tuple[float, float, float, float]:
        raw_box = self._raw_box_tuple(value, label=label, page_index=page_index)
        try:
            return PdfRect.normalized(raw_box).as_tuple()
        except ValueError as exc:
            raise PdfPageMutationError(f"{page_index + 1}ページ目の{label}が不正です") from exc

    def _crop_box_array_object(
        self,
        box: tuple[float, float, float, float],
    ) -> ArrayObject:
        return ArrayObject([FloatObject(float(value)) for value in box])

    def _ensure_box_within_media_box(
        self,
        *,
        crop_box: tuple[float, float, float, float],
        media_box: tuple[float, float, float, float],
        page_index: int,
    ) -> None:
        crop_rect = PdfRect.from_tuple(crop_box)
        media_rect = PdfRect.from_tuple(media_box)
        if (
            crop_rect.left < media_rect.left
            or crop_rect.bottom < media_rect.bottom
            or crop_rect.right > media_rect.right
            or crop_rect.top > media_rect.top
        ):
            raise PdfPageMutationError(
                f"{page_index + 1}ページ目のeffective CropBoxがeffective MediaBox外です"
            )

    def _page_box_state(self, page: pikepdf.Page) -> PageBoxState:
        return self._page_box_state_from_objects(page, page.obj)

    def _page_box_state_from_objects(
        self,
        page: pikepdf.Page,
        exact_page_object: Any,
    ) -> PageBoxState:
        direct_media_box = page.obj.get("/MediaBox", None)
        direct_crop_box = exact_page_object.get("/CropBox", None)
        inherited_crop_box = (
            None
            if direct_crop_box is not None
            else self._resolve_inherited_value(exact_page_object, "/CropBox")
        )
        effective_crop_box = page.obj.get("/CropBox", None) or inherited_crop_box or page.cropbox
        return PageBoxState(
            media_box_direct_present=direct_media_box is not None,
            media_box=self._normalize_box(page.mediabox),
            crop_box=self._normalize_effective_snapshot_box(effective_crop_box),
            crop_box_direct_present=direct_crop_box is not None,
            crop_box_direct_value=(
                None
                if direct_crop_box is None
                else _require_snapshot_raw_box(
                    direct_crop_box,
                    label="crop_box_direct_value",
                )
            ),
            crop_box_inherited=inherited_crop_box is not None,
            crop_box_falls_back_to_media_box=(
                direct_crop_box is None and inherited_crop_box is None
            ),
            trim_box=self._optional_box(page.obj.get("/TrimBox", None)),
            bleed_box=self._optional_box(page.obj.get("/BleedBox", None)),
            art_box=self._optional_box(page.obj.get("/ArtBox", None)),
        )

    @staticmethod
    def _normalize_box(box: Any) -> tuple[float, float, float, float]:
        values = tuple(float(value) for value in box)
        if len(values) != 4:
            raise PdfPageMutationError("ページボックスが不正です")
        return values

    @staticmethod
    def _normalize_effective_snapshot_box(box: object) -> tuple[float, float, float, float]:
        try:
            return PdfRect.normalized(
                _require_snapshot_raw_box(box, label="crop_box"),
            ).as_tuple()
        except ValueError as exc:
            raise PdfPageMutationError("ページボックスが不正です") from exc

    def _optional_box(self, value: object) -> tuple[float, float, float, float] | None:
        if value is None:
            return None
        return self._normalize_box(value)

    def _resolve_inherited_value(self, page_object: Any, key: str) -> object | None:
        current: Any = page_object
        visited: set[tuple[int, int]] = set()
        key_object = NameObject(key)
        while current is not None:
            parent_ref = current.get("/Parent", None)
            if parent_ref is None:
                return None
            parent = self._dereference(parent_ref)
            objgen = getattr(parent, "objgen", None)
            if objgen in visited:
                raise PdfPageMutationError("ページツリー継承を解決できません")
            if objgen is not None:
                visited.add(objgen)
            if key_object in parent:
                return cast(object, parent[key_object])
            current = parent
        return None

    def _create_candidate_path(self, target_path: Path) -> Path:
        prefix = f".{target_path.stem}.mutation."
        file_descriptor, candidate_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=prefix,
            suffix=".tmp.pdf",
        )
        os.close(file_descriptor)
        return Path(candidate_name)

    def _create_delete_undo_snapshot(
        self,
        target_path: Path,
        *,
        expected_page_count: int,
    ) -> tuple[Path, str]:
        prefix = f".{target_path.stem}.delete-undo."
        file_descriptor, snapshot_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=prefix,
            suffix=".pdf",
        )
        os.close(file_descriptor)
        snapshot_path = Path(snapshot_name)
        try:
            with suppress(FileNotFoundError):
                os.unlink(snapshot_path)
            try:
                os.link(target_path, snapshot_path)
                self._fsync_delete_undo_snapshot(snapshot_path)
            except OSError:
                self._copy_delete_undo_snapshot(target_path, snapshot_path)
            snapshot_sha256 = self._sha256_file(snapshot_path)
            self._validate_delete_undo_snapshot_path(
                snapshot_path,
                expected_page_count=expected_page_count,
            )
            return snapshot_path, snapshot_sha256
        except Exception:
            self._cleanup_delete_undo_snapshot(snapshot_path, primary_error=None)
            raise

    def _create_insert_source_snapshot_path(self, target_path: Path) -> Path:
        prefix = f".{target_path.stem}.insert-source."
        file_descriptor, snapshot_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=prefix,
            suffix=".pdf",
        )
        os.close(file_descriptor)
        return Path(snapshot_name)

    def _create_insert_undo_snapshot(
        self,
        target_path: Path,
        *,
        expected_page_count: int,
        render_page_indexes: tuple[int, ...],
    ) -> Path:
        snapshot_path = self._create_named_snapshot_path(target_path, label="insert-undo")
        try:
            self._copy_named_snapshot(
                target_path,
                snapshot_path,
                create_error="挿入前スナップショットPDFの作成に失敗しました",
                fsync_error="挿入前スナップショットPDFの同期に失敗しました",
            )
            self._validate_insert_target_undo_snapshot_path(
                snapshot_path,
                expected_page_count=expected_page_count,
                render_page_indexes=render_page_indexes,
            )
            return snapshot_path
        except Exception:
            self._cleanup_insert_snapshot(snapshot_path, primary_error=None)
            raise

    def _create_replace_source_snapshot_path(self, target_path: Path) -> Path:
        prefix = f".{target_path.stem}.replace-source."
        file_descriptor, snapshot_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=prefix,
            suffix=".pdf",
        )
        os.close(file_descriptor)
        return Path(snapshot_name)

    def _create_replace_undo_snapshot(
        self,
        target_path: Path,
        *,
        expected_page_count: int,
        render_page_indexes: tuple[int, ...],
    ) -> Path:
        snapshot_path = self._create_named_snapshot_path(target_path, label="replace-undo")
        try:
            self._copy_named_snapshot(
                target_path,
                snapshot_path,
                create_error="置換前スナップショットPDFの作成に失敗しました",
                fsync_error="置換前スナップショットPDFの同期に失敗しました",
            )
            self._validate_replace_target_undo_snapshot_path(
                snapshot_path,
                expected_page_count=expected_page_count,
                render_page_indexes=render_page_indexes,
            )
            return snapshot_path
        except Exception:
            self._cleanup_insert_snapshot(snapshot_path, primary_error=None)
            raise

    def _create_named_snapshot_path(self, target_path: Path, *, label: str) -> Path:
        prefix = f".{target_path.stem}.{label}."
        file_descriptor, snapshot_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=prefix,
            suffix=".pdf",
        )
        os.close(file_descriptor)
        return Path(snapshot_name)

    def _copy_named_snapshot(
        self,
        source_path: Path,
        snapshot_path: Path,
        *,
        create_error: str,
        fsync_error: str,
    ) -> None:
        try:
            with source_path.open("rb") as input_stream, snapshot_path.open("wb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
        except OSError as exc:
            raise PdfPageMutationError(create_error) from exc
        except Exception as exc:
            raise PdfPageMutationError(create_error) from exc
        try:
            self._fsync_named_snapshot(snapshot_path, message=fsync_error)
        except PdfPageMutationError:
            raise

    def _fsync_named_snapshot(self, path: Path, *, message: str) -> None:
        try:
            with path.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PdfPageMutationError(message) from exc

    def _source_snapshot_fingerprint(
        self,
        path: Path,
        *,
        operation_label: str = "挿入元",
    ) -> FileFingerprint:
        try:
            return FileFingerprint.from_path(path)
        except OSError as exc:
            raise PdfPageMutationError(f"{operation_label}PDFの状態を確認できませんでした") from exc

    def read_source_pdf_revision(self, path: Path) -> SourcePdfRevision:
        resolved_path = path.expanduser().resolve()
        fingerprint = self._source_snapshot_fingerprint(resolved_path, operation_label="置換元")
        sha = self._sha256_file(resolved_path)
        page_count = self.read_page_count(resolved_path)
        return SourcePdfRevision(
            resolved_path=resolved_path,
            fingerprint=fingerprint,
            sha256=sha,
            page_count=page_count,
        )

    def _validate_expected_source_revision(
        self,
        path: Path,
        expected_revision: SourcePdfRevision,
        *,
        operation_label: str,
    ) -> None:
        resolved_path = path.expanduser().resolve()
        if resolved_path != expected_revision.resolved_path:
            raise PdfPageMutationError(f"{operation_label}PDFのパスが変化しました")
        current_fingerprint = self._source_snapshot_fingerprint(
            resolved_path,
            operation_label=operation_label,
        )
        if current_fingerprint != expected_revision.fingerprint:
            raise PdfPageMutationError(f"{operation_label}PDFが変更されました")
        current_sha = self._sha256_file(resolved_path)
        if current_sha != expected_revision.sha256:
            raise PdfPageMutationError(f"{operation_label}PDFが変更されました")
        current_page_count = self.read_page_count(resolved_path)
        if current_page_count != expected_revision.page_count:
            raise PdfPageMutationError(f"{operation_label}PDFのページ数が変化しました")

    def _copy_delete_undo_snapshot(self, source_path: Path, snapshot_path: Path) -> None:
        try:
            with source_path.open("rb") as input_stream, snapshot_path.open("wb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream)
            self._fsync_delete_undo_snapshot(snapshot_path)
        except OSError as exc:
            raise PdfPageMutationError("削除前スナップショットPDFの作成に失敗しました") from exc
        except Exception as exc:
            raise PdfPageMutationError("削除前スナップショットPDFの作成に失敗しました") from exc

    def _fsync_delete_undo_snapshot(self, path: Path) -> None:
        try:
            with path.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PdfPageMutationError("削除前スナップショットPDFの同期に失敗しました") from exc

    def _validate_delete_undo_snapshot_path(
        self,
        path: Path,
        *,
        expected_page_count: int,
    ) -> None:
        try:
            self._validate_basic_candidate(path, expected_page_count=expected_page_count)
            self._render_pages(path, tuple(range(expected_page_count)))
        except PdfPageMutationError as exc:
            raise PdfPageMutationError("削除前スナップショットPDFの検証に失敗しました") from exc

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _write_candidate(self, writer: PdfWriter, candidate_path: Path) -> None:
        try:
            with candidate_path.open("wb") as output_stream:
                writer.write(output_stream)
            self._fsync_file(candidate_path)
        except OSError as exc:
            raise PdfPageMutationError("更新候補PDFの書き込みに失敗しました") from exc
        except Exception as exc:
            raise PdfPageMutationError("更新候補PDFの書き込みに失敗しました") from exc

    def _write_pikepdf_candidate(self, pdf: pikepdf.Pdf, candidate_path: Path) -> None:
        try:
            pdf.save(candidate_path)
            self._fsync_file(candidate_path)
        except OSError as exc:
            raise PdfPageMutationError("更新候補PDFの書き込みに失敗しました") from exc
        except Exception as exc:
            raise PdfPageMutationError("更新候補PDFの書き込みに失敗しました") from exc

    def _fsync_file(self, path: Path) -> None:
        try:
            with path.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PdfPageMutationError("更新候補PDFの同期に失敗しました") from exc

    def _validate_rotation_candidate(
        self,
        path: Path,
        *,
        expected_page_count: int,
        expected_states: tuple[PageRotationState, ...],
        original_rotation_snapshot: tuple[PageRotationState, ...],
        original_box_snapshot: tuple[PageBoxState, ...],
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=expected_page_count)
        with pikepdf.open(path) as pdf:
            pages = list(pdf.pages)
            current_rotation_snapshot = self.read_rotation_states(path, tuple(range(len(pages))))
            if len(current_rotation_snapshot) != len(original_rotation_snapshot):
                raise PdfPageMutationError("更新後のページ数検証に失敗しました")
            expected_state_map = {state.page_index: state for state in expected_states}
            for page_index, current_state in enumerate(current_rotation_snapshot):
                if page_index in expected_state_map:
                    if current_state != expected_state_map[page_index]:
                        raise PdfPageMutationError("更新後のページ回転検証に失敗しました")
                elif current_state != original_rotation_snapshot[page_index]:
                    raise PdfPageMutationError("非対象ページの回転状態が変化しました")
            current_box_snapshot = tuple(self._page_box_state(page) for page in pages)
            if current_box_snapshot != original_box_snapshot:
                raise PdfPageMutationError("ページボックスの検証に失敗しました")
        self._render_pages(path, tuple(state.page_index for state in expected_states))

    def _validate_page_crop_candidate(
        self,
        path: Path,
        *,
        before_snapshot: PdfDocumentStructureSnapshot,
        original_crop_states: tuple[CropPageState, ...],
        target_crop_states: tuple[PageCropTarget, ...],
    ) -> PdfDocumentStructureSnapshot:
        self._validate_basic_candidate(path, expected_page_count=before_snapshot.page_count)
        after_snapshot = self._snapshot_document_structure(path)
        self._validate_document_level_snapshots(
            after_snapshot,
            before_snapshot,
            page_mapping=tuple(range(before_snapshot.page_count)),
        )
        _validate_crop_snapshot_transition(
            before_snapshot,
            after_snapshot,
            tuple(state.page_index for state in original_crop_states),
            original_crop_states,
            target_crop_states,
        )
        target_lookup = {target.page_index: target for target in target_crop_states}
        original_lookup = {state.page_index: state for state in original_crop_states}
        for page_index, current_page in enumerate(after_snapshot.pages):
            target = target_lookup.get(page_index)
            if target is None:
                if current_page != before_snapshot.pages[page_index]:
                    raise PdfPageMutationError("非対象ページの構造が変化しました")
                continue
            original_state = original_lookup[page_index]
            expected_page = self._expected_cropped_page_snapshot(
                before_snapshot.pages[page_index],
                target_box=target.crop_box,
            )
            if current_page != expected_page:
                raise PdfPageMutationError("更新後のCropBox検証に失敗しました")
            if before_snapshot.pages[page_index].boxes.media_box != current_page.boxes.media_box:
                raise PdfPageMutationError("MediaBoxの検証に失敗しました")
            if (
                before_snapshot.pages[page_index].effective_rotation
                != current_page.effective_rotation
            ):
                raise PdfPageMutationError("回転状態の検証に失敗しました")
            if (
                before_snapshot.pages[page_index].content_fingerprint
                != current_page.content_fingerprint
            ):
                raise PdfPageMutationError("Contents fingerprintの検証に失敗しました")
            if (
                before_snapshot.pages[page_index].resources_fingerprint
                != current_page.resources_fingerprint
            ):
                raise PdfPageMutationError("Resources fingerprintの検証に失敗しました")
            if before_snapshot.pages[page_index].annotations != current_page.annotations:
                raise PdfPageMutationError("annotationの検証に失敗しました")
            if current_page.boxes.crop_box != target.crop_box:
                raise PdfPageMutationError("CropBoxの検証に失敗しました")
            self._validate_rendered_crop_dimensions(
                path,
                page_index=page_index,
                target_box=target.crop_box,
                rotation=original_state.effective_rotation,
            )
        self._render_pages(path, tuple(target_lookup))
        return after_snapshot

    def _validate_crop_receipt_ownership(
        self,
        path: Path,
        receipt: PageCropReceipt,
    ) -> None:
        if path != receipt.working_copy_path:
            raise PdfPageMutationError("このトリミング操作証跡は別の作業コピー用です")

    def _expected_cropped_page_snapshot(
        self,
        page: PdfPageStructureSnapshot,
        *,
        target_box: tuple[float, float, float, float],
    ) -> PdfPageStructureSnapshot:
        return PdfPageStructureSnapshot(
            content_fingerprint=page.content_fingerprint,
            boxes=PageBoxState(
                media_box_direct_present=page.boxes.media_box_direct_present,
                media_box=page.boxes.media_box,
                crop_box=target_box,
                crop_box_direct_present=True,
                crop_box_direct_value=target_box,
                crop_box_inherited=False,
                crop_box_falls_back_to_media_box=False,
                trim_box=page.boxes.trim_box,
                bleed_box=page.boxes.bleed_box,
                art_box=page.boxes.art_box,
            ),
            direct_resources_present=page.direct_resources_present,
            resources_fingerprint=page.resources_fingerprint,
            direct_rotate_present=page.direct_rotate_present,
            direct_rotate_value=page.direct_rotate_value,
            effective_rotation=page.effective_rotation,
            annotations=page.annotations,
            direct_page_keys=self._crop_direct_page_keys(page.direct_page_keys),
            extra_page_entries_fingerprint=page.extra_page_entries_fingerprint,
        )

    def _crop_direct_page_keys(self, keys: tuple[str, ...]) -> tuple[str, ...]:
        key_set = set(keys)
        key_set.add("/CropBox")
        return tuple(sorted(key_set))

    def _restore_inherited_crop_boxes_for_untouched_pages(
        self,
        page_objects: Sequence[dict[object, object]],
        *,
        reference_snapshot: PdfDocumentStructureSnapshot,
        changed_page_indexes: frozenset[int],
    ) -> None:
        for page_index, page_object in enumerate(page_objects):
            if page_index in changed_page_indexes:
                continue
            if reference_snapshot.pages[page_index].boxes.crop_box_direct_present:
                continue
            page_object.pop(NameObject("/CropBox"), None)

    def _validate_current_crop_state(
        self,
        path: Path,
        expected_snapshot: PdfDocumentStructureSnapshot,
    ) -> None:
        current_snapshot = self._snapshot_document_structure(path)
        if current_snapshot != expected_snapshot:
            raise PdfPageMutationError("トリミング対象ページの前提状態が変化しました")

    def _validate_current_crop_redo_state(
        self,
        path: Path,
        receipt: PageCropReceipt,
    ) -> None:
        current_snapshot = self._snapshot_document_structure(path)
        self._validate_document_level_snapshots(
            current_snapshot,
            receipt.before_snapshot,
            page_mapping=tuple(range(receipt.before_snapshot.page_count)),
        )
        current_states = self.read_crop_states(path, receipt.changed_page_indexes)
        if current_states != receipt.original_crop_states:
            raise PdfPageMutationError("トリミング対象ページの前提状態が変化しました")
        for page_index, page in enumerate(current_snapshot.pages):
            if page_index in receipt.changed_page_indexes:
                if not self._page_snapshot_matches_non_crop_structure(
                    page,
                    receipt.before_snapshot.pages[page_index],
                    expected_crop_box=receipt.original_crop_states[
                        receipt.changed_page_indexes.index(page_index)
                    ].effective_crop_box,
                ):
                    raise PdfPageMutationError("トリミング対象ページの前提状態が変化しました")
                continue
            if page != receipt.before_snapshot.pages[page_index]:
                raise PdfPageMutationError("トリミング対象ページの前提状態が変化しました")

    def _validate_undo_page_crop_candidate(
        self,
        path: Path,
        receipt: PageCropReceipt,
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=receipt.before_snapshot.page_count)
        snapshot = self._snapshot_document_structure(path)
        self._validate_document_level_snapshots(
            snapshot,
            receipt.before_snapshot,
            page_mapping=tuple(range(receipt.before_snapshot.page_count)),
        )
        current_states = self.read_crop_states(path, receipt.changed_page_indexes)
        if current_states != receipt.original_crop_states:
            raise PdfPageMutationError("トリミングの取り消し検証に失敗しました")
        for page_index, page in enumerate(snapshot.pages):
            if page_index in receipt.changed_page_indexes:
                if not self._page_snapshot_matches_non_crop_structure(
                    page,
                    receipt.before_snapshot.pages[page_index],
                    expected_crop_box=receipt.original_crop_states[
                        receipt.changed_page_indexes.index(page_index)
                    ].effective_crop_box,
                ):
                    raise PdfPageMutationError("トリミングの取り消し検証に失敗しました")
                continue
            if page != receipt.before_snapshot.pages[page_index]:
                raise PdfPageMutationError("トリミングの取り消し検証に失敗しました")
        self._render_pages(path, receipt.changed_page_indexes)

    def _validate_redo_page_crop_candidate(
        self,
        path: Path,
        receipt: PageCropReceipt,
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=receipt.after_snapshot.page_count)
        snapshot = self._snapshot_document_structure(path)
        if snapshot != receipt.after_snapshot:
            raise PdfPageMutationError("トリミングの再適用検証に失敗しました")
        self._render_pages(path, receipt.changed_page_indexes)

    def _page_snapshot_matches_non_crop_structure(
        self,
        current: PdfPageStructureSnapshot,
        expected: PdfPageStructureSnapshot,
        *,
        expected_crop_box: tuple[float, float, float, float],
    ) -> bool:
        return (
            current.content_fingerprint == expected.content_fingerprint
            and current.boxes.media_box_direct_present == expected.boxes.media_box_direct_present
            and current.boxes.media_box == expected.boxes.media_box
            and current.boxes.crop_box == expected_crop_box
            and current.boxes.trim_box == expected.boxes.trim_box
            and current.boxes.bleed_box == expected.boxes.bleed_box
            and current.boxes.art_box == expected.boxes.art_box
            and current.direct_resources_present == expected.direct_resources_present
            and current.resources_fingerprint == expected.resources_fingerprint
            and current.direct_rotate_present == expected.direct_rotate_present
            and current.direct_rotate_value == expected.direct_rotate_value
            and current.effective_rotation == expected.effective_rotation
            and current.annotations == expected.annotations
            and current.extra_page_entries_fingerprint == expected.extra_page_entries_fingerprint
        )

    def _validate_rendered_crop_dimensions(
        self,
        path: Path,
        *,
        page_index: int,
        target_box: tuple[float, float, float, float],
        rotation: int,
    ) -> None:
        document: Any | None = None
        try:
            document = pdfium.PdfDocument(str(path))
            page = document[page_index]
            try:
                width, height = page.get_size()
                target_rect = PdfRect.from_tuple(target_box)
                expected_width = target_rect.width if rotation in {0, 180} else target_rect.height
                expected_height = target_rect.height if rotation in {0, 180} else target_rect.width
                if abs(float(width) - expected_width) > 1.0:
                    raise PdfPageMutationError("更新後ページの表示幅検証に失敗しました")
                if abs(float(height) - expected_height) > 1.0:
                    raise PdfPageMutationError("更新後ページの表示高さ検証に失敗しました")
            finally:
                page.close()
        except PdfPageMutationError:
            raise
        except Exception as exc:
            raise PdfPageMutationError("更新後ページの描画検証に失敗しました") from exc
        finally:
            if document is not None:
                document.close()

    def _validate_page_duplication_candidate(
        self,
        path: Path,
        receipt: PageDuplicationReceipt,
    ) -> None:
        expected_page_count = receipt.original_page_count + len(receipt.source_page_indexes)
        self._validate_basic_candidate(path, expected_page_count=expected_page_count)
        after_snapshot = self._snapshot_document_structure(path)
        if after_snapshot.page_count != expected_page_count:
            raise PdfPageMutationError("更新後のページ数検証に失敗しました")
        self._validate_document_level_snapshots(
            after_snapshot,
            receipt.before_snapshot,
            page_mapping=self._build_execute_transition(
                receipt.original_page_count,
                receipt.source_page_indexes,
            ).cache_old_to_new,
        )
        self._validate_duplicate_page_layout(after_snapshot, receipt)
        self._validate_duplicate_page_independence(path, receipt)
        self._validate_duplicate_renders(path, receipt)

    def _validate_page_reordering_candidate(
        self,
        path: Path,
        *,
        before_snapshot: PdfDocumentStructureSnapshot,
        plan: PageReorderPlan,
    ) -> PdfDocumentStructureSnapshot:
        self._validate_basic_candidate(path, expected_page_count=before_snapshot.page_count)
        after_snapshot = self._snapshot_document_structure(path)
        if after_snapshot.page_count != before_snapshot.page_count:
            raise PdfPageMutationError("更新後のページ数検証に失敗しました")
        self._validate_document_level_snapshots(
            after_snapshot,
            before_snapshot,
            page_mapping=plan.old_to_new,
        )
        for new_page_index, original_page_index in enumerate(plan.target_order):
            if after_snapshot.pages[new_page_index] != before_snapshot.pages[original_page_index]:
                raise PdfPageMutationError("更新後のページ順序または構造の検証に失敗しました")
        self._render_pages(path, self._reorder_execute_render_page_indexes(plan))
        return after_snapshot

    def _validate_page_insertion_candidate(
        self,
        path: Path,
        *,
        source_snapshot_path: Path,
        target_before_snapshot: PdfDocumentStructureSnapshot,
        source_selected_page_snapshots: tuple[PdfPageStructureSnapshot, ...],
        plan: PageInsertionPlan,
    ) -> PdfDocumentStructureSnapshot:
        expected_page_count = target_before_snapshot.page_count + len(plan.source_page_indexes)
        self._validate_basic_candidate(path, expected_page_count=expected_page_count)
        after_snapshot = self._snapshot_document_structure(path)
        if after_snapshot.page_count != expected_page_count:
            raise PdfPageMutationError("更新後のページ数検証に失敗しました")
        transition = self._build_insert_execute_transition(plan)
        self._validate_document_level_snapshots(
            after_snapshot,
            target_before_snapshot,
            page_mapping=transition.cache_old_to_new,
        )
        for original_page_index, new_page_index in enumerate(plan.target_old_to_new):
            if (
                after_snapshot.pages[new_page_index]
                != target_before_snapshot.pages[original_page_index]
            ):
                raise PdfPageMutationError("既存ページの構造検証に失敗しました")
        for offset, expected_source_page in enumerate(source_selected_page_snapshots):
            imported_page = after_snapshot.pages[plan.inserted_page_indexes_after[offset]]
            expected_page = self._expected_imported_page_snapshot(expected_source_page)
            if imported_page != expected_page:
                raise PdfPageMutationError("挿入ページの構造検証に失敗しました")
        self._render_pages(path, self._page_insertion_render_page_indexes(plan))
        self._validate_inserted_page_render_equivalence(
            source_path=source_snapshot_path,
            source_page_indexes=plan.source_page_indexes,
            imported_path=path,
            imported_page_indexes=plan.inserted_page_indexes_after,
        )
        return after_snapshot

    def _validate_undo_duplication_candidate(
        self,
        path: Path,
        receipt: PageDuplicationReceipt,
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=receipt.original_page_count)
        after_snapshot = self._snapshot_document_structure(path)
        if after_snapshot != receipt.before_snapshot:
            raise PdfPageMutationError("複製の取り消し検証に失敗しました")
        self._render_pages(path, tuple(range(receipt.original_page_count)))

    def _validate_undo_page_reordering_candidate(
        self,
        path: Path,
        receipt: PageReorderReceipt,
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=receipt.original_page_count)
        snapshot = self._snapshot_document_structure(path)
        if snapshot != receipt.before_snapshot:
            raise PdfPageMutationError("ページ並べ替えの取り消し検証に失敗しました")
        self._render_pages(path, self._reorder_undo_render_page_indexes(receipt))

    def _validate_basic_candidate(
        self,
        path: Path,
        *,
        expected_page_count: int,
    ) -> None:
        try:
            self._validator.validate(str(path), expected_page_count=expected_page_count)
        except PdfDocumentValidationError as exc:
            raise PdfPageMutationError(str(exc)) from exc

    def _snapshot_document_structure(self, path: Path) -> PdfDocumentStructureSnapshot:
        resolved_path = path.expanduser().resolve()
        with pikepdf.open(resolved_path) as pdf:
            page_count = len(pdf.pages)
            rotations = self.read_rotation_states(resolved_path, tuple(range(page_count)))
            with resolved_path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                root = cast(Any, reader.trailer["/Root"])
                root_pages = cast(Any, root["/Pages"]).get_object()
                page_objects = self._collect_page_objects(root_pages)
            if len(page_objects) != page_count:
                raise PdfPageMutationError("ページ数の読み取り結果が一致しません")
            page_objgens = {
                page.obj.objgen
                for page in pdf.pages
                if self._has_indirect_objgen(getattr(page.obj, "objgen", None))
            }
            pages = tuple(
                self._page_structure_snapshot(
                    pdf.pages[index],
                    rotations[index],
                    page_objgens,
                    exact_page_object=page_objects[index],
                )
                for index in range(page_count)
            )
        metadata_fingerprint = self._metadata_fingerprint(resolved_path)
        outlines = self._outlines_snapshot(resolved_path, page_count=page_count)
        named_destinations = self._named_destination_snapshots(
            resolved_path,
            page_count=page_count,
        )
        attachments_fingerprint = self._attachments_fingerprint(resolved_path)
        return PdfDocumentStructureSnapshot(
            page_count=page_count,
            pages=pages,
            metadata_fingerprint=metadata_fingerprint,
            outlines=outlines,
            named_destinations=named_destinations,
            attachments_fingerprint=attachments_fingerprint,
        )

    def _page_structure_snapshot(
        self,
        page: pikepdf.Page,
        rotation_state: PageRotationState,
        page_objgens: set[tuple[int, int]],
        *,
        exact_page_object: Any,
    ) -> PdfPageStructureSnapshot:
        annotations = self._annotation_snapshots(
            page.obj.get("/Annots", None),
            owning_page=page.obj,
            page_objgens=page_objgens,
        )
        return PdfPageStructureSnapshot(
            content_fingerprint=self._contents_fingerprint(page.obj.get("/Contents", None)),
            boxes=self._page_box_state_from_objects(page, exact_page_object),
            direct_resources_present=exact_page_object.get("/Resources", None) is not None,
            resources_fingerprint=self._resources_fingerprint(page),
            direct_rotate_present=rotation_state.direct_rotate_present,
            direct_rotate_value=rotation_state.direct_rotate_value,
            effective_rotation=rotation_state.effective_rotation,
            annotations=annotations,
            direct_page_keys=tuple(sorted(str(key) for key in page.obj)),
            extra_page_entries_fingerprint=self._object_fingerprint(
                page.obj,
                exclude_keys=_PAGE_ENTRY_FINGERPRINT_EXCLUDED_KEYS,
            ),
        )

    def _annotation_snapshots(
        self,
        annots_object: object,
        *,
        owning_page: Any,
        page_objgens: set[tuple[int, int]],
    ) -> tuple[PdfAnnotationStructureSnapshot, ...]:
        if annots_object is None:
            return ()
        annots = self._dereference(annots_object)
        if not isinstance(annots, pikepdf.Array):
            raise PdfPageMutationError("注釈配列が不正です")
        snapshots: list[PdfAnnotationStructureSnapshot] = []
        for annot_ref in annots:
            annot = self._dereference(annot_ref)
            subtype = str(annot.get("/Subtype", ""))
            rect_object = annot.get("/Rect", None)
            if rect_object is None:
                raise PdfPageMutationError("注釈矩形が不正です")
            snapshots.append(
                PdfAnnotationStructureSnapshot(
                    subtype=subtype,
                    rect=self._normalize_box(rect_object),
                    has_appearance="/AP" in annot,
                    appearance_fingerprint=self._appearance_fingerprint(annot),
                    parent_state=self._annotation_parent_state(
                        annot,
                        owning_page=owning_page,
                        page_objgens=page_objgens,
                    ),
                    fingerprint=self._annotation_fingerprint(annot),
                )
            )
        return tuple(snapshots)

    def _annotation_parent_state(
        self,
        annot: Any,
        *,
        owning_page: Any,
        page_objgens: set[tuple[int, int]],
    ) -> AnnotationParentState:
        parent_object = annot.get("/P", None)
        if parent_object is None:
            return AnnotationParentState.ABSENT
        try:
            parent = self._dereference(parent_object)
        except Exception:
            return AnnotationParentState.INVALID
        parent_objgen = getattr(parent, "objgen", None)
        owning_page_objgen = getattr(owning_page, "objgen", None)
        if parent_objgen == owning_page_objgen:
            return AnnotationParentState.POINTS_TO_OWN_PAGE
        if (
            self._has_indirect_objgen(parent_objgen)
            and cast(tuple[int, int], parent_objgen) in page_objgens
            and str(parent.get("/Type", "")) == "/Page"
        ):
            return AnnotationParentState.POINTS_TO_OTHER_PAGE
        return AnnotationParentState.INVALID

    def _contents_fingerprint(self, contents: object) -> str:
        if contents is None:
            return "none"
        return self._object_fingerprint(contents)

    def _resources_fingerprint(self, page: pikepdf.Page) -> str:
        resources: object | None = page.obj.get("/Resources", None)
        if resources is None:
            resources = self._resolve_inherited_value(page.obj, "/Resources")
        if resources is None:
            return "none"
        normalized = self._normalize_resource_object(
            resources,
            memo={},
            active=set(),
        )
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def _normalize_resource_object(
        self,
        value: object,
        *,
        memo: dict[object, object],
        active: set[object],
    ) -> object:
        dereferenced = self._dereference(value)
        indirect_identity = self._indirect_normalization_identity(dereferenced)
        direct_cycle_identity = self._direct_cycle_identity(dereferenced)
        active_identity = (
            indirect_identity if indirect_identity is not None else direct_cycle_identity
        )
        if active_identity is not None:
            if active_identity in active:
                return {"cycle": True}
            if indirect_identity is not None and indirect_identity in memo:
                return memo[indirect_identity]
            active.add(active_identity)
        try:
            if isinstance(dereferenced, pikepdf.Stream):
                payload = {
                    str(key): self._normalize_resource_object(
                        item,
                        memo=memo,
                        active=active,
                    )
                    for key, item in sorted(dereferenced.items(), key=lambda entry: str(entry[0]))
                    if str(key) not in _VOLATILE_STREAM_KEYS
                }
                payload["__stream_data__"] = sha256(dereferenced.read_bytes()).hexdigest()
                normalized: object = payload
            elif isinstance(dereferenced, pikepdf.Dictionary):
                normalized = {
                    str(key): self._normalize_resource_object(
                        item,
                        memo=memo,
                        active=active,
                    )
                    for key, item in sorted(dereferenced.items(), key=lambda entry: str(entry[0]))
                }
            elif isinstance(dereferenced, pikepdf.Array):
                normalized = [
                    self._normalize_resource_object(
                        item,
                        memo=memo,
                        active=active,
                    )
                    for item in dereferenced
                ]
            elif isinstance(dereferenced, pikepdf.Name):
                normalized = {"__name__": str(dereferenced)}
            elif isinstance(dereferenced, pikepdf.String):
                normalized = {"__string__": str(dereferenced)}
            elif isinstance(dereferenced, bytes):
                normalized = {"__bytes__": sha256(dereferenced).hexdigest()}
            else:
                primitive = self._normalize_primitive_value(dereferenced)
                normalized = (
                    primitive if primitive is not _NORMALIZE_OBJECT_UNHANDLED else str(dereferenced)
                )
            if indirect_identity is not None:
                memo[indirect_identity] = normalized
            return normalized
        finally:
            if active_identity is not None:
                active.discard(active_identity)

    def _appearance_fingerprint(self, annot: Any) -> str | None:
        appearance = annot.get("/AP", None)
        if appearance is None:
            return None
        return self._object_fingerprint(appearance)

    def _annotation_fingerprint(self, annot: Any) -> str:
        dereferenced = self._dereference(annot)
        if not isinstance(dereferenced, pikepdf.Dictionary):
            raise PdfPageMutationError("注釈構造が不正です")
        normalized = {
            str(key): self._normalize_object(
                item,
                exclude_keys=frozenset(),
                seen={},
                active=set(),
                next_local_id=[1],
            )
            for key, item in sorted(
                ((str(key), item) for key, item in dereferenced.items() if str(key) != "/P"),
                key=lambda entry: entry[0],
            )
        }
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def _object_fingerprint(
        self,
        value: object,
        *,
        exclude_keys: frozenset[str] = frozenset(),
    ) -> str:
        normalized = self._normalize_object(
            value,
            exclude_keys=exclude_keys,
            seen={},
            active=set(),
            next_local_id=[1],
        )
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def _normalize_object(
        self,
        value: object,
        *,
        exclude_keys: frozenset[str],
        seen: dict[object, int],
        active: set[object],
        next_local_id: list[int],
    ) -> object:
        dereferenced = self._dereference(value)
        indirect_identity = self._indirect_normalization_identity(dereferenced)
        direct_cycle_identity = self._direct_cycle_identity(dereferenced)
        active_identity = (
            indirect_identity if indirect_identity is not None else direct_cycle_identity
        )
        if active_identity is not None:
            if active_identity in active:
                if indirect_identity is not None:
                    return {"cycle": seen[indirect_identity]}
                return {"cycle": True}
            if indirect_identity is not None:
                if indirect_identity in seen:
                    return {"ref": seen[indirect_identity]}
                seen[indirect_identity] = next_local_id[0]
                next_local_id[0] += 1
            active.add(active_identity)
        try:
            if isinstance(dereferenced, pikepdf.Stream):
                stream_exclude_keys = exclude_keys | _VOLATILE_STREAM_KEYS
                payload = {
                    key: self._normalize_object(
                        item,
                        exclude_keys=stream_exclude_keys,
                        seen=seen,
                        active=active,
                        next_local_id=next_local_id,
                    )
                    for key, item in sorted(
                        (
                            (str(key), item)
                            for key, item in dereferenced.items()
                            if str(key) not in stream_exclude_keys
                        ),
                        key=lambda entry: entry[0],
                    )
                }
                payload["__stream_data__"] = sha256(dereferenced.read_bytes()).hexdigest()
                return payload
            if isinstance(dereferenced, pikepdf.Dictionary):
                normalized_dict: dict[str, object] = {}
                for key, item in sorted(
                    (
                        (str(key), item)
                        for key, item in dereferenced.items()
                        if str(key) not in exclude_keys
                    ),
                    key=lambda entry: entry[0],
                ):
                    item_value = self._dereference(item)
                    if (
                        getattr(dereferenced, "is_indirect", False) is False
                        and getattr(item_value, "is_indirect", False) is False
                        and item_value == dereferenced
                    ):
                        normalized_dict[key] = {"cycle": True}
                        continue
                    normalized_dict[key] = self._normalize_object(
                        item,
                        exclude_keys=exclude_keys,
                        seen=seen,
                        active=active,
                        next_local_id=next_local_id,
                    )
                return normalized_dict
            if isinstance(dereferenced, pikepdf.Array):
                return [
                    self._normalize_object(
                        item,
                        exclude_keys=exclude_keys,
                        seen=seen,
                        active=active,
                        next_local_id=next_local_id,
                    )
                    for item in dereferenced
                ]
            if isinstance(dereferenced, bytes):
                return {"__bytes__": sha256(dereferenced).hexdigest()}
            primitive = self._normalize_primitive_value(dereferenced)
            if primitive is not _NORMALIZE_OBJECT_UNHANDLED:
                return primitive
            return str(dereferenced)
        finally:
            if active_identity is not None:
                active.discard(active_identity)

    def _normalize_primitive_value(self, value: object) -> object:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, Integral):
            return int(value)
        if isinstance(value, Decimal):
            return self._normalize_decimal_value(value)
        if isinstance(value, float):
            return self._normalize_decimal_value(Decimal(str(value)))
        if isinstance(value, str):
            return value
        return _NORMALIZE_OBJECT_UNHANDLED

    def _normalize_decimal_value(self, value: Decimal) -> object:
        normalized = value.normalize()
        if normalized == normalized.to_integral():
            return int(normalized)
        rendered = format(normalized, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        if rendered in {"", "-0"}:
            rendered = "0"
        return {"__number__": rendered}

    def _indirect_normalization_identity(self, value: object) -> object | None:
        if getattr(value, "is_indirect", False) is not True:
            return None
        objgen = getattr(value, "objgen", None)
        if self._has_indirect_objgen(objgen):
            typed_objgen = cast(tuple[int, int], objgen)
            return ("indirect", typed_objgen[0], typed_objgen[1])
        return None

    def _direct_cycle_identity(self, value: object) -> object | None:
        if isinstance(value, (pikepdf.Stream, pikepdf.Dictionary, pikepdf.Array)):
            return ("direct", id(value))
        return None

    def _metadata_fingerprint(self, path: Path) -> str:
        try:
            with path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                metadata = reader.metadata
                payload: dict[str, object] = {}
                if metadata is not None:
                    payload["info"] = {str(key): str(value) for key, value in metadata.items()}
                root = self._dereference(reader.trailer["/Root"])
                metadata_object = root.get("/Metadata", None)
                if metadata_object is not None:
                    resolved_metadata = self._dereference(metadata_object)
                    get_data = getattr(resolved_metadata, "get_data", None)
                    if not callable(get_data):
                        raise PdfPageMutationError("メタデータの解析に失敗しました")
                    payload["xmp_sha256"] = sha256(get_data()).hexdigest()
                return self._json_digest(payload)
        except PdfPageMutationError:
            raise
        except Exception as exc:
            raise PdfPageMutationError("メタデータの解析に失敗しました") from exc

    def _outlines_snapshot(
        self,
        path: Path,
        *,
        page_count: int,
    ) -> tuple[PdfOutlineItemSnapshot, ...]:
        try:
            with path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                root = self._dereference(reader.trailer["/Root"])
                if "/Outlines" not in root:
                    return ()
                outline = reader.outline
                if not isinstance(outline, list):
                    raise PdfPageMutationError("アウトラインの解析に失敗しました")
                return self._outline_items_snapshot(outline, reader, page_count=page_count)
        except PdfPageMutationError:
            raise
        except Exception as exc:
            raise PdfPageMutationError("アウトラインの解析に失敗しました") from exc

    def _outline_items_snapshot(
        self,
        items: Sequence[object],
        reader: PdfReader,
        *,
        page_count: int,
    ) -> tuple[PdfOutlineItemSnapshot, ...]:
        snapshots: list[PdfOutlineItemSnapshot] = []
        index = 0
        while index < len(items):
            item = items[index]
            if isinstance(item, list):
                raise PdfPageMutationError("アウトライン階層が不正です")
            children: tuple[PdfOutlineItemSnapshot, ...] = ()
            if index + 1 < len(items) and isinstance(items[index + 1], list):
                children = self._outline_items_snapshot(
                    cast(list[object], items[index + 1]),
                    reader,
                    page_count=page_count,
                )
                index += 1
            snapshots.append(
                self._outline_item_snapshot(
                    item,
                    reader,
                    children=children,
                    page_count=page_count,
                )
            )
            index += 1
        return tuple(snapshots)

    def _outline_item_snapshot(
        self,
        item: object,
        reader: PdfReader,
        *,
        children: tuple[PdfOutlineItemSnapshot, ...],
        page_count: int,
    ) -> PdfOutlineItemSnapshot:
        title = str(getattr(item, "title", ""))
        if not title:
            raise PdfPageMutationError("アウトラインの解析に失敗しました")
        action_type, requires_destination = self._outline_action_type(item)
        destination_page_index = self._outline_destination_page_index(
            item,
            reader,
            page_count=page_count,
            required=requires_destination,
        )
        return PdfOutlineItemSnapshot(
            title=title,
            destination_page_index=destination_page_index,
            action_type=action_type,
            children=children,
        )

    def _outline_action_type(self, item: object) -> tuple[str | None, bool]:
        get = getattr(item, "get", None)
        if callable(get):
            destination = get("/Dest", None)
            if destination is not None:
                return ("dest", True)
            action = get("/A", None)
            if action is not None:
                resolved_action = self._dereference(action)
                action_type = str(resolved_action.get("/S", ""))
                return (action_type or None, action_type == "/GoTo")
        return (None, False)

    def _outline_destination_page_index(
        self,
        item: object,
        reader: PdfReader,
        *,
        page_count: int,
        required: bool,
    ) -> int | None:
        try:
            page_index = reader.get_destination_page_number(item)  # type: ignore[arg-type]
        except Exception as exc:
            if required:
                raise PdfPageMutationError("アウトラインの解析に失敗しました") from exc
            return None
        if page_index is None:
            if required:
                raise PdfPageMutationError("アウトラインの解析に失敗しました")
            return None
        if page_index < 0 or page_index >= page_count:
            raise PdfPageMutationError("アウトラインの宛先ページが不正です")
        return int(page_index)

    def _named_destination_snapshots(
        self,
        path: Path,
        *,
        page_count: int,
    ) -> tuple[PdfNamedDestinationSnapshot, ...]:
        try:
            with path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                root = self._dereference(reader.trailer["/Root"])
                if "/Names" not in root and "/Dests" not in root:
                    return ()
                named_destinations = reader.named_destinations
                if not isinstance(named_destinations, dict):
                    raise PdfPageMutationError("名前付き宛先の解析に失敗しました")
                snapshots: list[PdfNamedDestinationSnapshot] = []
                for name, destination in sorted(
                    named_destinations.items(),
                    key=lambda item: str(item[0]),
                ):
                    page_index = reader.get_destination_page_number(destination)
                    if page_index is None:
                        raise PdfPageMutationError("名前付き宛先の解析に失敗しました")
                    if page_index < 0 or page_index >= page_count:
                        raise PdfPageMutationError("名前付き宛先のページが不正です")
                    snapshots.append(
                        PdfNamedDestinationSnapshot(
                            name=str(name),
                            destination_page_index=int(page_index),
                        )
                    )
                return tuple(snapshots)
        except PdfPageMutationError:
            raise
        except Exception as exc:
            raise PdfPageMutationError("名前付き宛先の解析に失敗しました") from exc

    def _attachments_fingerprint(self, path: Path) -> str:
        try:
            with path.open("rb") as source_stream:
                reader = PdfReader(source_stream)
                attachments = getattr(reader, "attachments", {})
                payload: dict[str, list[str]] = {}
                if isinstance(attachments, dict):
                    for name, values in attachments.items():
                        digests: list[str] = []
                        if isinstance(values, list):
                            for item in values:
                                digests.append(sha256(bytes(item)).hexdigest())
                        else:
                            digests.append(sha256(bytes(values)).hexdigest())
                        payload[str(name)] = digests
                return self._json_digest(payload)
        except Exception as exc:
            raise PdfPageMutationError("添付ファイルの解析に失敗しました") from exc

    @staticmethod
    def _json_digest(payload: object) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def _reject_unsupported_forms(self, path: Path, page_indexes: tuple[int, ...]) -> None:
        with pikepdf.open(path) as pdf:
            if "/AcroForm" in pdf.Root:
                raise PdfPageMutationError("フォームページの複製は未対応です")
            for page_index in page_indexes:
                annots_object = pdf.pages[page_index].obj.get("/Annots", None)
                if annots_object is None:
                    continue
                annots = self._dereference(annots_object)
                if not isinstance(annots, pikepdf.Array):
                    raise PdfPageMutationError("注釈配列が不正です")
                for annot_ref in annots:
                    annot = self._dereference(annot_ref)
                    if str(annot.get("/Subtype", "")) == "/Widget":
                        raise PdfPageMutationError("Widget注釈を含むページの複製は未対応です")

    def _reject_unsupported_page_deletion_structures(
        self,
        path: Path,
        deleted_page_indexes: tuple[int, ...],
    ) -> None:
        deleted_set = set(deleted_page_indexes)
        with pikepdf.open(path) as pdf:
            root = pdf.Root
            if "/AcroForm" in root:
                raise PdfPageMutationError("フォームを含むPDFの削除は未対応です")
            if "/StructTreeRoot" in root:
                raise PdfPageMutationError("タグ付きPDFの削除は未対応です")
            if "/PageLabels" in root:
                raise PdfPageMutationError("PageLabelsを含むPDFの削除は未対応です")
            if "/Threads" in root:
                raise PdfPageMutationError("Article Threadsを含むPDFの削除は未対応です")
            if "/OpenAction" in root:
                raise PdfPageMutationError("OpenActionを含むPDFの削除は未対応です")

            page_index_by_objgen = {
                page.obj.objgen: index
                for index, page in enumerate(pdf.pages)
                if self._has_indirect_objgen(getattr(page.obj, "objgen", None))
            }
            for page in pdf.pages:
                annots_object = page.obj.get("/Annots", None)
                if annots_object is None:
                    continue
                annots = self._dereference(annots_object)
                if not isinstance(annots, pikepdf.Array):
                    raise PdfPageMutationError("注釈配列が不正です")
                for annot_ref in annots:
                    annot = self._dereference(annot_ref)
                    if str(annot.get("/Subtype", "")) == "/Widget":
                        raise PdfPageMutationError("Widget注釈を含むPDFの削除は未対応です")
                    if "/Dest" in annot or self._annotation_has_internal_goto_action(annot):
                        raise PdfPageMutationError("内部宛先注釈を含むPDFの削除は未対応です")
                    parent_object = annot.get("/P", None)
                    if parent_object is None:
                        continue
                    parent = self._dereference(parent_object)
                    parent_objgen = getattr(parent, "objgen", None)
                    owning_objgen = getattr(page.obj, "objgen", None)
                    if parent_objgen == owning_objgen:
                        continue
                    if (
                        not self._has_indirect_objgen(parent_objgen)
                        or cast(tuple[int, int], parent_objgen) not in page_index_by_objgen
                        or str(parent.get("/Type", "")) != "/Page"
                    ):
                        raise PdfPageMutationError("注釈の/P参照を解決できません")
                    parent_page_index = page_index_by_objgen[cast(tuple[int, int], parent_objgen)]
                    if parent_page_index in deleted_set:
                        raise PdfPageMutationError(
                            "削除対象ページを参照する注釈の/P参照は未対応です"
                        )
                    raise PdfPageMutationError("他ページを参照する注釈の/P参照は未対応です")

    def _reject_unsupported_page_reordering_structures(self, path: Path) -> None:
        with pikepdf.open(path) as pdf:
            root = pdf.Root
            if "/AcroForm" in root:
                raise PdfPageMutationError("フォームを含むPDFの並べ替えは未対応です")
            if "/StructTreeRoot" in root:
                raise PdfPageMutationError("タグ付きPDFの並べ替えは未対応です")
            if "/PageLabels" in root:
                raise PdfPageMutationError("PageLabelsを含むPDFの並べ替えは未対応です")
            if "/Threads" in root:
                raise PdfPageMutationError("Article Threadsを含むPDFの並べ替えは未対応です")
            if "/OpenAction" in root:
                raise PdfPageMutationError("OpenActionを含むPDFの並べ替えは未対応です")

            page_index_by_objgen = {
                page.obj.objgen: index
                for index, page in enumerate(pdf.pages)
                if self._has_indirect_objgen(getattr(page.obj, "objgen", None))
            }
            for page_index, page in enumerate(pdf.pages):
                annots_object = page.obj.get("/Annots", None)
                if annots_object is None:
                    continue
                annots = self._dereference(annots_object)
                if not isinstance(annots, pikepdf.Array):
                    raise PdfPageMutationError("注釈配列が不正です")
                for annot_ref in annots:
                    annot = self._dereference(annot_ref)
                    if str(annot.get("/Subtype", "")) == "/Widget":
                        raise PdfPageMutationError("Widget注釈を含むPDFの並べ替えは未対応です")
                    if "/Dest" in annot or self._annotation_has_internal_goto_action(annot):
                        raise PdfPageMutationError("内部宛先注釈を含むPDFの並べ替えは未対応です")
                    parent_object = annot.get("/P", None)
                    if parent_object is None:
                        continue
                    parent = self._dereference(parent_object)
                    parent_objgen = getattr(parent, "objgen", None)
                    owning_objgen = getattr(page.obj, "objgen", None)
                    if parent_objgen == owning_objgen:
                        continue
                    if (
                        not self._has_indirect_objgen(parent_objgen)
                        or cast(tuple[int, int], parent_objgen) not in page_index_by_objgen
                        or str(parent.get("/Type", "")) != "/Page"
                    ):
                        raise PdfPageMutationError("注釈の/P参照を解決できません")
                    resolved_parent_index = page_index_by_objgen[
                        cast(tuple[int, int], parent_objgen)
                    ]
                    if resolved_parent_index != page_index:
                        raise PdfPageMutationError("他ページを参照する注釈の/P参照は未対応です")

    def _reject_unsupported_page_insertion_target_structures(self, path: Path) -> None:
        with pikepdf.open(path) as pdf:
            root = pdf.Root
            if len(pdf.pages) <= 0:
                raise PdfPageMutationError("0ページのPDFは扱えません")
            if "/AcroForm" in root:
                raise PdfPageMutationError("フォームを含むPDFへのページ挿入は未対応です")
            if "/StructTreeRoot" in root:
                raise PdfPageMutationError("タグ付きPDFへのページ挿入は未対応です")
            if "/PageLabels" in root:
                raise PdfPageMutationError("PageLabelsを含むPDFへのページ挿入は未対応です")
            if "/Threads" in root:
                raise PdfPageMutationError("Article Threadsを含むPDFへのページ挿入は未対応です")
            if "/OpenAction" in root:
                raise PdfPageMutationError("OpenActionを含むPDFへのページ挿入は未対応です")

    def _reject_unsupported_page_insertion_source_structures(
        self,
        path: Path,
        source_page_indexes: tuple[int, ...],
    ) -> None:
        self._reject_unsupported_import_source_structures(
            path,
            source_page_indexes,
            operation_label="挿入元",
        )

    def _reject_unsupported_import_source_structures(
        self,
        path: Path,
        source_page_indexes: tuple[int, ...],
        *,
        operation_label: str,
    ) -> None:
        operation_name = "ページ置換" if operation_label == "置換元" else "ページ挿入"
        with pikepdf.open(path) as pdf:
            root = pdf.Root
            if len(pdf.pages) <= 0:
                raise PdfPageMutationError("0ページのPDFは扱えません")
            if "/AcroForm" in root:
                raise PdfPageMutationError(f"フォームを含むPDFからの{operation_name}は未対応です")
            if "/StructTreeRoot" in root:
                raise PdfPageMutationError(f"タグ付きPDFからの{operation_name}は未対応です")
            if "/PageLabels" in root:
                raise PdfPageMutationError(f"PageLabelsを含むPDFからの{operation_name}は未対応です")
            if "/Threads" in root:
                raise PdfPageMutationError(
                    f"Article Threadsを含むPDFからの{operation_name}は未対応です"
                )
            if "/OpenAction" in root:
                raise PdfPageMutationError(f"OpenActionを含むPDFからの{operation_name}は未対応です")

            page_index_by_objgen = {
                page.obj.objgen: index
                for index, page in enumerate(pdf.pages)
                if self._has_indirect_objgen(getattr(page.obj, "objgen", None))
            }
            for page_index in source_page_indexes:
                page = pdf.pages[page_index]
                self._validate_supported_replacement_source_page_keys(
                    page.obj,
                    operation_label=operation_label,
                )
                annots_object = page.obj.get("/Annots", None)
                if annots_object is None:
                    continue
                annots = self._dereference(annots_object)
                if not isinstance(annots, pikepdf.Array):
                    raise PdfPageMutationError("注釈配列が不正です")
                for annot_ref in annots:
                    self._validate_importable_source_annotation(
                        annot_ref,
                        source_owning_page=page.obj,
                        source_page_objgens=set(page_index_by_objgen),
                        operation_label=operation_label,
                    )

    def _validate_supported_replacement_source_page_keys(
        self,
        page_object: pikepdf.Dictionary,
        *,
        operation_label: str,
    ) -> None:
        for key_object in page_object:
            key = str(key_object)
            if key in _REPLACEMENT_ALLOWED_PAGE_KEYS:
                continue
            if key == "/B" and operation_label == "挿入元":
                raise PdfPageMutationError("Article beadを含むページの挿入は未対応です")
            if key == "/AA":
                raise PdfPageMutationError(f"{operation_label}PDFのページアクションは未対応です")
            raise PdfPageMutationError(f"{operation_label}PDFのページに未対応の{key}があります")

    @staticmethod
    def _annotation_has_internal_goto_action(annot: Any) -> bool:
        action = annot.get("/A", None)
        if action is None:
            return False
        resolved_action = PdfPageMutationService._dereference(action)
        return str(resolved_action.get("/S", "")) == "/GoTo"

    def _apply_page_duplication_to_writer(
        self,
        writer: PdfWriter,
        reader: PdfReader,
        receipt: PageDuplicationReceipt,
    ) -> None:
        for inserted, source_page_index in enumerate(receipt.source_page_indexes):
            insertion_index = source_page_index + inserted + 1
            source_page = reader.pages[source_page_index]
            writer.insert_page(source_page, index=insertion_index)
            duplicated_page = writer.pages[insertion_index]
            self._detach_duplicate_page_objects(writer, source_page, duplicated_page)
            self._set_duplicate_annotation_parent_references(duplicated_page)

    def _detach_duplicate_page_objects(
        self,
        writer: PdfWriter,
        source_page: Any,
        duplicated_page: Any,
    ) -> None:
        for key in ("/MediaBox", "/CropBox", "/TrimBox", "/BleedBox", "/ArtBox"):
            source_value = source_page.get(key, None)
            if source_value is None:
                continue
            cloned_value = self._clone_into_writer(source_value, writer)
            duplicated_page[NameObject(key)] = cloned_value
        source_annots = source_page.get("/Annots", None)
        if source_annots is not None:
            duplicated_page[NameObject("/Annots")] = self._clone_annotations_into_writer(
                source_annots,
                writer,
            )

    def _clone_into_writer(self, value: Any, writer: PdfWriter) -> Any:
        clone = getattr(value, "clone", None)
        if not callable(clone):
            return value
        return clone(writer, force_duplicate=True)

    def _clone_annotations_into_writer(self, value: Any, writer: PdfWriter) -> ArrayObject:
        resolved_value = self._dereference(value)
        if not isinstance(resolved_value, ArrayObject):
            raise PdfPageMutationError("注釈配列が不正です")
        cloned_annots = ArrayObject()
        for annot_ref in resolved_value:
            annot = self._dereference(annot_ref)
            cloned_annot = self._clone_into_writer(annot, writer)
            indirect_reference = getattr(cloned_annot, "indirect_reference", None)
            cloned_annots.append(
                indirect_reference if indirect_reference is not None else cloned_annot
            )
        return cloned_annots

    def _set_duplicate_annotation_parent_references(self, page: Any) -> None:
        annots = page.get("/Annots", None)
        page_reference = getattr(page, "indirect_reference", None)
        if annots is None or page_reference is None:
            return
        for annot_ref in annots:
            annot = self._dereference(annot_ref)
            annot[NameObject("/P")] = page_reference

    def _copy_imported_page_annotations(
        self,
        target_pdf: pikepdf.Pdf,
        source_pdf: pikepdf.Pdf,
        *,
        source_page: pikepdf.Page,
        inserted_page: pikepdf.Page,
        operation_label: str = "挿入元",
    ) -> None:
        source_annots_object = source_page.obj.get("/Annots", None)
        if source_annots_object is None:
            if "/Annots" in inserted_page.obj:
                del inserted_page.obj["/Annots"]
            return
        source_annots = self._dereference(source_annots_object)
        if not isinstance(source_annots, pikepdf.Array):
            raise PdfPageMutationError("注釈配列のコピーに失敗しました")

        page_objgens = {
            page.obj.objgen
            for page in source_pdf.pages
            if self._has_indirect_objgen(getattr(page.obj, "objgen", None))
        }
        copied_annots = pikepdf.Array()
        for source_annot_ref in source_annots:
            copied_annot = self._copy_single_imported_annotation(
                target_pdf,
                source_pdf,
                source_annot_ref=source_annot_ref,
                source_owning_page=source_page.obj,
                source_page_objgens=page_objgens,
                inserted_page=inserted_page.obj,
                operation_label=operation_label,
            )
            copied_annots.append(copied_annot)
        inserted_page.obj[NameObject("/Annots")] = target_pdf.make_indirect(copied_annots)

    def _copy_single_imported_annotation(
        self,
        target_pdf: pikepdf.Pdf,
        source_pdf: pikepdf.Pdf,
        *,
        source_annot_ref: Any,
        source_owning_page: Any,
        source_page_objgens: set[tuple[int, int]],
        inserted_page: Any,
        operation_label: str,
    ) -> Any:
        source_annot, parent_state = self._validate_importable_source_annotation(
            source_annot_ref,
            source_owning_page=source_owning_page,
            source_page_objgens=source_page_objgens,
            operation_label=operation_label,
        )

        source_handle = source_annot
        if not self._has_indirect_objgen(getattr(source_annot, "objgen", None)):
            source_handle = source_pdf.make_indirect(source_annot)
        try:
            copied_annot = target_pdf.copy_foreign(source_handle)
        except Exception as exc:
            raise PdfPageMutationError("注釈オブジェクトのコピーに失敗しました") from exc

        copied_annot_object = self._dereference(copied_annot)
        self._rewrite_imported_annotation_parent(
            copied_annot_object,
            inserted_page=inserted_page,
            parent_state=parent_state,
        )
        return copied_annot

    def _replace_page_contents(
        self,
        target_pdf: pikepdf.Pdf,
        source_pdf: pikepdf.Pdf,
        *,
        target_page: pikepdf.Page,
        source_page: pikepdf.Page,
        source_page_snapshot: PdfPageStructureSnapshot,
    ) -> None:
        target_page_objgen = getattr(target_page.obj, "objgen", None)
        self._clear_replacement_target_page_dictionary(target_page.obj)
        copied_contents = self._copy_replacement_page_contents(
            target_pdf,
            source_pdf,
            source_page=source_page,
        )
        copied_resources = self._copy_effective_page_resources(
            target_pdf,
            source_pdf,
            source_page=source_page,
        )
        self._materialize_replacement_page_structure(
            target_page,
            source_page_snapshot,
            copied_contents=copied_contents,
            copied_resources=copied_resources,
        )
        self._copy_imported_page_annotations(
            target_pdf,
            source_pdf,
            source_page=source_page,
            inserted_page=target_page,
            operation_label="置換元",
        )
        if getattr(target_page.obj, "objgen", None) != target_page_objgen:
            raise PdfPageMutationError("置換対象ページのオブジェクト識別子が変化しました")

    def _copy_foreign_value(
        self,
        target_pdf: pikepdf.Pdf,
        source_pdf: pikepdf.Pdf,
        *,
        value: object,
        error_message: str,
    ) -> object:
        source_handle: object = value
        if not self._has_indirect_objgen(getattr(source_handle, "objgen", None)):
            source_handle = source_pdf.make_indirect(cast(pikepdf.Object, source_handle))
        try:
            copied_value = target_pdf.copy_foreign(cast(pikepdf.Object, source_handle))
        except Exception as exc:
            raise PdfPageMutationError(error_message) from exc
        return copied_value

    def _copy_replacement_page_contents(
        self,
        target_pdf: pikepdf.Pdf,
        source_pdf: pikepdf.Pdf,
        *,
        source_page: pikepdf.Page,
    ) -> object | None:
        contents = source_page.obj.get("/Contents", None)
        if contents is None:
            return None
        copied_contents = self._copy_foreign_value(
            target_pdf,
            source_pdf,
            value=contents,
            error_message="置換元ページの/Contentsコピーに失敗しました",
        )
        resolved = self._dereference(copied_contents)
        if not isinstance(resolved, (pikepdf.Stream, pikepdf.Array)):
            raise PdfPageMutationError("置換元ページの/Contents構造が不正です")
        return copied_contents

    def _copy_effective_page_resources(
        self,
        target_pdf: pikepdf.Pdf,
        source_pdf: pikepdf.Pdf,
        *,
        source_page: pikepdf.Page,
    ) -> object | None:
        resources: object | None = source_page.obj.get("/Resources", None)
        if resources is None:
            resources = self._resolve_inherited_value(source_page.obj, "/Resources")
        if resources is None:
            return None
        copied_resources = self._copy_foreign_value(
            target_pdf,
            source_pdf,
            value=resources,
            error_message="置換元ページの/Resourcesコピーに失敗しました",
        )
        resolved = self._dereference(copied_resources)
        if not isinstance(resolved, pikepdf.Dictionary):
            raise PdfPageMutationError("置換元ページの/Resources構造が不正です")
        return copied_resources

    def _clear_replacement_target_page_dictionary(self, page_object: pikepdf.Dictionary) -> None:
        for key_object in list(page_object.keys()):
            key = str(key_object)
            if key in {"/Type", "/Parent"}:
                continue
            del page_object[key_object]

    def _rewrite_imported_annotation_parent(
        self,
        annotation: Any,
        *,
        inserted_page: Any,
        parent_state: AnnotationParentState,
    ) -> None:
        if parent_state is AnnotationParentState.ABSENT:
            if "/P" in annotation:
                del annotation["/P"]
            return
        if parent_state is not AnnotationParentState.POINTS_TO_OWN_PAGE:
            raise PdfPageMutationError("注釈の/P参照の更新に失敗しました")
        annotation[NameObject("/P")] = inserted_page
        rewritten_parent = annotation.get("/P", None)
        resolved_parent = (
            self._dereference(rewritten_parent) if rewritten_parent is not None else None
        )
        if getattr(resolved_parent, "objgen", None) != getattr(inserted_page, "objgen", None):
            raise PdfPageMutationError("注釈の/P参照の更新に失敗しました")

    def _validate_importable_source_annotation(
        self,
        annot_ref: Any,
        *,
        source_owning_page: Any,
        source_page_objgens: set[tuple[int, int]],
        operation_label: str = "挿入元",
    ) -> tuple[pikepdf.Dictionary, AnnotationParentState]:
        source_annot = self._dereference(annot_ref)
        if not isinstance(source_annot, pikepdf.Dictionary):
            raise PdfPageMutationError(f"{operation_label}PDFの注釈構造が不正です")

        subtype_object = source_annot.get("/Subtype", None)
        if not isinstance(subtype_object, pikepdf.Name):
            raise PdfPageMutationError(f"{operation_label}PDFの注釈subtypeが不正です")
        subtype = str(subtype_object)
        if subtype not in SUPPORTED_IMPORTED_ANNOTATION_SUBTYPES:
            raise PdfPageMutationError(
                f"{operation_label}PDFの{subtype.removeprefix('/')}注釈は未対応です"
            )

        prohibited_key = self._first_prohibited_imported_annotation_key(source_annot)
        if prohibited_key is not None:
            raise PdfPageMutationError(
                self._unsupported_imported_annotation_key_message(
                    prohibited_key,
                    operation_label=operation_label,
                )
            )

        parent_state = self._annotation_parent_state(
            source_annot,
            owning_page=source_owning_page,
            page_objgens=source_page_objgens,
        )
        if parent_state is AnnotationParentState.POINTS_TO_OTHER_PAGE:
            raise PdfPageMutationError("他ページを参照する注釈の/P参照は未対応です")
        if parent_state is AnnotationParentState.INVALID:
            raise PdfPageMutationError("注釈の/P参照を解決できません")
        return source_annot, parent_state

    def _first_prohibited_imported_annotation_key(self, annot: pikepdf.Dictionary) -> str | None:
        for key in _PROHIBITED_IMPORTED_ANNOTATION_KEYS:
            if key in annot:
                return key
        return None

    @staticmethod
    def _unsupported_imported_annotation_key_message(
        key: str,
        *,
        operation_label: str,
    ) -> str:
        if key in {"/A", "/AA", "/Dest"}:
            return f"{operation_label}PDFのannotation actionは未対応です"
        if key == "/FS":
            return f"{operation_label}PDFのFileSpec参照付き注釈は未対応です"
        if key in {"/RichMediaContent", "/RichMediaSettings"}:
            return f"{operation_label}PDFのRichMedia注釈は未対応です"
        if key in {"/3DD", "/3DV"}:
            return f"{operation_label}PDFの3D注釈は未対応です"
        if key == "/Sound":
            return f"{operation_label}PDFのSound注釈は未対応です"
        if key == "/Movie":
            return f"{operation_label}PDFのMovie注釈は未対応です"
        return f"{operation_label}PDFのactive contentを含む注釈は未対応です"

    def _materialize_imported_page_structure(
        self,
        page: pikepdf.Page,
        snapshot: PdfPageStructureSnapshot,
    ) -> None:
        self._materialize_page_boxes_and_rotation(page, snapshot)
        resources = page.resources
        if resources is not None:
            page.obj[NameObject("/Resources")] = resources

    def _materialize_page_boxes_and_rotation(
        self,
        page: pikepdf.Page,
        snapshot: PdfPageStructureSnapshot,
    ) -> None:
        media_box = pikepdf.Array(list(snapshot.boxes.media_box))
        page.obj[NameObject("/MediaBox")] = media_box
        page.obj[NameObject("/CropBox")] = pikepdf.Array(list(snapshot.boxes.crop_box))
        self._set_optional_page_box(page.obj, "/TrimBox", snapshot.boxes.trim_box)
        self._set_optional_page_box(page.obj, "/BleedBox", snapshot.boxes.bleed_box)
        self._set_optional_page_box(page.obj, "/ArtBox", snapshot.boxes.art_box)
        if snapshot.direct_rotate_present:
            if snapshot.direct_rotate_value is None:
                raise PdfPageMutationError("挿入ページの回転情報が不正です")
            page.obj[NameObject("/Rotate")] = NumberObject(snapshot.direct_rotate_value)
        elif snapshot.effective_rotation != 0:
            page.obj[NameObject("/Rotate")] = NumberObject(snapshot.effective_rotation)
        else:
            rotate_key = NameObject("/Rotate")
            if rotate_key in page.obj:
                del page.obj[rotate_key]

    def _materialize_replacement_page_structure(
        self,
        page: pikepdf.Page,
        snapshot: PdfPageStructureSnapshot,
        *,
        copied_contents: object | None,
        copied_resources: object | None,
    ) -> None:
        if copied_contents is not None:
            page.obj[NameObject("/Contents")] = copied_contents
        if copied_resources is not None:
            page.obj[NameObject("/Resources")] = copied_resources
        self._materialize_page_boxes_and_rotation(page, snapshot)

    def _apply_page_replacement_pairs(
        self,
        target_pdf: pikepdf.Pdf,
        source_pdf: pikepdf.Pdf,
        *,
        plan: PageReplacementPlan,
        source_selected_page_snapshots: tuple[PdfPageStructureSnapshot, ...],
    ) -> None:
        for (target_page_index, source_page_index), source_page_snapshot in zip(
            plan.replacement_pairs,
            source_selected_page_snapshots,
            strict=True,
        ):
            self._replace_page_contents(
                target_pdf,
                source_pdf,
                target_page=target_pdf.pages[target_page_index],
                source_page=source_pdf.pages[source_page_index],
                source_page_snapshot=source_page_snapshot,
            )

    def _set_optional_page_box(
        self,
        page_object: Any,
        key: str,
        value: tuple[float, float, float, float] | None,
    ) -> None:
        key_object = NameObject(key)
        if value is None:
            if key_object in page_object:
                del page_object[key_object]
            return
        page_object[key_object] = pikepdf.Array(list(value))

    def _apply_page_order_to_pdf(
        self,
        pdf: pikepdf.Pdf,
        target_order: tuple[int, ...],
    ) -> None:
        page_count = len(pdf.pages)
        if len(target_order) != page_count:
            raise PdfPageMutationError("ページ順序の長さが現在のページ数と一致しません")
        if set(target_order) != set(range(page_count)):
            raise PdfPageMutationError("ページ順序が不正です")
        pages = [pdf.pages[index] for index in range(page_count)]
        reordered_pages = [pages[index] for index in target_order]
        for page_index in range(page_count - 1, -1, -1):
            del pdf.pages[page_index]
        pdf.pages.extend(reordered_pages)

    def _validate_document_level_snapshots(
        self,
        current: PdfDocumentStructureSnapshot,
        before: PdfDocumentStructureSnapshot,
        *,
        page_mapping: tuple[int | None, ...] | None,
    ) -> None:
        if current.metadata_fingerprint != before.metadata_fingerprint:
            raise PdfPageMutationError("メタデータの検証に失敗しました")
        expected_outlines = (
            before.outlines
            if page_mapping is None
            else self._map_outline_snapshots(before.outlines, page_mapping)
        )
        if current.outlines != expected_outlines:
            raise PdfPageMutationError("アウトラインの検証に失敗しました")
        expected_named_destinations = (
            before.named_destinations
            if page_mapping is None
            else self._map_named_destination_snapshots(before.named_destinations, page_mapping)
        )
        if current.named_destinations != expected_named_destinations:
            raise PdfPageMutationError("名前付き宛先の検証に失敗しました")
        if current.attachments_fingerprint != before.attachments_fingerprint:
            raise PdfPageMutationError("添付ファイルの検証に失敗しました")

    def _map_outline_snapshots(
        self,
        items: tuple[PdfOutlineItemSnapshot, ...],
        page_mapping: tuple[int | None, ...],
    ) -> tuple[PdfOutlineItemSnapshot, ...]:
        return tuple(
            PdfOutlineItemSnapshot(
                title=item.title,
                destination_page_index=(
                    None
                    if item.destination_page_index is None
                    else self._mapped_page_index(item.destination_page_index, page_mapping)
                ),
                action_type=item.action_type,
                children=self._map_outline_snapshots(item.children, page_mapping),
            )
            for item in items
        )

    def _map_named_destination_snapshots(
        self,
        items: tuple[PdfNamedDestinationSnapshot, ...],
        page_mapping: tuple[int | None, ...],
    ) -> tuple[PdfNamedDestinationSnapshot, ...]:
        return tuple(
            PdfNamedDestinationSnapshot(
                name=item.name,
                destination_page_index=self._mapped_page_index(
                    item.destination_page_index,
                    page_mapping,
                ),
            )
            for item in items
        )

    def _mapped_page_index(
        self,
        page_index: int,
        page_mapping: tuple[int | None, ...],
    ) -> int:
        if page_index < 0 or page_index >= len(page_mapping):
            raise PdfPageMutationError("ページ参照の検証に失敗しました")
        mapped_page_index = page_mapping[page_index]
        if mapped_page_index is None:
            raise PdfPageMutationError("ページ参照の検証に失敗しました")
        return mapped_page_index

    def _validate_duplicate_page_layout(
        self,
        after_snapshot: PdfDocumentStructureSnapshot,
        receipt: PageDuplicationReceipt,
    ) -> None:
        all_original_indexes_after = _all_original_indexes_after(
            receipt.original_page_count,
            receipt.source_page_indexes,
        )
        for original_page_index, current_page_index in enumerate(all_original_indexes_after):
            current_page = after_snapshot.pages[current_page_index]
            original_page = receipt.before_snapshot.pages[original_page_index]
            if current_page != original_page:
                raise PdfPageMutationError("ページ順序または構造の検証に失敗しました")
        for source_page_index, duplicate_page_index in zip(
            receipt.source_page_indexes,
            receipt.duplicate_page_indexes,
            strict=True,
        ):
            duplicate_page = after_snapshot.pages[duplicate_page_index]
            source_page = self._expected_duplicate_page_snapshot(
                receipt.before_snapshot.pages[source_page_index]
            )
            if duplicate_page != source_page:
                raise PdfPageMutationError("複製ページの構造検証に失敗しました")

    def _expected_duplicate_page_snapshot(
        self,
        page: PdfPageStructureSnapshot,
    ) -> PdfPageStructureSnapshot:
        return PdfPageStructureSnapshot(
            content_fingerprint=page.content_fingerprint,
            boxes=page.boxes,
            direct_resources_present=page.direct_resources_present,
            resources_fingerprint=page.resources_fingerprint,
            direct_rotate_present=page.direct_rotate_present,
            direct_rotate_value=page.direct_rotate_value,
            effective_rotation=page.effective_rotation,
            annotations=tuple(
                PdfAnnotationStructureSnapshot(
                    subtype=annot.subtype,
                    rect=annot.rect,
                    has_appearance=annot.has_appearance,
                    appearance_fingerprint=annot.appearance_fingerprint,
                    parent_state=AnnotationParentState.POINTS_TO_OWN_PAGE,
                    fingerprint=annot.fingerprint,
                )
                for annot in page.annotations
            ),
            direct_page_keys=page.direct_page_keys,
            extra_page_entries_fingerprint=page.extra_page_entries_fingerprint,
        )

    def _validate_duplicate_page_independence(
        self,
        path: Path,
        receipt: PageDuplicationReceipt,
    ) -> None:
        with pikepdf.open(path) as pdf:
            source_current_indexes = dict(
                zip(
                    receipt.source_page_indexes,
                    receipt.original_page_indexes_after,
                    strict=True,
                )
            )
            for source_page_index, duplicate_page_index in zip(
                receipt.source_page_indexes,
                receipt.duplicate_page_indexes,
                strict=True,
            ):
                source_page = pdf.pages[source_current_indexes[source_page_index]].obj
                duplicate_page = pdf.pages[duplicate_page_index].obj
                if getattr(source_page, "objgen", None) == getattr(duplicate_page, "objgen", None):
                    raise PdfPageMutationError("複製ページが元ページと同じオブジェクトです")
                self._validate_page_box_independence(source_page, duplicate_page)
                source_annots = self._annotation_objects(source_page.get("/Annots", None))
                duplicate_annots = self._annotation_objects(duplicate_page.get("/Annots", None))
                if len(source_annots) != len(duplicate_annots):
                    raise PdfPageMutationError("複製ページの注釈数検証に失敗しました")
                source_annots_obj = source_page.get("/Annots", None)
                duplicate_annots_obj = duplicate_page.get("/Annots", None)
                if source_annots_obj is not None and duplicate_annots_obj is not None:
                    source_annots_value = self._dereference(source_annots_obj)
                    duplicate_annots_value = self._dereference(duplicate_annots_obj)
                    source_array_objgen = getattr(source_annots_value, "objgen", None)
                    duplicate_array_objgen = getattr(duplicate_annots_value, "objgen", None)
                    if (
                        self._has_indirect_objgen(source_array_objgen)
                        and self._has_indirect_objgen(duplicate_array_objgen)
                        and source_array_objgen == duplicate_array_objgen
                    ):
                        raise PdfPageMutationError("複製ページが元ページと注釈配列を共有しています")
                for source_annot, duplicate_annot in zip(
                    source_annots,
                    duplicate_annots,
                    strict=True,
                ):
                    source_objgen = getattr(source_annot, "objgen", None)
                    duplicate_objgen = getattr(duplicate_annot, "objgen", None)
                    if source_objgen == duplicate_objgen:
                        raise PdfPageMutationError(
                            "複製ページが元ページと注釈オブジェクトを共有しています"
                        )
                    duplicate_parent = duplicate_annot.get("/P", None)
                    if duplicate_parent is None:
                        raise PdfPageMutationError("複製注釈の/P参照が不足しています")
                    resolved_parent = self._dereference(duplicate_parent)
                    if getattr(resolved_parent, "objgen", None) != getattr(
                        duplicate_page,
                        "objgen",
                        None,
                    ):
                        raise PdfPageMutationError("複製注釈の/P参照が複製ページを指していません")

    def _validate_page_box_independence(self, source_page: Any, duplicate_page: Any) -> None:
        for key in ("/MediaBox", "/CropBox", "/TrimBox", "/BleedBox", "/ArtBox"):
            source_value = source_page.get(key, None)
            duplicate_value = duplicate_page.get(key, None)
            if source_value is None or duplicate_value is None:
                continue
            source_resolved = self._dereference(source_value)
            duplicate_resolved = self._dereference(duplicate_value)
            source_objgen = getattr(source_resolved, "objgen", None)
            duplicate_objgen = getattr(duplicate_resolved, "objgen", None)
            if (
                self._has_indirect_objgen(source_objgen)
                and self._has_indirect_objgen(duplicate_objgen)
                and source_objgen == duplicate_objgen
            ):
                raise PdfPageMutationError("複製ページが元ページとページボックスを共有しています")

    def _annotation_objects(self, annots_object: object) -> tuple[Any, ...]:
        if annots_object is None:
            return ()
        annots = self._dereference(annots_object)
        if not isinstance(annots, pikepdf.Array):
            raise PdfPageMutationError("注釈配列が不正です")
        return tuple(self._dereference(item) for item in annots)

    def _validate_duplicate_renders(
        self,
        path: Path,
        receipt: PageDuplicationReceipt,
    ) -> None:
        render_info = self._render_page_digests(
            path,
            sorted(
                {
                    *receipt.original_page_indexes_after,
                    *receipt.duplicate_page_indexes,
                }
            ),
        )
        for source_current_index, duplicate_page_index in zip(
            receipt.original_page_indexes_after,
            receipt.duplicate_page_indexes,
            strict=True,
        ):
            source_render = render_info[source_current_index]
            duplicate_render = render_info[duplicate_page_index]
            if source_render != duplicate_render:
                raise PdfPageMutationError("複製ページの描画検証に失敗しました")

    def _snapshot_selected_source_pages(
        self,
        path: Path,
        source_page_indexes: tuple[int, ...],
    ) -> tuple[PdfPageStructureSnapshot, ...]:
        snapshot = self._snapshot_document_structure(path)
        return tuple(snapshot.pages[page_index] for page_index in source_page_indexes)

    def _expected_imported_page_snapshot(
        self,
        page: PdfPageStructureSnapshot,
    ) -> PdfPageStructureSnapshot:
        direct_page_keys = ["/Type", "/Parent", "/MediaBox", "/CropBox"]
        if page.content_fingerprint != "none":
            direct_page_keys.append("/Contents")
        if page.resources_fingerprint != "none":
            direct_page_keys.append("/Resources")
        if page.boxes.trim_box is not None:
            direct_page_keys.append("/TrimBox")
        if page.boxes.bleed_box is not None:
            direct_page_keys.append("/BleedBox")
        if page.boxes.art_box is not None:
            direct_page_keys.append("/ArtBox")
        if page.direct_rotate_present or page.effective_rotation != 0:
            direct_page_keys.append("/Rotate")
        if page.annotations:
            direct_page_keys.append("/Annots")
        return PdfPageStructureSnapshot(
            content_fingerprint=page.content_fingerprint,
            boxes=PageBoxState(
                media_box_direct_present=True,
                media_box=page.boxes.media_box,
                crop_box=page.boxes.crop_box,
                crop_box_direct_present=True,
                crop_box_direct_value=page.boxes.crop_box_direct_value
                if page.boxes.crop_box_direct_value is not None
                else page.boxes.crop_box,
                crop_box_inherited=False,
                crop_box_falls_back_to_media_box=False,
                trim_box=page.boxes.trim_box,
                bleed_box=page.boxes.bleed_box,
                art_box=page.boxes.art_box,
            ),
            direct_resources_present=True,
            resources_fingerprint=page.resources_fingerprint,
            direct_rotate_present=page.direct_rotate_present or page.effective_rotation != 0,
            direct_rotate_value=(
                page.direct_rotate_value
                if page.direct_rotate_present
                else (page.effective_rotation if page.effective_rotation != 0 else None)
            ),
            effective_rotation=page.effective_rotation,
            annotations=tuple(
                PdfAnnotationStructureSnapshot(
                    subtype=annot.subtype,
                    rect=annot.rect,
                    has_appearance=annot.has_appearance,
                    appearance_fingerprint=annot.appearance_fingerprint,
                    parent_state=(
                        AnnotationParentState.POINTS_TO_OWN_PAGE
                        if annot.parent_state is AnnotationParentState.POINTS_TO_OWN_PAGE
                        else AnnotationParentState.ABSENT
                    ),
                    fingerprint=annot.fingerprint,
                )
                for annot in page.annotations
            ),
            direct_page_keys=tuple(sorted(direct_page_keys)),
            extra_page_entries_fingerprint=self._empty_page_entry_fingerprint(),
        )

    def _empty_page_entry_fingerprint(self) -> str:
        return self._object_fingerprint(pikepdf.Dictionary(), exclude_keys=frozenset())

    def _page_insertion_render_page_indexes(self, plan: PageInsertionPlan) -> tuple[int, ...]:
        indexes = set(plan.inserted_page_indexes_after)
        if plan.insertion_slot > 0:
            indexes.add(plan.insertion_slot - 1)
        if plan.insertion_slot < plan.target_page_count:
            indexes.add(plan.insertion_slot + len(plan.source_page_indexes))
        if plan.insertion_slot < plan.target_page_count:
            indexes.add(plan.target_old_to_new[plan.insertion_slot])
        return tuple(sorted(indexes))

    def _target_snapshot_validation_page_indexes(
        self,
        page_count: int,
        insertion_slot: int,
    ) -> tuple[int, ...]:
        indexes: set[int] = set()
        if page_count <= 0:
            return ()
        indexes.add(0)
        indexes.add(page_count - 1)
        if insertion_slot > 0:
            indexes.add(insertion_slot - 1)
        if insertion_slot < page_count:
            indexes.add(insertion_slot)
        return tuple(sorted(indexes))

    def _normalize_render_page_indexes(
        self,
        page_count: int,
        page_indexes: tuple[int, ...],
    ) -> tuple[int, ...]:
        normalized = {page_index for page_index in page_indexes if 0 <= page_index < page_count}
        return tuple(sorted(normalized))

    def _render_pages(self, path: Path, page_indexes: tuple[int, ...]) -> None:
        self._render_page_digests(path, page_indexes)

    def _validate_inserted_page_render_equivalence(
        self,
        *,
        source_path: Path,
        source_page_indexes: tuple[int, ...],
        imported_path: Path,
        imported_page_indexes: tuple[int, ...],
    ) -> None:
        source_render = self._render_page_digests(source_path, source_page_indexes)
        imported_render = self._render_page_digests(imported_path, imported_page_indexes)
        for source_page_index, imported_page_index in zip(
            source_page_indexes,
            imported_page_indexes,
            strict=True,
        ):
            if source_render[source_page_index] != imported_render[imported_page_index]:
                raise PdfPageMutationError("挿入ページの描画検証に失敗しました")

    def _validate_page_replacement_candidate(
        self,
        path: Path,
        *,
        source_snapshot_path: Path,
        target_before_snapshot: PdfDocumentStructureSnapshot,
        source_selected_page_snapshots: tuple[PdfPageStructureSnapshot, ...],
        plan: PageReplacementPlan,
    ) -> PdfDocumentStructureSnapshot:
        self._validate_basic_candidate(path, expected_page_count=target_before_snapshot.page_count)
        after_snapshot = self._snapshot_document_structure(path)
        self._validate_document_level_snapshots(
            after_snapshot,
            target_before_snapshot,
            page_mapping=tuple(range(target_before_snapshot.page_count)),
        )
        replaced_lookup = dict(
            zip(
                plan.target_page_indexes,
                source_selected_page_snapshots,
                strict=True,
            )
        )
        for page_index, current_page in enumerate(after_snapshot.pages):
            replacement_source = replaced_lookup.get(page_index)
            if replacement_source is None:
                if current_page != target_before_snapshot.pages[page_index]:
                    raise PdfPageMutationError("更新後のページ順序または構造の検証に失敗しました")
                continue
            if current_page != self._expected_imported_page_snapshot(replacement_source):
                raise PdfPageMutationError("置換ページの構造検証に失敗しました")
        self._render_pages(path, plan.target_page_indexes)
        self._validate_inserted_page_render_equivalence(
            source_path=source_snapshot_path,
            source_page_indexes=plan.source_page_indexes,
            imported_path=path,
            imported_page_indexes=plan.target_page_indexes,
        )
        return after_snapshot

    def _validate_page_deletion_candidate(
        self,
        path: Path,
        *,
        before_snapshot: PdfDocumentStructureSnapshot,
        deleted_page_indexes: tuple[int, ...],
    ) -> PdfDocumentStructureSnapshot:
        expected_page_count = before_snapshot.page_count - len(deleted_page_indexes)
        self._validate_basic_candidate(path, expected_page_count=expected_page_count)
        after_snapshot = self._snapshot_document_structure(path)
        if after_snapshot.page_count != expected_page_count:
            raise PdfPageMutationError("更新後のページ数検証に失敗しました")
        transition = self._build_delete_execute_transition(
            before_snapshot.page_count,
            deleted_page_indexes,
        )
        self._validate_document_level_snapshots(
            after_snapshot,
            before_snapshot,
            page_mapping=transition.cache_old_to_new,
        )
        survivor_original_indexes = self._survivor_original_indexes(
            before_snapshot.page_count,
            deleted_page_indexes,
        )
        if len(after_snapshot.pages) != len(survivor_original_indexes):
            raise PdfPageMutationError("更新後のページ順序検証に失敗しました")
        for new_page_index, original_page_index in enumerate(survivor_original_indexes):
            if after_snapshot.pages[new_page_index] != before_snapshot.pages[original_page_index]:
                raise PdfPageMutationError("更新後のページ順序または構造の検証に失敗しました")
        self._render_pages(path, tuple(range(expected_page_count)))
        return after_snapshot

    def _validate_current_deletion_state(
        self,
        path: Path,
        receipt: PageDeletionReceipt,
    ) -> None:
        current_snapshot = self._snapshot_document_structure(path)
        if current_snapshot != receipt.after_snapshot:
            raise PdfPageMutationError("削除済みページの状態が変化しているため元に戻せません")

    def _validate_delete_undo_snapshot(
        self,
        working_copy_path: Path,
        receipt: PageDeletionReceipt,
    ) -> None:
        snapshot_path = self._validate_delete_undo_snapshot_ownership(
            working_copy_path,
            receipt,
            require_exists=True,
        )
        current_sha = self._sha256_file(snapshot_path)
        if current_sha != receipt.undo_snapshot_sha256:
            raise PdfPageMutationError("削除前スナップショットの整合性検証に失敗しました")
        self._validate_delete_undo_snapshot_path(
            snapshot_path,
            expected_page_count=receipt.original_page_count,
        )
        snapshot = self._snapshot_document_structure(snapshot_path)
        if snapshot != receipt.before_snapshot:
            raise PdfPageMutationError("削除前スナップショットの構造検証に失敗しました")

    def _validate_delete_undo_snapshot_ownership(
        self,
        working_copy_path: Path,
        receipt: PageDeletionReceipt,
        *,
        require_exists: bool,
    ) -> Path:
        resolved_working_copy_path = working_copy_path.expanduser().resolve()
        receipt_working_copy_path = receipt.working_copy_path.expanduser().resolve()
        if receipt_working_copy_path != resolved_working_copy_path:
            raise PdfPageMutationError("削除前スナップショットの所有者が一致しません")
        snapshot_path = receipt.undo_snapshot_path.expanduser()
        if not snapshot_path.is_absolute():
            raise PdfPageMutationError("削除前スナップショットの場所が不正です")
        if snapshot_path == resolved_working_copy_path:
            raise PdfPageMutationError("削除前スナップショットの場所が不正です")
        if snapshot_path.parent != resolved_working_copy_path.parent:
            raise PdfPageMutationError("削除前スナップショットの場所が不正です")
        expected_prefix = f".{resolved_working_copy_path.stem}.delete-undo."
        if (
            not snapshot_path.name.startswith(expected_prefix)
            or snapshot_path.suffix.lower() != ".pdf"
        ):
            raise PdfPageMutationError("削除前スナップショットの場所が不正です")
        if snapshot_path.is_symlink():
            raise PdfPageMutationError("削除前スナップショットの場所が不正です")
        if snapshot_path.exists():
            if not snapshot_path.is_file():
                raise PdfPageMutationError("削除前スナップショットの場所が不正です")
        elif require_exists:
            raise PdfPageMutationError("削除前スナップショットが見つかりません")
        return snapshot_path

    def _validate_undo_page_deletion_candidate(
        self,
        path: Path,
        receipt: PageDeletionReceipt,
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=receipt.original_page_count)
        snapshot = self._snapshot_document_structure(path)
        if snapshot != receipt.before_snapshot:
            raise PdfPageMutationError("ページ削除の取り消し検証に失敗しました")
        self._render_pages(path, tuple(range(receipt.original_page_count)))

    def _validate_redo_page_deletion_candidate(
        self,
        path: Path,
        receipt: PageDeletionReceipt,
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=receipt.after_snapshot.page_count)
        snapshot = self._snapshot_document_structure(path)
        if snapshot != receipt.after_snapshot:
            raise PdfPageMutationError("ページ削除の再適用検証に失敗しました")
        self._render_pages(path, tuple(range(receipt.after_snapshot.page_count)))

    def _validate_redo_page_reordering_candidate(
        self,
        path: Path,
        receipt: PageReorderReceipt,
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=receipt.original_page_count)
        snapshot = self._snapshot_document_structure(path)
        if snapshot != receipt.after_snapshot:
            raise PdfPageMutationError("ページ並べ替えの再適用検証に失敗しました")
        self._render_pages(
            path,
            self._reorder_execute_render_page_indexes(self._reorder_plan_from_receipt(receipt)),
        )

    def _validate_current_insertion_state(
        self,
        path: Path,
        receipt: PageInsertionReceipt,
    ) -> None:
        current_snapshot = self._snapshot_document_structure(path)
        if current_snapshot != receipt.target_after_snapshot:
            raise PdfPageMutationError("挿入済みページの状態が変化しているため元に戻せません")

    def _validate_current_replacement_state(
        self,
        path: Path,
        receipt: PageReplacementReceipt,
    ) -> None:
        current_snapshot = self._snapshot_document_structure(path)
        if current_snapshot != receipt.target_after_snapshot:
            raise PdfPageMutationError("置換済みページの状態が変化しているため元に戻せません")

    def _validate_insert_source_snapshot(
        self,
        working_copy_path: Path,
        receipt: PageInsertionReceipt,
    ) -> None:
        snapshot_path = self._validate_insert_source_snapshot_ownership(
            working_copy_path,
            receipt,
            require_exists=True,
        )
        if self._sha256_file(snapshot_path) != receipt.source_snapshot_sha256:
            raise PdfPageMutationError("挿入元スナップショットの整合性検証に失敗しました")
        self._validate_insert_source_snapshot_path(
            snapshot_path,
            expected_page_count=receipt.source_snapshot_page_count,
            render_page_indexes=receipt.source_page_indexes,
        )
        snapshot = self._snapshot_selected_source_pages(snapshot_path, receipt.source_page_indexes)
        if snapshot != receipt.source_selected_page_snapshots:
            raise PdfPageMutationError("挿入元スナップショットの構造検証に失敗しました")

    def _validate_replace_source_snapshot(
        self,
        working_copy_path: Path,
        receipt: PageReplacementReceipt,
    ) -> None:
        snapshot_path = self._validate_replace_source_snapshot_ownership(
            working_copy_path,
            receipt,
            require_exists=True,
        )
        if self._sha256_file(snapshot_path) != receipt.source_snapshot_sha256:
            raise PdfPageMutationError("置換元スナップショットの整合性検証に失敗しました")
        self._validate_replace_source_snapshot_path(
            snapshot_path,
            expected_page_count=receipt.source_snapshot_page_count,
            render_page_indexes=receipt.source_page_indexes,
        )
        snapshot = self._snapshot_selected_source_pages(snapshot_path, receipt.source_page_indexes)
        if snapshot != receipt.source_selected_page_snapshots:
            raise PdfPageMutationError("置換元スナップショットの構造検証に失敗しました")

    def _validate_insert_target_undo_snapshot(
        self,
        working_copy_path: Path,
        receipt: PageInsertionReceipt,
    ) -> None:
        snapshot_path = self._validate_insert_target_undo_snapshot_ownership(
            working_copy_path,
            receipt,
            require_exists=True,
        )
        if self._sha256_file(snapshot_path) != receipt.target_undo_snapshot_sha256:
            raise PdfPageMutationError("挿入前スナップショットの整合性検証に失敗しました")
        self._validate_insert_target_undo_snapshot_path(
            snapshot_path,
            expected_page_count=receipt.target_page_count_before,
            render_page_indexes=self._target_snapshot_validation_page_indexes(
                receipt.target_page_count_before,
                receipt.insertion_slot,
            ),
        )
        snapshot = self._snapshot_document_structure(snapshot_path)
        if snapshot != receipt.target_before_snapshot:
            raise PdfPageMutationError("挿入前スナップショットの構造検証に失敗しました")

    def _validate_replace_target_undo_snapshot(
        self,
        working_copy_path: Path,
        receipt: PageReplacementReceipt,
    ) -> None:
        snapshot_path = self._validate_replace_target_undo_snapshot_ownership(
            working_copy_path,
            receipt,
            require_exists=True,
        )
        if self._sha256_file(snapshot_path) != receipt.target_undo_snapshot_sha256:
            raise PdfPageMutationError("置換前スナップショットの整合性検証に失敗しました")
        self._validate_replace_target_undo_snapshot_path(
            snapshot_path,
            expected_page_count=receipt.target_page_count_before,
            render_page_indexes=receipt.target_page_indexes,
        )
        snapshot = self._snapshot_document_structure(snapshot_path)
        if snapshot != receipt.target_before_snapshot:
            raise PdfPageMutationError("置換前スナップショットの構造検証に失敗しました")

    def _validate_insert_source_snapshot_ownership(
        self,
        working_copy_path: Path,
        receipt: PageInsertionReceipt,
        *,
        require_exists: bool,
    ) -> Path:
        return self._validate_named_snapshot_ownership(
            working_copy_path,
            receipt.working_copy_path,
            receipt.source_snapshot_path,
            expected_label="insert-source",
            require_exists=require_exists,
        )

    def _validate_insert_target_undo_snapshot_ownership(
        self,
        working_copy_path: Path,
        receipt: PageInsertionReceipt,
        *,
        require_exists: bool,
    ) -> Path:
        return self._validate_named_snapshot_ownership(
            working_copy_path,
            receipt.working_copy_path,
            receipt.target_undo_snapshot_path,
            expected_label="insert-undo",
            require_exists=require_exists,
        )

    def _validate_replace_source_snapshot_ownership(
        self,
        working_copy_path: Path,
        receipt: PageReplacementReceipt,
        *,
        require_exists: bool,
    ) -> Path:
        return self._validate_named_snapshot_ownership(
            working_copy_path,
            receipt.working_copy_path,
            receipt.source_snapshot_path,
            expected_label="replace-source",
            require_exists=require_exists,
        )

    def _validate_replace_target_undo_snapshot_ownership(
        self,
        working_copy_path: Path,
        receipt: PageReplacementReceipt,
        *,
        require_exists: bool,
    ) -> Path:
        return self._validate_named_snapshot_ownership(
            working_copy_path,
            receipt.working_copy_path,
            receipt.target_undo_snapshot_path,
            expected_label="replace-undo",
            require_exists=require_exists,
        )

    def _validate_named_snapshot_ownership(
        self,
        working_copy_path: Path,
        receipt_working_copy_path: Path,
        snapshot_path: Path,
        *,
        expected_label: str,
        require_exists: bool,
    ) -> Path:
        resolved_working_copy_path = working_copy_path.expanduser().resolve()
        if receipt_working_copy_path.expanduser().resolve() != resolved_working_copy_path:
            raise PdfPageMutationError("スナップショットの所有者が一致しません")
        expanded_snapshot_path = snapshot_path.expanduser()
        if not expanded_snapshot_path.is_absolute():
            raise PdfPageMutationError("スナップショットの場所が不正です")
        if expanded_snapshot_path == resolved_working_copy_path:
            raise PdfPageMutationError("スナップショットの場所が不正です")
        if expanded_snapshot_path.parent != resolved_working_copy_path.parent:
            raise PdfPageMutationError("スナップショットの場所が不正です")
        expected_prefix = f".{resolved_working_copy_path.stem}.{expected_label}."
        if (
            not expanded_snapshot_path.name.startswith(expected_prefix)
            or expanded_snapshot_path.suffix.lower() != ".pdf"
        ):
            raise PdfPageMutationError("スナップショットの場所が不正です")
        if expanded_snapshot_path.is_symlink():
            raise PdfPageMutationError("スナップショットの場所が不正です")
        if expanded_snapshot_path.exists():
            if not expanded_snapshot_path.is_file():
                raise PdfPageMutationError("スナップショットの場所が不正です")
        elif require_exists:
            raise PdfPageMutationError("スナップショットが見つかりません")
        return expanded_snapshot_path

    def _validate_insert_source_snapshot_path(
        self,
        path: Path,
        *,
        expected_page_count: int,
        render_page_indexes: tuple[int, ...],
    ) -> None:
        try:
            self._validate_basic_candidate(path, expected_page_count=expected_page_count)
            self._render_pages(
                path,
                self._normalize_render_page_indexes(expected_page_count, render_page_indexes),
            )
        except PdfPageMutationError as exc:
            raise PdfPageMutationError("挿入元スナップショットPDFの検証に失敗しました") from exc

    def _validate_insert_target_undo_snapshot_path(
        self,
        path: Path,
        *,
        expected_page_count: int,
        render_page_indexes: tuple[int, ...],
    ) -> None:
        try:
            self._validate_basic_candidate(path, expected_page_count=expected_page_count)
            self._render_pages(
                path,
                self._normalize_render_page_indexes(expected_page_count, render_page_indexes),
            )
        except PdfPageMutationError as exc:
            raise PdfPageMutationError("挿入前スナップショットPDFの検証に失敗しました") from exc

    def _validate_replace_source_snapshot_path(
        self,
        path: Path,
        *,
        expected_page_count: int,
        render_page_indexes: tuple[int, ...],
    ) -> None:
        try:
            self._validate_basic_candidate(path, expected_page_count=expected_page_count)
            self._render_pages(
                path,
                self._normalize_render_page_indexes(expected_page_count, render_page_indexes),
            )
        except PdfPageMutationError as exc:
            raise PdfPageMutationError("置換元スナップショットPDFの検証に失敗しました") from exc

    def _validate_replace_target_undo_snapshot_path(
        self,
        path: Path,
        *,
        expected_page_count: int,
        render_page_indexes: tuple[int, ...],
    ) -> None:
        try:
            self._validate_basic_candidate(path, expected_page_count=expected_page_count)
            self._render_pages(
                path,
                self._normalize_render_page_indexes(expected_page_count, render_page_indexes),
            )
        except PdfPageMutationError as exc:
            raise PdfPageMutationError("置換前スナップショットPDFの検証に失敗しました") from exc

    def _validate_undo_page_insertion_candidate(
        self,
        path: Path,
        receipt: PageInsertionReceipt,
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=receipt.target_page_count_before)
        snapshot = self._snapshot_document_structure(path)
        if snapshot != receipt.target_before_snapshot:
            raise PdfPageMutationError("ページ挿入の取り消し検証に失敗しました")
        self._render_pages(
            path,
            self._target_snapshot_validation_page_indexes(
                receipt.target_page_count_before,
                receipt.insertion_slot,
            ),
        )

    def _validate_redo_page_insertion_candidate(
        self,
        path: Path,
        receipt: PageInsertionReceipt,
    ) -> None:
        self._validate_basic_candidate(
            path,
            expected_page_count=receipt.target_after_snapshot.page_count,
        )
        snapshot = self._snapshot_document_structure(path)
        if snapshot != receipt.target_after_snapshot:
            raise PdfPageMutationError("ページ挿入の再適用検証に失敗しました")
        plan = build_page_insertion_plan(
            receipt.target_page_count_before,
            receipt.source_snapshot_page_count,
            receipt.source_page_indexes,
            receipt.insertion_slot,
        )
        self._render_pages(path, self._page_insertion_render_page_indexes(plan))
        self._validate_inserted_page_render_equivalence(
            source_path=receipt.source_snapshot_path,
            source_page_indexes=receipt.source_page_indexes,
            imported_path=path,
            imported_page_indexes=plan.inserted_page_indexes_after,
        )

    def _validate_undo_page_replacement_candidate(
        self,
        path: Path,
        receipt: PageReplacementReceipt,
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=receipt.target_page_count_before)
        snapshot = self._snapshot_document_structure(path)
        if snapshot != receipt.target_before_snapshot:
            raise PdfPageMutationError("ページ置換の取り消し検証に失敗しました")
        self._render_pages(path, receipt.target_page_indexes)

    def _validate_redo_page_replacement_candidate(
        self,
        path: Path,
        receipt: PageReplacementReceipt,
    ) -> None:
        self._validate_basic_candidate(path, expected_page_count=receipt.target_page_count_before)
        snapshot = self._snapshot_document_structure(path)
        if snapshot != receipt.target_after_snapshot:
            raise PdfPageMutationError("ページ置換の再適用検証に失敗しました")
        self._render_pages(path, receipt.target_page_indexes)
        self._validate_inserted_page_render_equivalence(
            source_path=receipt.source_snapshot_path,
            source_page_indexes=receipt.source_page_indexes,
            imported_path=path,
            imported_page_indexes=receipt.target_page_indexes,
        )

    def _render_page_digests(
        self,
        path: Path,
        page_indexes: list[int] | tuple[int, ...],
    ) -> dict[int, tuple[int, int, str]]:
        document: Any | None = None
        results: dict[int, tuple[int, int, str]] = {}
        try:
            document = pdfium.PdfDocument(str(path))
            for page_index in page_indexes:
                page = document[page_index]
                bitmap: Any | None = None
                image: Any | None = None
                try:
                    bitmap = page.render(scale=0.2)
                    image = bitmap.to_pil().convert("RGBA")
                    if image.width <= 0 or image.height <= 0:
                        raise PdfPageMutationError("更新後ページの描画検証に失敗しました")
                    results[page_index] = (
                        image.width,
                        image.height,
                        sha256(image.tobytes()).hexdigest(),
                    )
                except PdfPageMutationError:
                    raise
                except Exception as exc:
                    raise PdfPageMutationError("更新後ページの描画検証に失敗しました") from exc
                finally:
                    if image is not None:
                        image.close()
                    if bitmap is not None and hasattr(bitmap, "close"):
                        bitmap.close()
                    page.close()
        except PdfPageMutationError:
            raise
        except Exception as exc:
            raise PdfPageMutationError("更新後ページの描画検証に失敗しました") from exc
        finally:
            if document is not None:
                document.close()
        return results

    def _validate_current_duplication_state(
        self,
        path: Path,
        receipt: PageDuplicationReceipt,
    ) -> None:
        try:
            current_snapshot = self._snapshot_document_structure(path)
            expected_page_count = receipt.original_page_count + len(receipt.source_page_indexes)
            if current_snapshot.page_count != expected_page_count:
                raise PdfPageMutationError("複製済みページの状態が変化しているため元に戻せません")
            self._validate_document_level_snapshots(
                current_snapshot,
                receipt.before_snapshot,
                page_mapping=self._build_execute_transition(
                    receipt.original_page_count,
                    receipt.source_page_indexes,
                ).cache_old_to_new,
            )
            self._validate_duplicate_page_layout(current_snapshot, receipt)
        except PdfPageMutationError as exc:
            if "元に戻せません" in str(exc):
                raise
            raise PdfPageMutationError(
                "複製済みページの状態が変化しているため元に戻せません"
            ) from exc

    def validate_duplication_redo_precondition(
        self,
        path: Path,
        receipt: PageDuplicationReceipt,
    ) -> None:
        resolved_path = path.expanduser().resolve()
        current_snapshot = self._snapshot_document_structure(resolved_path)
        if current_snapshot != receipt.before_snapshot:
            raise PdfPageMutationError("複製対象ページの前提状態が変化しました")
        self._reject_unsupported_forms(resolved_path, receipt.source_page_indexes)

    def _validate_current_reordering_state(
        self,
        path: Path,
        receipt: PageReorderReceipt,
    ) -> None:
        current_snapshot = self._snapshot_document_structure(path)
        if current_snapshot != receipt.after_snapshot:
            raise PdfPageMutationError("並べ替え済みページの状態が変化しているため元に戻せません")

    def _build_delete_execute_transition(
        self,
        original_page_count: int,
        deleted_page_indexes: tuple[int, ...],
    ) -> PageIndexTransition:
        deleted_set = set(deleted_page_indexes)
        cache_mapping: list[int | None] = []
        survivor_new_indexes: dict[int, int] = {}
        deleted_before = 0
        for page_index in range(original_page_count):
            if page_index in deleted_set:
                cache_mapping.append(None)
                deleted_before += 1
                continue
            new_index = page_index - deleted_before
            survivor_new_indexes[page_index] = new_index
            cache_mapping.append(new_index)

        survivor_indexes = sorted(survivor_new_indexes)
        current_mapping: list[int | None] = []
        for page_index in range(original_page_count):
            mapped_page = survivor_new_indexes.get(page_index)
            if mapped_page is not None:
                current_mapping.append(mapped_page)
                continue
            right_survivor = next(
                (index for index in survivor_indexes if index > page_index),
                None,
            )
            if right_survivor is not None:
                current_mapping.append(survivor_new_indexes[right_survivor])
                continue
            left_survivor = next(
                index for index in reversed(survivor_indexes) if index < page_index
            )
            current_mapping.append(survivor_new_indexes[left_survivor])

        return PageIndexTransition(
            old_page_count=original_page_count,
            new_page_count=original_page_count - len(deleted_page_indexes),
            cache_old_to_new=tuple(cache_mapping),
            current_page_old_to_new=tuple(current_mapping),
        )

    def _build_delete_undo_transition(self, receipt: PageDeletionReceipt) -> PageIndexTransition:
        survivor_indexes = receipt.survivor_original_indexes
        return PageIndexTransition(
            old_page_count=receipt.after_snapshot.page_count,
            new_page_count=receipt.original_page_count,
            cache_old_to_new=survivor_indexes,
            current_page_old_to_new=survivor_indexes,
        )

    def _build_reorder_execute_transition(self, plan: PageReorderPlan) -> PageIndexTransition:
        return PageIndexTransition(
            old_page_count=plan.page_count,
            new_page_count=plan.page_count,
            cache_old_to_new=plan.old_to_new,
            current_page_old_to_new=plan.old_to_new,
        )

    def _build_insert_execute_transition(self, plan: PageInsertionPlan) -> PageIndexTransition:
        return PageIndexTransition(
            old_page_count=plan.target_page_count,
            new_page_count=plan.target_page_count + len(plan.source_page_indexes),
            cache_old_to_new=plan.target_old_to_new,
            current_page_old_to_new=plan.target_old_to_new,
        )

    def _build_insert_undo_transition(self, plan: PageInsertionPlan) -> PageIndexTransition:
        inserted_set = set(plan.inserted_page_indexes_after)
        cache_mapping: list[int | None] = []
        for page_index in range(plan.target_page_count + len(plan.source_page_indexes)):
            if page_index in inserted_set:
                cache_mapping.append(None)
                continue
            if page_index < plan.insertion_slot:
                cache_mapping.append(page_index)
            else:
                cache_mapping.append(page_index - len(plan.source_page_indexes))
        return PageIndexTransition(
            old_page_count=plan.target_page_count + len(plan.source_page_indexes),
            new_page_count=plan.target_page_count,
            cache_old_to_new=tuple(cache_mapping),
            current_page_old_to_new=tuple(cache_mapping),
        )

    def _build_replace_execute_transition(
        self,
        plan: PageReplacementPlan,
    ) -> PageIndexTransition:
        return PageIndexTransition(
            old_page_count=plan.target_page_count,
            new_page_count=plan.target_page_count,
            cache_old_to_new=plan.execute_cache_old_to_new,
            current_page_old_to_new=plan.execute_current_page_old_to_new,
        )

    def _build_replace_undo_transition(
        self,
        plan: PageReplacementPlan,
    ) -> PageIndexTransition:
        return PageIndexTransition(
            old_page_count=plan.target_page_count,
            new_page_count=plan.target_page_count,
            cache_old_to_new=plan.execute_cache_old_to_new,
            current_page_old_to_new=plan.execute_current_page_old_to_new,
        )

    def _build_crop_transition(
        self,
        page_count: int,
        changed_page_indexes: tuple[int, ...],
    ) -> PageIndexTransition:
        changed_set = set(changed_page_indexes)
        cache_mapping = tuple(
            None if page_index in changed_set else page_index for page_index in range(page_count)
        )
        identity_mapping = tuple(range(page_count))
        return PageIndexTransition(
            old_page_count=page_count,
            new_page_count=page_count,
            cache_old_to_new=cache_mapping,
            current_page_old_to_new=identity_mapping,
        )

    def _build_reorder_undo_transition(self, receipt: PageReorderReceipt) -> PageIndexTransition:
        return PageIndexTransition(
            old_page_count=receipt.original_page_count,
            new_page_count=receipt.original_page_count,
            cache_old_to_new=receipt.target_order,
            current_page_old_to_new=receipt.target_order,
        )

    def _build_execute_transition(
        self,
        original_page_count: int,
        source_page_indexes: tuple[int, ...],
    ) -> PageIndexTransition:
        original_indexes_after = _all_original_indexes_after(
            original_page_count,
            source_page_indexes,
        )
        selected_set = set(source_page_indexes)
        current_page_old_to_new = tuple(
            original_indexes_after[page_index] + 1
            if page_index in selected_set
            else original_indexes_after[page_index]
            for page_index in range(original_page_count)
        )
        return PageIndexTransition(
            old_page_count=original_page_count,
            new_page_count=original_page_count + len(source_page_indexes),
            cache_old_to_new=original_indexes_after,
            current_page_old_to_new=current_page_old_to_new,
        )

    def _build_undo_transition(self, receipt: PageDuplicationReceipt) -> PageIndexTransition:
        original_indexes_after = _all_original_indexes_after(
            receipt.original_page_count,
            receipt.source_page_indexes,
        )
        original_index_by_current = {
            current_index: original_index
            for original_index, current_index in enumerate(original_indexes_after)
        }
        source_index_by_duplicate = dict(
            zip(
                receipt.duplicate_page_indexes,
                receipt.source_page_indexes,
                strict=True,
            )
        )
        cache_mapping: list[int | None] = []
        current_mapping: list[int | None] = []
        current_page_count = receipt.original_page_count + len(receipt.source_page_indexes)
        for current_index in range(current_page_count):
            if current_index in source_index_by_duplicate:
                cache_mapping.append(None)
                current_mapping.append(source_index_by_duplicate[current_index])
                continue
            original_index = original_index_by_current.get(current_index)
            cache_mapping.append(original_index)
            current_mapping.append(original_index)
        return PageIndexTransition(
            old_page_count=current_page_count,
            new_page_count=receipt.original_page_count,
            cache_old_to_new=tuple(cache_mapping),
            current_page_old_to_new=tuple(current_mapping),
        )

    def _reorder_plan_from_receipt(self, receipt: PageReorderReceipt) -> PageReorderPlan:
        return PageReorderPlan(
            page_count=receipt.original_page_count,
            source_page_indexes=receipt.source_page_indexes,
            insertion_slot=receipt.insertion_slot,
            target_order=receipt.target_order,
            old_to_new=receipt.old_to_new,
            new_to_old=receipt.target_order,
            moved_page_indexes_after=receipt.moved_page_indexes_after,
        )

    def _reorder_affected_pages(self, plan: PageReorderPlan) -> frozenset[int]:
        return frozenset(
            page_index
            for page_index, mapped_page_index in enumerate(plan.old_to_new)
            if mapped_page_index != page_index
        )

    def _reorder_execute_render_page_indexes(self, plan: PageReorderPlan) -> tuple[int, ...]:
        changed_indexes = {
            new_page_index
            for new_page_index, original_page_index in enumerate(plan.target_order)
            if new_page_index != original_page_index
        }
        changed_indexes.update(plan.moved_page_indexes_after)
        if plan.moved_page_indexes_after:
            first = plan.moved_page_indexes_after[0]
            last = plan.moved_page_indexes_after[-1]
            if first > 0:
                changed_indexes.add(first - 1)
            if last + 1 < plan.page_count:
                changed_indexes.add(last + 1)
        return tuple(sorted(changed_indexes))

    def _reorder_undo_render_page_indexes(self, receipt: PageReorderReceipt) -> tuple[int, ...]:
        changed_indexes = {
            page_index
            for page_index, mapped_page_index in enumerate(receipt.old_to_new)
            if mapped_page_index != page_index
        }
        changed_indexes.update(receipt.source_page_indexes)
        if receipt.source_page_indexes:
            first = receipt.source_page_indexes[0]
            last = receipt.source_page_indexes[-1]
            if first > 0:
                changed_indexes.add(first - 1)
            if last + 1 < receipt.original_page_count:
                changed_indexes.add(last + 1)
        return tuple(sorted(changed_indexes))

    def _build_revision_from_candidate(
        self,
        candidate_path: Path,
        destination_path: Path,
    ) -> DocumentRevision:
        stat_result = candidate_path.stat()
        return DocumentRevision(
            resolved_path=str(destination_path.expanduser().resolve()),
            file_size=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
        )

    def _replace_atomically(self, source_path: Path, destination_path: Path) -> None:
        try:
            os.replace(source_path, destination_path)
        except OSError as exc:
            raise PdfPageMutationError("検証済みPDFの置換に失敗しました") from exc

    def _fsync_parent_directory(self, directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            directory_handle = os.open(directory, os.O_RDONLY)
        except OSError as exc:
            logger.warning("Failed to open parent directory for fsync: %s (%s)", directory, exc)
            return
        try:
            os.fsync(directory_handle)
        except OSError as exc:
            logger.warning(
                "Failed to fsync parent directory after page mutation: %s (%s)",
                directory,
                exc,
            )
        finally:
            os.close(directory_handle)

    def _cleanup_candidate(
        self,
        candidate_path: Path | None,
        *,
        primary_error: BaseException | None,
    ) -> None:
        if candidate_path is None or not candidate_path.exists():
            return
        try:
            candidate_path.unlink()
        except OSError as exc:
            if primary_error is None:
                logger.warning(
                    "Failed to remove candidate PDF after page mutation: %s (%s)",
                    candidate_path,
                    exc,
                )
            else:
                logger.warning(
                    "Failed to remove candidate PDF after page mutation error: "
                    "candidate=%s primary_error=%s cleanup_error=%s",
                    candidate_path,
                    type(primary_error).__name__,
                    exc,
                )

    def _cleanup_delete_undo_snapshot(
        self,
        snapshot_path: Path | None,
        *,
        primary_error: BaseException | None,
    ) -> None:
        if snapshot_path is None or not snapshot_path.exists():
            return
        try:
            snapshot_path.unlink()
        except OSError as exc:
            if primary_error is None:
                logger.warning(
                    "Failed to remove delete undo snapshot: %s (%s)",
                    snapshot_path,
                    exc,
                )
            else:
                logger.warning(
                    "Failed to remove delete undo snapshot after page deletion error: "
                    "snapshot=%s primary_error=%s cleanup_error=%s",
                    snapshot_path,
                    type(primary_error).__name__,
                    exc,
                )

    def _cleanup_insert_snapshot(
        self,
        snapshot_path: Path | None,
        *,
        primary_error: BaseException | None,
    ) -> None:
        if snapshot_path is None or not snapshot_path.exists():
            return
        try:
            snapshot_path.unlink()
        except OSError as exc:
            if primary_error is None:
                logger.warning(
                    "Failed to remove insertion snapshot: %s (%s)",
                    snapshot_path,
                    exc,
                )
            else:
                logger.warning(
                    "Failed to remove insertion snapshot after page insertion error: "
                    "snapshot=%s primary_error=%s cleanup_error=%s",
                    snapshot_path,
                    type(primary_error).__name__,
                    exc,
                )

    @staticmethod
    def _has_indirect_objgen(objgen: object) -> bool:
        return isinstance(objgen, tuple) and len(objgen) == 2 and objgen != (0, 0)

    @staticmethod
    def _dereference(value: Any) -> Any:
        if isinstance(value, pikepdf.Object):
            return value
        get_object = getattr(type(value), "get_object", None)
        if callable(get_object):
            return get_object(value)
        return value
