from __future__ import annotations

import logging
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pikepdf

from pdf_workbench.domain.document_session import FileFingerprint
from pdf_workbench.domain.page_extraction import PageExtractionPlan
from pdf_workbench.services.pdf_document_validator import (
    PdfDocumentValidationError,
    PdfDocumentValidator,
)
from pdf_workbench.services.pdf_page_mutation import (
    SUPPORTED_IMPORTED_ANNOTATION_SUBTYPES,
    PdfDocumentStructureSnapshot,
    PdfPageMutationService,
    PdfPageStructureSnapshot,
    SourcePdfRevision,
)
from pdf_workbench.services.pdf_save_service import TargetChangedError, TargetSnapshot

logger = logging.getLogger(__name__)

_PROHIBITED_EXTRACT_ANNOTATION_KEYS = frozenset(
    {
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
    }
)
_ALLOWED_EXTRACT_PAGE_KEYS = frozenset(
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


class PdfPageExportError(RuntimeError):
    """Raised when pages cannot be exported to an independent PDF."""


class PdfPageExportValidationError(PdfPageExportError):
    """Raised when an export candidate fails structural or render validation."""


@dataclass(frozen=True, slots=True)
class PageExtractionResult:
    target_path: Path
    fingerprint: FileFingerprint
    exported_page_count: int
    exported_at: datetime


class PdfPageExportService:
    def __init__(
        self,
        *,
        validator: PdfDocumentValidator | None = None,
        mutation_service: PdfPageMutationService | None = None,
    ) -> None:
        self._validator = validator if validator is not None else PdfDocumentValidator()
        self._mutation_service = (
            mutation_service if mutation_service is not None else PdfPageMutationService()
        )

    def read_source_pdf_revision(self, path: Path) -> SourcePdfRevision:
        return self._mutation_service.read_source_pdf_revision(path)

    def extract_pages(
        self,
        source_path: Path,
        target_path: Path,
        plan: PageExtractionPlan,
        *,
        working_copy_path: Path | None = None,
        expected_source_revision: SourcePdfRevision | None = None,
        expected_target_snapshot: TargetSnapshot | None = None,
    ) -> PageExtractionResult:
        resolved_source = source_path.expanduser().resolve()
        resolved_target = target_path.expanduser().resolve()
        resolved_working_copy = (
            working_copy_path.expanduser().resolve() if working_copy_path is not None else None
        )
        candidate_path: Path | None = None
        primary_error: BaseException | None = None
        exported_at = datetime.now(UTC)
        try:
            self._ensure_paths_are_allowed(
                resolved_source,
                resolved_target,
                resolved_working_copy,
            )
            self._ensure_target_directory(resolved_target.parent)
            source_revision = self._read_and_validate_source_revision(
                resolved_source,
                expected_source_revision,
                plan,
            )
            source_snapshot = self._mutation_service.snapshot_document_structure(resolved_source)
            self._reject_unsupported_source_structures(resolved_source, plan)
            target_snapshot = (
                expected_target_snapshot
                if expected_target_snapshot is not None
                else TargetSnapshot.capture(resolved_target)
            )
            candidate_path = self._create_temp_output_path(resolved_target)
            self._write_candidate(resolved_source, candidate_path, plan)
            self._fsync_file(candidate_path)
            candidate_snapshot = self._validate_candidate(
                candidate_path,
                source_snapshot,
                plan,
            )
            self._validate_candidate_render(candidate_path, plan.output_page_count)
            self._ensure_no_metadata_or_document_navigation(candidate_snapshot)
            self._ensure_source_revision_unchanged(resolved_source, source_revision)
            self._ensure_target_snapshot_matches(resolved_target, target_snapshot)
            self._apply_existing_target_mode(candidate_path, resolved_target)
            self._ensure_target_snapshot_matches(resolved_target, target_snapshot)
            fingerprint = FileFingerprint.from_path(candidate_path)
            self._replace_atomically(candidate_path, resolved_target)
            self._fsync_parent_directory(resolved_target.parent)
            return PageExtractionResult(
                target_path=resolved_target,
                fingerprint=fingerprint,
                exported_page_count=plan.output_page_count,
                exported_at=exported_at,
            )
        except (PdfPageExportError, TargetChangedError) as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfPageExportError("PDF抽出の保存準備に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def _read_and_validate_source_revision(
        self,
        source_path: Path,
        expected_revision: SourcePdfRevision | None,
        plan: PageExtractionPlan,
    ) -> SourcePdfRevision:
        try:
            revision = self._mutation_service.read_source_pdf_revision(source_path)
        except Exception as exc:
            raise PdfPageExportError("抽出元PDFの状態を確認できませんでした") from exc
        if revision.page_count != plan.page_count:
            raise PdfPageExportError("抽出元PDFのページ数が変更されました")
        if expected_revision is not None and revision != expected_revision:
            raise PdfPageExportError("抽出元PDFが変更されたため、抽出を中止しました")
        return revision

    def _ensure_source_revision_unchanged(
        self,
        source_path: Path,
        expected_revision: SourcePdfRevision,
    ) -> None:
        try:
            current = self._mutation_service.read_source_pdf_revision(source_path)
        except Exception as exc:
            raise PdfPageExportError("抽出元PDFの状態を再確認できませんでした") from exc
        if current != expected_revision:
            raise PdfPageExportError("抽出中に抽出元PDFが変更されました")

    @staticmethod
    def _ensure_paths_are_allowed(
        source_path: Path,
        target_path: Path,
        working_copy_path: Path | None,
    ) -> None:
        if source_path == target_path:
            raise PdfPageExportError("抽出元PDFと同じ場所には出力できません")
        if working_copy_path is not None and target_path == working_copy_path:
            raise PdfPageExportError("現在の作業コピーには出力できません")

    @staticmethod
    def _ensure_target_directory(directory: Path) -> None:
        if not directory.exists():
            raise PdfPageExportError("出力先フォルダが存在しません")
        if not directory.is_dir():
            raise PdfPageExportError("出力先フォルダが不正です")
        if not os.access(directory, os.W_OK):
            raise PdfPageExportError("出力先フォルダに書き込めません")

    @staticmethod
    def _create_temp_output_path(target_path: Path) -> Path:
        file_descriptor, temp_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=f".{target_path.stem}.",
            suffix=".extract.tmp.pdf",
        )
        os.close(file_descriptor)
        return Path(temp_name)

    def _reject_unsupported_source_structures(
        self,
        source_path: Path,
        plan: PageExtractionPlan,
    ) -> None:
        try:
            with pikepdf.open(source_path) as pdf:
                root = pdf.Root
                if len(pdf.pages) <= 0:
                    raise PdfPageExportError("0ページのPDFは抽出できません")
                for key, message in (
                    ("/AcroForm", "フォームを含むPDFからの抽出は未対応です"),
                    ("/StructTreeRoot", "タグ付きPDFからの抽出は未対応です"),
                    ("/PageLabels", "PageLabelsを含むPDFからの抽出は未対応です"),
                    ("/Threads", "Article Threadsを含むPDFからの抽出は未対応です"),
                    ("/OpenAction", "OpenActionを含むPDFからの抽出は未対応です"),
                ):
                    if key in root:
                        raise PdfPageExportError(message)
                page_objgens = {
                    page.obj.objgen
                    for page in pdf.pages
                    if self._has_indirect_objgen(getattr(page.obj, "objgen", None))
                }
                for page_index in plan.source_page_indexes:
                    page = pdf.pages[page_index]
                    self._validate_supported_page_keys(page.obj)
                    self._validate_page_annotations(
                        page.obj,
                        source_page_objgens=page_objgens,
                    )
        except PdfPageExportError:
            raise
        except Exception as exc:
            raise PdfPageExportError("抽出元PDFの構造検証に失敗しました") from exc

    @staticmethod
    def _validate_supported_page_keys(page_object: pikepdf.Dictionary) -> None:
        for key_object in page_object:
            key = str(key_object)
            if key in _ALLOWED_EXTRACT_PAGE_KEYS:
                continue
            if key == "/B":
                raise PdfPageExportError("Article beadを含むページの抽出は未対応です")
            if key == "/AA":
                raise PdfPageExportError("ページアクションを含むページの抽出は未対応です")
            raise PdfPageExportError(f"抽出元PDFのページに未対応の{key}があります")

    def _validate_page_annotations(
        self,
        page_object: pikepdf.Dictionary,
        *,
        source_page_objgens: set[tuple[int, int]],
    ) -> None:
        annots_object = page_object.get("/Annots", None)
        if annots_object is None:
            return
        annots = self._dereference(annots_object)
        if not isinstance(annots, pikepdf.Array):
            raise PdfPageExportError("注釈配列が不正です")
        for annot_ref in annots:
            annot = self._dereference(annot_ref)
            if not isinstance(annot, pikepdf.Dictionary):
                raise PdfPageExportError("注釈構造が不正です")
            subtype = str(annot.get("/Subtype", ""))
            if subtype == "/Widget":
                raise PdfPageExportError("Widget注釈を含むページの抽出は未対応です")
            if subtype not in SUPPORTED_IMPORTED_ANNOTATION_SUBTYPES:
                raise PdfPageExportError(f"{subtype or '不明な'}注釈を含むページの抽出は未対応です")
            prohibited = next(
                (key for key in _PROHIBITED_EXTRACT_ANNOTATION_KEYS if key in annot),
                None,
            )
            if prohibited is not None:
                raise PdfPageExportError("annotation actionまたは外部依存を含む抽出は未対応です")
            parent = annot.get("/P", None)
            if parent is not None:
                parent_obj = self._dereference(parent)
                parent_objgen = getattr(parent_obj, "objgen", None)
                own_objgen = getattr(page_object, "objgen", None)
                if (
                    not self._has_indirect_objgen(parent_objgen)
                    or parent_objgen not in source_page_objgens
                    or parent_objgen != own_objgen
                ):
                    raise PdfPageExportError("他ページを参照する注釈を含む抽出は未対応です")

    def _write_candidate(
        self,
        source_path: Path,
        candidate_path: Path,
        plan: PageExtractionPlan,
    ) -> None:
        try:
            with pikepdf.open(source_path) as source_pdf:
                output_pdf = pikepdf.Pdf.new()
                try:
                    for source_page_index in plan.source_page_indexes:
                        output_pdf.pages.append(source_pdf.pages[source_page_index])
                        output_page = output_pdf.pages[-1]
                        self._rewrite_annotation_parents(output_page)
                    output_pdf.save(candidate_path)
                finally:
                    output_pdf.close()
        except PdfPageExportError:
            raise
        except Exception as exc:
            raise PdfPageExportError("抽出PDF候補の作成に失敗しました") from exc

    def _rewrite_annotation_parents(self, page: pikepdf.Page) -> None:
        annots = page.obj.get("/Annots", None)
        if annots is None:
            return
        annots_array = self._dereference(annots)
        if not isinstance(annots_array, pikepdf.Array):
            raise PdfPageExportError("注釈配列のコピーに失敗しました")
        for annot_ref in annots_array:
            annot = self._dereference(annot_ref)
            if not isinstance(annot, pikepdf.Dictionary):
                raise PdfPageExportError("注釈オブジェクトのコピーに失敗しました")
            annot[pikepdf.Name("/P")] = page.obj

    def _validate_candidate(
        self,
        candidate_path: Path,
        source_snapshot: PdfDocumentStructureSnapshot,
        plan: PageExtractionPlan,
    ) -> PdfDocumentStructureSnapshot:
        try:
            candidate_snapshot = self._mutation_service.snapshot_document_structure(candidate_path)
        except Exception as exc:
            raise PdfPageExportValidationError("抽出PDF候補の構造検証に失敗しました") from exc
        if candidate_snapshot.page_count != plan.output_page_count:
            raise PdfPageExportValidationError("抽出PDF候補のページ数が一致しません")
        for output_page_index, source_page_index in enumerate(plan.source_page_indexes):
            if not self._page_snapshot_matches_extracted_source(
                candidate_snapshot.pages[output_page_index],
                source_snapshot.pages[source_page_index],
            ):
                raise PdfPageExportValidationError("抽出PDF候補のページ構造が一致しません")
        return candidate_snapshot

    @staticmethod
    def _page_snapshot_matches_extracted_source(
        candidate_page: PdfPageStructureSnapshot,
        source_page: PdfPageStructureSnapshot,
    ) -> bool:
        for field_name in (
            "content_fingerprint",
            "boxes",
            "direct_resources_present",
            "resources_fingerprint",
            "direct_rotate_present",
            "direct_rotate_value",
            "effective_rotation",
            "direct_page_keys",
            "extra_page_entries_fingerprint",
        ):
            if getattr(candidate_page, field_name) != getattr(source_page, field_name):
                return False
        source_annotations = source_page.annotations
        candidate_annotations = candidate_page.annotations
        if len(candidate_annotations) != len(source_annotations):
            return False
        for candidate_annotation, source_annotation in zip(
            candidate_annotations,
            source_annotations,
            strict=True,
        ):
            for field_name in (
                "subtype",
                "rect",
                "has_appearance",
                "appearance_fingerprint",
                "fingerprint",
            ):
                if getattr(candidate_annotation, field_name) != getattr(
                    source_annotation,
                    field_name,
                ):
                    return False
        return True

    def _validate_candidate_render(self, candidate_path: Path, page_count: int) -> None:
        try:
            self._validator.validate(str(candidate_path), expected_page_count=page_count)
        except PdfDocumentValidationError as exc:
            raise PdfPageExportValidationError(str(exc)) from exc

    @staticmethod
    def _ensure_no_metadata_or_document_navigation(
        candidate_snapshot: PdfDocumentStructureSnapshot,
    ) -> None:
        if candidate_snapshot.outlines:
            raise PdfPageExportValidationError("抽出PDF候補にbookmarkが残っています")
        if candidate_snapshot.named_destinations:
            raise PdfPageExportValidationError("抽出PDF候補にnamed destinationが残っています")

    @staticmethod
    def _ensure_target_snapshot_matches(
        target_path: Path,
        target_snapshot: TargetSnapshot,
    ) -> None:
        try:
            current_snapshot = TargetSnapshot.capture(target_path)
        except OSError as exc:
            raise TargetChangedError(
                "出力先の状態を再確認できなかったため、抽出を中止しました"
            ) from exc
        if current_snapshot != target_snapshot:
            raise TargetChangedError(
                "抽出中に出力先が別のプロセスで変更されたため、上書きを中止しました"
            )

    @staticmethod
    def _apply_existing_target_mode(candidate_path: Path, target_path: Path) -> None:
        if not target_path.exists() or os.name == "nt":
            return
        try:
            target_mode = stat.S_IMODE(target_path.stat().st_mode)
            os.chmod(candidate_path, target_mode)
        except OSError as exc:
            raise PdfPageExportError("出力先ファイル属性の適用に失敗しました") from exc

    @staticmethod
    def _replace_atomically(candidate_path: Path, target_path: Path) -> None:
        try:
            os.replace(candidate_path, target_path)
        except OSError as exc:
            raise PdfPageExportError("検証済み抽出PDFの置換に失敗しました") from exc

    @staticmethod
    def _fsync_file(path: Path) -> None:
        try:
            with path.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PdfPageExportError("抽出PDF候補の同期に失敗しました") from exc

    @staticmethod
    def _fsync_parent_directory(directory: Path) -> None:
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
                "Failed to fsync parent directory after extract: %s (%s)",
                directory,
                exc,
            )
        finally:
            os.close(directory_handle)

    @staticmethod
    def _cleanup_candidate(
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
                logger.warning("Failed to remove extract candidate: %s (%s)", candidate_path, exc)
            else:
                logger.warning(
                    "Failed to remove extract candidate after error: candidate=%s "
                    "primary_error=%s cleanup_error=%s",
                    candidate_path,
                    type(primary_error).__name__,
                    exc,
                )

    @staticmethod
    def _dereference(value: Any) -> Any:
        try:
            return value.get_object()
        except (AttributeError, ValueError):
            return value

    @staticmethod
    def _has_indirect_objgen(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 2
            and all(isinstance(item, int) for item in value)
            and value != (0, 0)
        )
