from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from numbers import Integral
from pathlib import Path
from typing import Any, cast

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

from pdf_workbench.domain.mutation import WorkingCopyMutationResult
from pdf_workbench.services.pdf_document_validator import (
    PdfDocumentValidationError,
    PdfDocumentValidator,
)
from pdf_workbench.services.pdf_renderer import DocumentRevision

logger = logging.getLogger(__name__)


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
class PageBoxState:
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float] | None
    trim_box: tuple[float, float, float, float] | None
    bleed_box: tuple[float, float, float, float] | None
    art_box: tuple[float, float, float, float] | None


class PdfPageMutationService:
    def __init__(self, validator: PdfDocumentValidator | None = None) -> None:
        self._validator = validator if validator is not None else PdfDocumentValidator()

    def read_rotation_states(
        self,
        path: Path,
        page_indexes: tuple[int, ...],
    ) -> tuple[PageRotationState, ...]:
        resolved_path = path.expanduser().resolve()
        try:
            reader = PdfReader(str(resolved_path))
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
            writer = PdfWriter(clone_from=str(resolved_path))
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
            with candidate_path.open("wb") as output_stream:
                writer.write(output_stream)

            self._fsync_file(candidate_path)
            self._validate_candidate(
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

    @staticmethod
    def _validate_page_indexes(page_indexes: tuple[int, ...], page_count: int) -> tuple[int, ...]:
        validated = tuple(page_indexes)
        if not validated:
            raise ValueError("page_indexes must not be empty")
        for page_index in validated:
            if page_index < 0:
                raise ValueError("page indexes must be non-negative")
            if page_index >= page_count:
                raise ValueError("page index is out of range")
        return validated

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

    def _page_box_state(self, page: pikepdf.Page) -> PageBoxState:
        return PageBoxState(
            media_box=self._normalize_box(page.mediabox),
            crop_box=self._optional_box(page.obj.get("/CropBox", None)),
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

    def _optional_box(self, value: object) -> tuple[float, float, float, float] | None:
        if value is None:
            return None
        return self._normalize_box(value)

    def _create_candidate_path(self, target_path: Path) -> Path:
        prefix = f".{target_path.stem}.mutation."
        file_descriptor, candidate_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=prefix,
            suffix=".tmp.pdf",
        )
        os.close(file_descriptor)
        return Path(candidate_name)

    def _fsync_file(self, path: Path) -> None:
        try:
            with path.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PdfPageMutationError("更新候補PDFの同期に失敗しました") from exc

    def _validate_candidate(
        self,
        path: Path,
        *,
        expected_page_count: int,
        expected_states: tuple[PageRotationState, ...],
        original_rotation_snapshot: tuple[PageRotationState, ...],
        original_box_snapshot: tuple[PageBoxState, ...],
    ) -> None:
        try:
            self._validator.validate(str(path), expected_page_count=expected_page_count)
        except PdfDocumentValidationError as exc:
            raise PdfPageMutationError(str(exc)) from exc

        with pikepdf.open(path) as pdf:
            pages = list(pdf.pages)
            if len(pages) != expected_page_count:
                raise PdfPageMutationError("更新後のページ数検証に失敗しました")
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

        self._render_affected_pages(path, expected_states)

    def _render_affected_pages(
        self,
        path: Path,
        states: tuple[PageRotationState, ...],
    ) -> None:
        document: Any | None = None
        try:
            document = pdfium.PdfDocument(str(path))
            for state in states:
                page = document[state.page_index]
                bitmap: Any | None = None
                image: Any | None = None
                try:
                    bitmap = page.render(scale=0.2)
                    image = bitmap.to_pil()
                    if image.width <= 0 or image.height <= 0:
                        raise PdfPageMutationError("更新後ページの描画検証に失敗しました")
                except PdfPageMutationError:
                    raise
                except Exception as exc:
                    raise PdfPageMutationError("更新後ページの描画検証に失敗しました") from exc
                finally:
                    if image is not None and hasattr(image, "close"):
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

    @staticmethod
    def _dereference(value: Any) -> Any:
        return value.get_object() if hasattr(value, "get_object") else value
