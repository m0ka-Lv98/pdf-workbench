from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

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
class PageMutationResult:
    old_revision: DocumentRevision
    new_revision: DocumentRevision
    page_count: int
    affected_pages: frozenset[int]


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
    ) -> PageMutationResult:
        if not states:
            raise ValueError("states must not be empty")
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None
        old_revision = DocumentRevision.from_path(resolved_path)

        try:
            writer = PdfWriter(clone_from=str(resolved_path))
            root_pages = cast(Any, writer._root_object["/Pages"]).get_object()
            page_objects = self._collect_page_objects(root_pages)
            page_count = len(page_objects)
            self._validate_page_indexes(tuple(state.page_index for state in states), page_count)
            self._validate_states(states)
            candidate_path = self._create_candidate_path(resolved_path)
            for state in states:
                page_object = cast(dict[object, object], page_objects[state.page_index])
                rotate_key = NameObject("/Rotate")
                if state.direct_rotate_present:
                    if state.direct_rotate_value is None:
                        raise PdfPageMutationError("回転値が不正です")
                    page_object[rotate_key] = NumberObject(state.direct_rotate_value)
                else:
                    page_object.pop(rotate_key, None)
            with candidate_path.open("wb") as output_stream:
                writer.write(output_stream)
            self._fsync_file(candidate_path)
            self._validate_candidate(
                candidate_path,
                expected_page_count=page_count,
                expected_states=states,
            )
            self._replace_atomically(candidate_path, resolved_path)
            self._fsync_parent_directory(resolved_path.parent)
            new_revision = DocumentRevision.from_path(resolved_path)
            return PageMutationResult(
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

    def _rotation_state_for_page(
        self,
        page_object: object,
        page_index: int,
    ) -> PageRotationState:
        typed_page_object = cast(dict[object, object], page_object)
        rotate_key = NameObject("/Rotate")
        direct_rotate_present = rotate_key in typed_page_object
        direct_rotate_value: int | None = None
        if direct_rotate_present:
            direct_rotate_value = self._normalize_rotation(
                typed_page_object[rotate_key],
                page_index=page_index,
                label="direct",
            )
            effective_rotation = direct_rotate_value
        else:
            effective_rotation = self._resolve_inherited_rotation(
                typed_page_object,
                page_index=page_index,
            )
        return PageRotationState(
            page_index=page_index,
            direct_rotate_present=direct_rotate_present,
            direct_rotate_value=direct_rotate_value,
            effective_rotation=effective_rotation,
        )

    def _resolve_inherited_rotation(self, page_object: object, *, page_index: int) -> int:
        current: Any = cast(dict[object, object], page_object)
        visited: set[int] = set()
        while current is not None:
            object_id = id(current)
            if object_id in visited:
                raise PdfPageRotationValidationError("ページツリーの回転継承を解決できません")
            visited.add(object_id)
            if getattr(current, "get", None) is None:
                break
            parent_reference = current.get("/Parent", None)
            if parent_reference is None:
                break
            parent = parent_reference.get_object()
            if parent is None:
                break
            rotate_key = NameObject("/Rotate")
            rotate = parent.get(rotate_key, None)
            if rotate is not None:
                return self._normalize_rotation(
                    rotate,
                    page_index=page_index,
                    label="inherited",
                )
            current = parent
        return 0

    def _normalize_rotation(
        self,
        value: object,
        *,
        page_index: int,
        label: str,
    ) -> int:
        try:
            rotation = int(cast(Any, value))
        except Exception as exc:
            raise PdfPageRotationValidationError(
                f"{page_index + 1}ページ目の{label}回転値が不正です"
            ) from exc
        if rotation % 90 != 0:
            raise PdfPageRotationValidationError(
                f"{page_index + 1}ページ目の{label}回転値は90度単位である必要があります"
            )
        normalized = rotation % 360
        if normalized not in {0, 90, 180, 270}:
            raise PdfPageRotationValidationError(
                f"{page_index + 1}ページ目の{label}回転値が不正です"
            )
        return int(normalized)

    def _collect_page_objects(self, pages_object: object) -> list[object]:
        page_objects: list[object] = []
        self._append_page_objects(cast(dict[object, object], pages_object), page_objects)
        return page_objects

    def _append_page_objects(self, node: dict[object, object], page_objects: list[object]) -> None:
        node_type = str(node.get(NameObject("/Type"), ""))
        if node_type == "/Page":
            page_objects.append(node)
            return
        kids = cast(list[object], node.get(NameObject("/Kids"), []))
        for kid in kids:
            child = cast(Any, kid).get_object()
            self._append_page_objects(cast(dict[object, object], child), page_objects)

    def _validate_states(self, states: tuple[PageRotationState, ...]) -> None:
        for state in states:
            if state.page_index < 0:
                raise ValueError("page indexes must be non-negative")
            if state.effective_rotation not in {0, 90, 180, 270}:
                raise PdfPageRotationValidationError("回転値が不正です")
            if state.direct_rotate_present:
                if state.direct_rotate_value is None:
                    raise PdfPageRotationValidationError("回転値が不正です")
                if state.direct_rotate_value not in {0, 90, 180, 270}:
                    raise PdfPageRotationValidationError("回転値が不正です")

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
    ) -> None:
        try:
            self._validator.validate(str(path), expected_page_count=expected_page_count)
        except PdfDocumentValidationError as exc:
            raise PdfPageMutationError(str(exc)) from exc
        candidate_states = self.read_rotation_states(
            path,
            tuple(state.page_index for state in expected_states),
        )
        if candidate_states != expected_states:
            raise PdfPageMutationError("更新後のページ回転検証に失敗しました")
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
