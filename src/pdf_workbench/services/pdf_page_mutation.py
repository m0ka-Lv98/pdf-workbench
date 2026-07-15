from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from numbers import Integral
from pathlib import Path
from typing import Any, cast

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

from pdf_workbench.domain.mutation import PageIndexTransition, WorkingCopyMutationResult
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


@dataclass(frozen=True, slots=True)
class PdfAnnotationStructureSnapshot:
    subtype: str
    rect: tuple[float, float, float, float]
    has_appearance: bool
    appearance_fingerprint: str | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PdfPageStructureSnapshot:
    content_fingerprint: str
    boxes: PageBoxState
    direct_rotate_present: bool
    direct_rotate_value: int | None
    effective_rotation: int
    annotations: tuple[PdfAnnotationStructureSnapshot, ...]


@dataclass(frozen=True, slots=True)
class PdfDocumentStructureSnapshot:
    page_count: int
    pages: tuple[PdfPageStructureSnapshot, ...]
    metadata_fingerprint: str
    outlines_fingerprint: str
    attachments_fingerprint: str


@dataclass(frozen=True, slots=True)
class PageDuplicationReceipt:
    original_page_count: int
    source_page_indexes: tuple[int, ...]
    original_page_indexes_after: tuple[int, ...]
    duplicate_page_indexes: tuple[int, ...]
    before_snapshot: PdfDocumentStructureSnapshot


@dataclass(frozen=True, slots=True)
class PageDuplicationMutation:
    mutation_result: WorkingCopyMutationResult
    receipt: PageDuplicationReceipt


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

    def duplicate_pages(
        self,
        path: Path,
        page_indexes: tuple[int, ...],
    ) -> PageDuplicationMutation:
        resolved_path = path.expanduser().resolve()
        candidate_path: Path | None = None
        primary_error: BaseException | None = None

        try:
            old_revision = DocumentRevision.from_path(resolved_path)
            before_snapshot = self._snapshot_document_structure(resolved_path)
            self._validate_page_indexes(page_indexes, before_snapshot.page_count)
            self._reject_unsupported_forms(resolved_path, page_indexes)
            original_indexes_after = self._all_original_indexes_after(
                before_snapshot.page_count,
                page_indexes,
            )
            duplicate_page_indexes = tuple(
                original_indexes_after[source_index] + 1 for source_index in page_indexes
            )
            receipt = PageDuplicationReceipt(
                original_page_count=before_snapshot.page_count,
                source_page_indexes=page_indexes,
                original_page_indexes_after=tuple(
                    original_indexes_after[source_index] for source_index in page_indexes
                ),
                duplicate_page_indexes=duplicate_page_indexes,
                before_snapshot=before_snapshot,
            )
            reader = PdfReader(str(resolved_path))
            writer = PdfWriter(clone_from=str(resolved_path))
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
                    page_count=before_snapshot.page_count + len(page_indexes),
                    affected_pages=frozenset(duplicate_page_indexes),
                    page_index_transition=self._build_execute_transition(
                        before_snapshot.page_count,
                        page_indexes,
                    ),
                ),
                receipt=receipt,
            )
        except PdfPageMutationError as exc:
            primary_error = exc
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
            writer = PdfWriter(clone_from=str(resolved_path))
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

    def _write_candidate(self, writer: PdfWriter, candidate_path: Path) -> None:
        try:
            with candidate_path.open("wb") as output_stream:
                writer.write(output_stream)
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
        self._validate_document_level_fingerprints(after_snapshot, receipt.before_snapshot)
        self._validate_duplicate_page_layout(after_snapshot, receipt)
        self._validate_duplicate_page_independence(path, receipt)
        self._validate_duplicate_renders(path, receipt)

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
            pages = tuple(
                self._page_structure_snapshot(pdf.pages[index], rotations[index])
                for index in range(page_count)
            )
        metadata_fingerprint = self._metadata_fingerprint(resolved_path)
        outlines_fingerprint = self._outlines_fingerprint(resolved_path)
        attachments_fingerprint = self._attachments_fingerprint(resolved_path)
        return PdfDocumentStructureSnapshot(
            page_count=page_count,
            pages=pages,
            metadata_fingerprint=metadata_fingerprint,
            outlines_fingerprint=outlines_fingerprint,
            attachments_fingerprint=attachments_fingerprint,
        )

    def _page_structure_snapshot(
        self,
        page: pikepdf.Page,
        rotation_state: PageRotationState,
    ) -> PdfPageStructureSnapshot:
        annotations = self._annotation_snapshots(page.obj.get("/Annots", None))
        return PdfPageStructureSnapshot(
            content_fingerprint=self._contents_fingerprint(page.obj.get("/Contents", None)),
            boxes=self._page_box_state(page),
            direct_rotate_present=rotation_state.direct_rotate_present,
            direct_rotate_value=rotation_state.direct_rotate_value,
            effective_rotation=rotation_state.effective_rotation,
            annotations=annotations,
        )

    def _annotation_snapshots(
        self,
        annots_object: object,
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
                    fingerprint=self._object_fingerprint(
                        annot,
                        exclude_keys=frozenset({"/P"}),
                    ),
                )
            )
        return tuple(snapshots)

    def _contents_fingerprint(self, contents: object) -> str:
        if contents is None:
            return "none"
        return self._object_fingerprint(contents)

    def _appearance_fingerprint(self, annot: Any) -> str | None:
        appearance = annot.get("/AP", None)
        if appearance is None:
            return None
        return self._object_fingerprint(appearance)

    def _object_fingerprint(
        self,
        value: object,
        *,
        exclude_keys: frozenset[str] = frozenset(),
    ) -> str:
        normalized = self._normalize_object(value, exclude_keys=exclude_keys, seen=set())
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
        seen: set[tuple[int, int]],
    ) -> object:
        dereferenced = self._dereference(value)
        objgen = getattr(dereferenced, "objgen", None)
        if objgen is not None:
            typed_objgen = cast(tuple[int, int], objgen)
            if typed_objgen in seen:
                return {"ref": [typed_objgen[0], typed_objgen[1]]}
            seen.add(typed_objgen)
        if isinstance(dereferenced, pikepdf.Stream):
            payload = {
                str(key): self._normalize_object(item, exclude_keys=exclude_keys, seen=seen)
                for key, item in dereferenced.items()
                if str(key) not in exclude_keys
            }
            payload["__stream_data__"] = sha256(dereferenced.read_bytes()).hexdigest()
            return payload
        if isinstance(dereferenced, pikepdf.Dictionary):
            return {
                str(key): self._normalize_object(item, exclude_keys=exclude_keys, seen=seen)
                for key, item in dereferenced.items()
                if str(key) not in exclude_keys
            }
        if isinstance(dereferenced, pikepdf.Array):
            return [
                self._normalize_object(item, exclude_keys=exclude_keys, seen=seen)
                for item in dereferenced
            ]
        if isinstance(dereferenced, bytes):
            return {"__bytes__": sha256(dereferenced).hexdigest()}
        if dereferenced is None or isinstance(dereferenced, (bool, int, float, str)):
            return dereferenced
        return str(dereferenced)

    def _metadata_fingerprint(self, path: Path) -> str:
        reader = PdfReader(str(path))
        metadata = reader.metadata
        payload: dict[str, object] = {}
        if metadata is not None:
            payload["info"] = {str(key): str(value) for key, value in metadata.items()}
        root = self._dereference(reader.trailer["/Root"])
        metadata_object = root.get("/Metadata", None)
        if metadata_object is not None:
            resolved_metadata = self._dereference(metadata_object)
            get_data = getattr(resolved_metadata, "get_data", None)
            if callable(get_data):
                payload["xmp_sha256"] = sha256(get_data()).hexdigest()
        return self._json_digest(payload)

    def _outlines_fingerprint(self, path: Path) -> str:
        reader = PdfReader(str(path))
        try:
            outline = reader.outline
        except Exception:
            return self._json_digest([])
        payload = self._normalize_outline(outline, reader)
        return self._json_digest(payload)

    def _normalize_outline(self, value: object, reader: PdfReader) -> object:
        if isinstance(value, list):
            return [self._normalize_outline(item, reader) for item in value]
        title = str(getattr(value, "title", value))
        page_number: int | None
        try:
            page_number = reader.get_destination_page_number(value)  # type: ignore[arg-type]
        except Exception:
            page_number = None
        return {
            "type": type(value).__name__,
            "title": title,
            "page": page_number,
        }

    def _attachments_fingerprint(self, path: Path) -> str:
        reader = PdfReader(str(path))
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

    def _apply_page_duplication_to_writer(
        self,
        writer: PdfWriter,
        reader: PdfReader,
        receipt: PageDuplicationReceipt,
    ) -> None:
        for inserted, source_page_index in enumerate(receipt.source_page_indexes):
            insertion_index = source_page_index + inserted + 1
            writer.insert_page(reader.pages[source_page_index], index=insertion_index)
            duplicated_page = writer.pages[insertion_index]
            self._set_duplicate_annotation_parent_references(duplicated_page)

    def _set_duplicate_annotation_parent_references(self, page: Any) -> None:
        annots = page.get("/Annots", None)
        page_reference = getattr(page, "indirect_reference", None)
        if annots is None or page_reference is None:
            return
        for annot_ref in annots:
            annot = self._dereference(annot_ref)
            annot[NameObject("/P")] = page_reference

    def _validate_document_level_fingerprints(
        self,
        current: PdfDocumentStructureSnapshot,
        before: PdfDocumentStructureSnapshot,
    ) -> None:
        if current.metadata_fingerprint != before.metadata_fingerprint:
            raise PdfPageMutationError("メタデータの検証に失敗しました")
        if current.outlines_fingerprint != before.outlines_fingerprint:
            raise PdfPageMutationError("アウトラインの検証に失敗しました")
        if current.attachments_fingerprint != before.attachments_fingerprint:
            raise PdfPageMutationError("添付ファイルの検証に失敗しました")

    def _validate_duplicate_page_layout(
        self,
        after_snapshot: PdfDocumentStructureSnapshot,
        receipt: PageDuplicationReceipt,
    ) -> None:
        all_original_indexes_after = self._all_original_indexes_after(
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
            source_page = receipt.before_snapshot.pages[source_page_index]
            if duplicate_page != source_page:
                raise PdfPageMutationError("複製ページの構造検証に失敗しました")

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

    def _render_pages(self, path: Path, page_indexes: tuple[int, ...]) -> None:
        self._render_page_digests(path, page_indexes)

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
            self._validate_document_level_fingerprints(current_snapshot, receipt.before_snapshot)
            self._validate_duplicate_page_layout(current_snapshot, receipt)
        except PdfPageMutationError as exc:
            if "元に戻せません" in str(exc):
                raise
            raise PdfPageMutationError(
                "複製済みページの状態が変化しているため元に戻せません"
            ) from exc

    @staticmethod
    def _all_original_indexes_after(
        original_page_count: int,
        source_page_indexes: tuple[int, ...],
    ) -> tuple[int, ...]:
        return tuple(
            page_index + sum(1 for source_index in source_page_indexes if source_index < page_index)
            for page_index in range(original_page_count)
        )

    def _build_execute_transition(
        self,
        original_page_count: int,
        source_page_indexes: tuple[int, ...],
    ) -> PageIndexTransition:
        original_indexes_after = self._all_original_indexes_after(
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
        original_indexes_after = self._all_original_indexes_after(
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
