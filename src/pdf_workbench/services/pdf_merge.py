from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import pikepdf

from pdf_workbench.domain.document_session import FileFingerprint
from pdf_workbench.domain.pdf_merge import (
    PdfMergeBookmarkPolicy,
    PdfMergeInput,
    PdfMergeMetadataPolicy,
    PdfMergePlan,
)
from pdf_workbench.services.pdf_document_validator import (
    PdfDocumentValidationError,
    PdfDocumentValidator,
)
from pdf_workbench.services.pdf_page_import import PdfPageImportInspector
from pdf_workbench.services.pdf_page_mutation import (
    AnnotationParentState,
    PdfAnnotationStructureSnapshot,
    PdfDocumentStructureSnapshot,
    PdfPageMutationError,
    PdfPageMutationService,
    PdfPageStructureSnapshot,
    SourcePdfRevision,
)
from pdf_workbench.services.pdf_save_service import TargetChangedError, TargetSnapshot

logger = logging.getLogger(__name__)

_COPIED_METADATA_KEYS = frozenset(
    {
        "/Title",
        "/Author",
        "/Subject",
        "/Keywords",
        "/Creator",
    }
)


class PdfMergeStage(StrEnum):
    VALIDATING = "validating"
    SNAPSHOTTING = "snapshotting"
    IMPORTING = "importing"
    METADATA = "metadata"
    BOOKMARKS = "bookmarks"
    VALIDATING_OUTPUT = "validating_output"
    REPLACING = "replacing"
    COMPLETE = "complete"


class PdfMergeError(RuntimeError):
    """Raised when PDF merge cannot complete safely."""


class PdfMergeCancelled(PdfMergeError):
    """Raised when the user cancels an in-progress merge."""


class SourcePdfChangedError(PdfMergeError):
    """Raised when one merge source changes during merge."""


@dataclass(frozen=True, slots=True)
class PdfMergeProgress:
    stage: PdfMergeStage
    input_number: int
    input_count: int
    filename: str
    source_page_count: int
    completed_output_pages: int
    total_output_pages: int
    message: str


@dataclass(frozen=True, slots=True)
class PdfMergeResultInput:
    label: str
    path: Path
    page_count: int
    display_range: str


@dataclass(frozen=True, slots=True)
class PdfMergeResult:
    target_path: Path
    fingerprint: FileFingerprint
    input_count: int
    total_page_count: int
    metadata_source_label: str | None
    bookmark_policy: PdfMergeBookmarkPolicy
    inputs: tuple[PdfMergeResultInput, ...]
    merged_at: datetime


@dataclass(frozen=True, slots=True)
class InspectedPdfMergeInput:
    merge_input: PdfMergeInput
    source_revision: SourcePdfRevision


@dataclass(frozen=True, slots=True)
class PdfMergeExpectedContext:
    source_revisions: Mapping[Path, SourcePdfRevision]
    target_snapshot: TargetSnapshot


@dataclass(frozen=True, slots=True)
class PdfMergeAnnotationSnapshot:
    subtype: str
    rect: tuple[float, float, float, float]
    has_appearance: bool
    appearance_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class PdfMergePageSnapshot:
    source_path: Path
    source_page_index: int
    output_page_index: int
    content_fingerprint: str
    resources_fingerprint: str
    boxes: object
    effective_rotation: int
    annotations: tuple[PdfMergeAnnotationSnapshot, ...]


class PdfMergeService:
    def __init__(
        self,
        *,
        validator: PdfDocumentValidator | None = None,
        mutation_service: PdfPageMutationService | None = None,
        import_inspector: PdfPageImportInspector | None = None,
    ) -> None:
        self._validator = validator if validator is not None else PdfDocumentValidator()
        self._mutation_service = (
            mutation_service if mutation_service is not None else PdfPageMutationService()
        )
        self._import_inspector = (
            import_inspector if import_inspector is not None else PdfPageImportInspector()
        )

    def read_source_pdf_revision(self, path: Path) -> SourcePdfRevision:
        return self._mutation_service.read_source_pdf_revision(path)

    def read_merge_input(self, path: Path) -> PdfMergeInput:
        resolved_path = path.expanduser().resolve()
        if not resolved_path.exists() or not resolved_path.is_file():
            raise PdfMergeError("PDFファイルが存在しません")
        if resolved_path.suffix.lower() != ".pdf":
            raise PdfMergeError("PDFファイルを選択してください")
        revision = self.read_source_pdf_revision(resolved_path)
        return PdfMergeInput(
            path=resolved_path,
            page_count=revision.page_count,
            label=resolved_path.name,
        )

    def inspect_merge_input(self, path: Path) -> InspectedPdfMergeInput:
        resolved_path = path.expanduser().resolve()
        revision = self.read_source_pdf_revision(resolved_path)
        return InspectedPdfMergeInput(
            merge_input=PdfMergeInput(
                path=resolved_path,
                page_count=revision.page_count,
                label=resolved_path.name,
            ),
            source_revision=revision,
        )

    def merge_pdfs(
        self,
        plan: PdfMergePlan,
        *,
        overwrite: bool = False,
        expected_source_revisions: Mapping[Path, SourcePdfRevision] | None = None,
        expected_target_snapshot: TargetSnapshot | None = None,
        is_managed_path: Callable[[Path], bool] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        progress_callback: Callable[[PdfMergeProgress], None] | None = None,
    ) -> PdfMergeResult:
        candidate_path: Path | None = None
        source_snapshot_path: Path | None = None
        primary_error: BaseException | None = None
        expected_pages: list[PdfMergePageSnapshot] = []
        merged_at = datetime.now(UTC)
        try:
            self._check_cancelled(should_cancel)
            self._preflight(plan, overwrite=overwrite, is_managed_path=is_managed_path)
            source_revisions = self._read_and_validate_source_revisions(
                plan,
                expected_source_revisions,
            )
            target_snapshot = (
                expected_target_snapshot
                if expected_target_snapshot is not None
                else TargetSnapshot.capture(plan.output_path)
            )
            self._ensure_target_policy(plan.output_path, target_snapshot, overwrite=overwrite)
            self._ensure_target_snapshot_matches(plan.output_path, target_snapshot)
            candidate_path = self._create_temp_output_path(plan.output_path)
            self._emit_progress(progress_callback, plan, PdfMergeStage.VALIDATING, 0, "")
            with pikepdf.Pdf.new() as output_pdf:
                completed_pages = 0
                metadata_source_docinfo: dict[str, str] = {}
                source_group_titles = self._unique_group_titles(plan.inputs)
                for index, input_item in enumerate(plan.inputs):
                    self._check_cancelled(should_cancel)
                    self._emit_progress(
                        progress_callback,
                        plan,
                        PdfMergeStage.SNAPSHOTTING,
                        index,
                        input_item.label,
                        completed_pages=completed_pages,
                    )
                    self._ensure_source_revision_unchanged(
                        input_item.path,
                        source_revisions[input_item.path],
                    )
                    source_snapshot_path = self._create_source_snapshot(
                        input_item.path,
                        snapshot_directory=plan.output_path.parent,
                    )
                    self._ensure_snapshot_matches_revision(
                        source_snapshot_path,
                        source_revisions[input_item.path],
                    )
                    source_operation_error: BaseException | None = None
                    try:
                        self._check_cancelled(should_cancel)
                        self._emit_progress(
                            progress_callback,
                            plan,
                            PdfMergeStage.IMPORTING,
                            index,
                            input_item.label,
                            completed_pages=completed_pages,
                        )
                        with pikepdf.open(source_snapshot_path) as source_pdf:
                            self._import_inspector.reject_unsupported_document_structures(
                                source_pdf,
                                inspect_bookmarks=(
                                    plan.bookmark_policy is PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE
                                ),
                            )
                            if (
                                plan.metadata_policy is PdfMergeMetadataPolicy.SELECTED_SOURCE
                                and plan.metadata_source_path == input_item.path
                            ):
                                metadata_source_docinfo = self._extract_allowed_metadata(source_pdf)
                            output_start = len(output_pdf.pages)
                            source_structure = self._mutation_service.snapshot_document_structure(
                                source_snapshot_path
                            )
                            for page in source_pdf.pages:
                                source_page_index = len(output_pdf.pages) - output_start
                                expected_pages.append(
                                    self._merge_page_snapshot(
                                        source_structure.pages[source_page_index],
                                        source_path=input_item.path,
                                        source_page_index=source_page_index,
                                        output_page_index=len(output_pdf.pages),
                                    )
                                )
                                output_pdf.pages.append(page)
                                self._import_inspector.rewrite_annotation_parents(
                                    output_pdf.pages[-1]
                                )
                                completed_pages += 1
                            self._check_cancelled(should_cancel)
                            if plan.bookmark_policy is PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE:
                                self._copy_grouped_outline(
                                    source_pdf,
                                    output_pdf,
                                    group_title=source_group_titles[input_item.path],
                                    output_start_index=output_start,
                                )
                    except BaseException as exc:
                        source_operation_error = exc
                        raise
                    finally:
                        try:
                            self._cleanup_source_snapshot(
                                source_snapshot_path,
                                primary_error=source_operation_error,
                            )
                        except Exception as cleanup_exc:
                            if source_operation_error is None:
                                raise
                            logger.warning(
                                "Failed to cleanup source snapshot after primary error: "
                                "primary_error=%s cleanup_error=%s",
                                type(source_operation_error).__name__,
                                cleanup_exc,
                            )
                        source_snapshot_path = None
                self._check_cancelled(should_cancel)
                self._emit_progress(
                    progress_callback,
                    plan,
                    PdfMergeStage.METADATA,
                    len(plan.inputs) - 1,
                    "",
                    completed_pages=completed_pages,
                )
                if metadata_source_docinfo:
                    for key, value in metadata_source_docinfo.items():
                        output_pdf.docinfo[pikepdf.Name(key)] = pikepdf.String(value)
                self._emit_progress(
                    progress_callback,
                    plan,
                    PdfMergeStage.VALIDATING_OUTPUT,
                    len(plan.inputs) - 1,
                    "",
                    completed_pages=completed_pages,
                )
                output_pdf.save(candidate_path)
            self._fsync_file(candidate_path)
            self._validate_candidate(plan, candidate_path, expected_pages=tuple(expected_pages))
            for input_item in plan.inputs:
                self._ensure_source_revision_unchanged(
                    input_item.path,
                    source_revisions[input_item.path],
                )
            self._check_cancelled(should_cancel)
            self._ensure_target_snapshot_matches(plan.output_path, target_snapshot)
            self._apply_existing_target_mode(candidate_path, plan.output_path)
            self._ensure_target_snapshot_matches(plan.output_path, target_snapshot)
            fingerprint = FileFingerprint.from_path(candidate_path)
            self._emit_progress(
                progress_callback,
                plan,
                PdfMergeStage.REPLACING,
                len(plan.inputs) - 1,
                "",
                completed_pages=plan.total_page_count,
            )
            self._replace_atomically(candidate_path, plan.output_path)
            self._fsync_parent_directory(plan.output_path.parent)
            self._emit_progress(
                progress_callback,
                plan,
                PdfMergeStage.COMPLETE,
                len(plan.inputs) - 1,
                "",
                completed_pages=plan.total_page_count,
            )
            return PdfMergeResult(
                target_path=plan.output_path,
                fingerprint=fingerprint,
                input_count=len(plan.inputs),
                total_page_count=plan.total_page_count,
                metadata_source_label=self._metadata_source_label(plan),
                bookmark_policy=plan.bookmark_policy,
                inputs=tuple(
                    PdfMergeResultInput(
                        label=input_item.label,
                        path=input_item.path,
                        page_count=input_item.page_count,
                        display_range=output_range.display_range,
                    )
                    for input_item, output_range in zip(
                        plan.inputs,
                        plan.output_ranges,
                        strict=True,
                    )
                ),
                merged_at=merged_at,
            )
        except (PdfMergeError, TargetChangedError) as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfMergeError("PDF結合の保存準備に失敗しました") from exc
        finally:
            self._cleanup_source_snapshot(source_snapshot_path, primary_error=primary_error)
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    def _preflight(
        self,
        plan: PdfMergePlan,
        *,
        overwrite: bool,
        is_managed_path: Callable[[Path], bool] | None,
    ) -> None:
        output_parent = plan.output_path.parent
        if not output_parent.exists():
            raise PdfMergeError("出力先フォルダが存在しません")
        if not output_parent.is_dir():
            raise PdfMergeError("出力先フォルダが不正です")
        if not os.access(output_parent, os.W_OK):
            raise PdfMergeError("出力先フォルダに書き込めません")
        if is_managed_path is not None and is_managed_path(plan.output_path):
            raise PdfMergeError("アプリの一時作業フォルダ内には出力できません")
        for input_item in plan.inputs:
            if is_managed_path is not None and is_managed_path(input_item.path):
                raise PdfMergeError("アプリの一時作業フォルダ内のPDFは結合元にできません")
            if input_item.path == plan.output_path:
                raise PdfMergeError("結合元PDFと同じ場所には出力できません")
        if plan.output_path.exists() and not overwrite:
            raise PdfMergeError("出力先PDFが既に存在します")

    def _read_and_validate_source_revisions(
        self,
        plan: PdfMergePlan,
        expected_source_revisions: Mapping[Path, SourcePdfRevision] | None,
    ) -> dict[Path, SourcePdfRevision]:
        expected_by_path: dict[Path, SourcePdfRevision] | None = None
        plan_paths = {item.path for item in plan.inputs}
        if expected_source_revisions is not None:
            expected_by_path = {
                path.expanduser().resolve(): revision
                for path, revision in expected_source_revisions.items()
            }
            expected_paths = set(expected_by_path)
            missing_paths = plan_paths - expected_paths
            extra_paths = expected_paths - plan_paths
            if missing_paths:
                missing = ", ".join(sorted(path.name for path in missing_paths))
                raise PdfMergeError(f"結合元PDFの期待revisionが不足しています: {missing}")
            if extra_paths:
                extra = ", ".join(sorted(path.name for path in extra_paths))
                raise PdfMergeError(f"結合元PDFの期待revisionが余分です: {extra}")
        revisions: dict[Path, SourcePdfRevision] = {}
        for input_item in plan.inputs:
            revision = self.read_source_pdf_revision(input_item.path)
            if revision.page_count != input_item.page_count:
                raise SourcePdfChangedError(f"{input_item.label} のページ数が変更されました")
            expected_revision = expected_by_path[input_item.path] if expected_by_path else None
            if expected_revision is not None and revision != expected_revision:
                raise SourcePdfChangedError(
                    f"{input_item.label} が変更されたため結合を中止しました"
                )
            revisions[input_item.path] = revision
        return revisions

    def _ensure_source_revision_unchanged(
        self,
        source_path: Path,
        expected_revision: SourcePdfRevision,
    ) -> None:
        current = self.read_source_pdf_revision(source_path)
        if current != expected_revision:
            raise SourcePdfChangedError(f"{source_path.name} が結合中に変更されました")

    def _ensure_snapshot_matches_revision(
        self,
        snapshot_path: Path,
        expected_revision: SourcePdfRevision,
    ) -> None:
        snapshot_revision = self.read_source_pdf_revision(snapshot_path)
        if (
            snapshot_revision.page_count != expected_revision.page_count
            or snapshot_revision.sha256 != expected_revision.sha256
        ):
            raise SourcePdfChangedError("結合元PDFのsnapshot検証に失敗しました")

    @staticmethod
    def _ensure_target_policy(
        target_path: Path,
        target_snapshot: TargetSnapshot,
        *,
        overwrite: bool,
    ) -> None:
        if target_snapshot.exists and not overwrite:
            raise PdfMergeError("出力先PDFが既に存在します")
        if target_path.exists() != target_snapshot.exists:
            raise TargetChangedError("出力先の状態が変更されました")

    @staticmethod
    def _create_temp_output_path(target_path: Path) -> Path:
        file_descriptor, temp_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=f".{target_path.stem}.",
            suffix=".merge.tmp.pdf",
        )
        os.close(file_descriptor)
        return Path(temp_name)

    @staticmethod
    def _create_source_snapshot(source_path: Path, *, snapshot_directory: Path) -> Path:
        file_descriptor, temp_name = tempfile.mkstemp(
            dir=snapshot_directory,
            prefix=f".{source_path.stem}.",
            suffix=".merge-source.tmp.pdf",
        )
        snapshot_path = Path(temp_name)
        try:
            with os.fdopen(file_descriptor, "wb") as target:
                with source_path.open("rb") as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        except Exception:
            with suppress(OSError):
                os.close(file_descriptor)
            with suppress(OSError):
                snapshot_path.unlink()
            raise
        return snapshot_path

    def _validate_candidate(
        self,
        plan: PdfMergePlan,
        candidate_path: Path,
        *,
        expected_pages: tuple[PdfMergePageSnapshot, ...],
    ) -> None:
        if not candidate_path.exists() or candidate_path.stat().st_size <= 0:
            raise PdfMergeError("結合候補PDFが作成されていません")
        try:
            self._validator.validate(
                str(candidate_path),
                expected_page_count=plan.total_page_count,
                render_page_indexes=range(plan.total_page_count),
            )
        except PdfDocumentValidationError as exc:
            raise PdfMergeError(str(exc)) from exc
        try:
            with pikepdf.open(candidate_path) as pdf:
                if len(pdf.pages) != plan.total_page_count:
                    raise PdfMergeError("結合候補PDFのページ数が一致しません")
                self._reject_unsupported_candidate_root(pdf)
            candidate_structure = self._mutation_service.snapshot_document_structure(candidate_path)
            self._validate_candidate_pages(plan, candidate_structure, expected_pages)
            self._validate_candidate_metadata(plan, candidate_path)
            self._validate_candidate_bookmarks(plan, candidate_path)
        except PdfMergeError:
            raise
        except PdfPageMutationError as exc:
            raise PdfMergeError(str(exc)) from exc
        except Exception as exc:
            raise PdfMergeError("結合候補PDFの構造検証に失敗しました") from exc

    @staticmethod
    def _reject_unsupported_candidate_root(pdf: pikepdf.Pdf) -> None:
        root = pdf.Root
        for key in (
            "/Names",
            "/AcroForm",
            "/PageLabels",
            "/Threads",
            "/OpenAction",
            "/StructTreeRoot",
        ):
            if key in root:
                raise PdfMergeError(f"結合候補PDFに未対応の{key}が残っています")

    def _validate_candidate_pages(
        self,
        plan: PdfMergePlan,
        candidate_structure: PdfDocumentStructureSnapshot,
        expected_pages: tuple[PdfMergePageSnapshot, ...],
    ) -> None:
        if len(expected_pages) != plan.total_page_count:
            raise PdfMergeError("結合候補PDFのページ検証情報が不足しています")
        if candidate_structure.page_count != plan.total_page_count:
            raise PdfMergeError("結合候補PDFのページ数が一致しません")
        for expected, actual_page in zip(
            expected_pages,
            candidate_structure.pages,
            strict=True,
        ):
            actual = self._merge_page_snapshot(
                actual_page,
                source_path=expected.source_path,
                source_page_index=expected.source_page_index,
                output_page_index=expected.output_page_index,
            )
            if actual != expected:
                raise PdfMergeError(
                    f"結合候補PDFの{expected.output_page_index + 1}ページ目が"
                    "結合元PDFと一致しません"
                )
            for annotation in actual_page.annotations:
                if annotation.parent_state is not AnnotationParentState.POINTS_TO_OWN_PAGE:
                    raise PdfMergeError(
                        f"結合候補PDFの{expected.output_page_index + 1}ページ目の"
                        "annotation /P が自身のページを参照していません"
                    )

    @staticmethod
    def _merge_page_snapshot(
        page: PdfPageStructureSnapshot,
        *,
        source_path: Path,
        source_page_index: int,
        output_page_index: int,
    ) -> PdfMergePageSnapshot:
        return PdfMergePageSnapshot(
            source_path=source_path,
            source_page_index=source_page_index,
            output_page_index=output_page_index,
            content_fingerprint=page.content_fingerprint,
            resources_fingerprint=page.resources_fingerprint,
            boxes=page.boxes,
            effective_rotation=page.effective_rotation,
            annotations=tuple(
                PdfMergeService._merge_annotation_snapshot(annotation)
                for annotation in page.annotations
            ),
        )

    @staticmethod
    def _merge_annotation_snapshot(
        annotation: PdfAnnotationStructureSnapshot,
    ) -> PdfMergeAnnotationSnapshot:
        return PdfMergeAnnotationSnapshot(
            subtype=annotation.subtype,
            rect=annotation.rect,
            has_appearance=annotation.has_appearance,
            appearance_fingerprint=annotation.appearance_fingerprint,
        )

    def _validate_candidate_metadata(self, plan: PdfMergePlan, candidate_path: Path) -> None:
        with pikepdf.open(candidate_path) as pdf:
            docinfo = {str(key): str(value) for key, value in pdf.docinfo.items()}
            if "/Metadata" in pdf.Root:
                raise PdfMergeError("結合候補PDFにXMP metadataが残っています")
        copied = {key: value for key, value in docinfo.items() if key in _COPIED_METADATA_KEYS}
        if plan.metadata_policy is PdfMergeMetadataPolicy.NONE:
            if copied:
                raise PdfMergeError("結合候補PDFにsource metadataが残っています")
            custom_keys = set(docinfo) - {"/Producer", "/CreationDate", "/ModDate"}
            if custom_keys:
                raise PdfMergeError("結合候補PDFにcustom metadataが残っています")
            return
        if plan.metadata_source_path is None:
            raise PdfMergeError("metadata sourceが不正です")
        with pikepdf.open(plan.metadata_source_path) as source_pdf:
            expected = self._extract_allowed_metadata(source_pdf)
        if copied != expected:
            raise PdfMergeError("結合候補PDFのmetadataが選択元と一致しません")
        custom_keys = set(docinfo) - set(_COPIED_METADATA_KEYS) - {"/Producer"}
        if custom_keys:
            raise PdfMergeError("結合候補PDFに未許可のmetadataが残っています")

    def _validate_candidate_bookmarks(self, plan: PdfMergePlan, candidate_path: Path) -> None:
        structure = self._mutation_service.snapshot_document_structure(candidate_path)
        if structure.named_destinations:
            raise PdfMergeError("結合候補PDFにnamed destinationが残っています")
        if plan.bookmark_policy is PdfMergeBookmarkPolicy.NONE:
            if structure.outlines:
                raise PdfMergeError("結合候補PDFにbookmarkが残っています")
            return
        group_titles = self._unique_group_titles(plan.inputs)
        expected_group_titles = tuple(group_titles[item.path] for item in plan.inputs)
        actual_group_titles = tuple(item.title for item in structure.outlines)
        if actual_group_titles != expected_group_titles[: len(actual_group_titles)]:
            raise PdfMergeError("結合候補PDFのbookmark group順が一致しません")

    @staticmethod
    def _extract_allowed_metadata(pdf: pikepdf.Pdf) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for key, value in pdf.docinfo.items():
            key_text = str(key)
            if key_text not in _COPIED_METADATA_KEYS:
                continue
            metadata[key_text] = str(value)
        return metadata

    def _copy_grouped_outline(
        self,
        source_pdf: pikepdf.Pdf,
        output_pdf: pikepdf.Pdf,
        *,
        group_title: str,
        output_start_index: int,
    ) -> None:
        try:
            with source_pdf.open_outline() as source_outline:
                page_objgen_to_index = self._page_objgen_to_index(source_pdf)
                named_destinations = self._source_named_destinations(source_pdf)
                copied_children = self._copy_outline_items(
                    source_outline.root,
                    output_start_index=output_start_index,
                    page_objgen_to_index=page_objgen_to_index,
                    named_destinations=named_destinations,
                    visited=set(),
                    depth=0,
                )
            if not copied_children:
                return
            group = pikepdf.OutlineItem(group_title, output_start_index)
            group.children.extend(copied_children)
            with output_pdf.open_outline() as output_outline:
                output_outline.root.append(group)
        except PdfMergeError:
            raise
        except Exception as exc:
            raise PdfMergeError("bookmarkの結合に失敗しました") from exc

    def _copy_outline_items(
        self,
        items: list[Any],
        *,
        output_start_index: int,
        page_objgen_to_index: dict[tuple[int, int], int],
        named_destinations: dict[str, object],
        visited: set[int],
        depth: int,
    ) -> list[pikepdf.OutlineItem]:
        if depth > 64:
            raise PdfMergeError("bookmark階層が深すぎるため結合できません")
        copied: list[pikepdf.OutlineItem] = []
        for item in items:
            identity = id(item)
            if identity in visited:
                raise PdfMergeError("bookmark階層にcycleがあります")
            visited.add(identity)
            destination = self._destination_from_outline_item(item)
            output_destination = self._offset_outline_destination(
                destination,
                output_start_index=output_start_index,
                page_objgen_to_index=page_objgen_to_index,
                named_destinations=named_destinations,
            )
            copied_item = pikepdf.OutlineItem(str(item.title), output_destination)
            copied_item.children.extend(
                self._copy_outline_items(
                    getattr(item, "children", []),
                    output_start_index=output_start_index,
                    page_objgen_to_index=page_objgen_to_index,
                    named_destinations=named_destinations,
                    visited=visited,
                    depth=depth + 1,
                )
            )
            visited.remove(identity)
            copied.append(copied_item)
        return copied

    def _destination_from_outline_item(self, item: Any) -> object:
        action = getattr(item, "action", None)
        if action is None:
            return getattr(item, "destination", None)
        action_dict = self._import_inspector.dereference(action)
        if not isinstance(action_dict, pikepdf.Dictionary):
            raise PdfMergeError("bookmark actionが不正です")
        if str(action_dict.get("/S", "")) != "/GoTo":
            raise PdfMergeError("GoTo以外のbookmark actionは結合未対応です")
        destination = action_dict.get("/D", None)
        if destination is None:
            raise PdfMergeError("bookmark actionのdestinationが不正です")
        return destination

    def _offset_outline_destination(
        self,
        destination: object,
        *,
        output_start_index: int,
        page_objgen_to_index: dict[tuple[int, int], int],
        named_destinations: dict[str, object],
    ) -> pikepdf.Array | pikepdf.String | pikepdf.Name | int | None:
        if destination is None:
            raise PdfMergeError("解決できないbookmark destinationがあります")
        if isinstance(destination, int):
            return destination + output_start_index
        if isinstance(destination, pikepdf.Array):
            if not destination:
                raise PdfMergeError("空のbookmark destinationは未対応です")
            page_ref = destination[0]
            page_index: int | None
            if isinstance(page_ref, int):
                page_index = int(page_ref)
            else:
                objgen = getattr(page_ref, "objgen", None)
                page_index = (
                    page_objgen_to_index.get(objgen)
                    if isinstance(objgen, tuple)
                    and len(objgen) == 2
                    and all(isinstance(item, int) for item in objgen)
                    else None
                )
            if page_index is None:
                raise PdfMergeError("入力PDF内で解決できないbookmark destinationがあります")
            copied = pikepdf.Array(destination)
            copied[0] = page_index + output_start_index
            self._validate_destination_array(copied)
            return copied
        if isinstance(destination, (pikepdf.String, pikepdf.Name)):
            name = str(destination)
            if name not in named_destinations:
                raise PdfMergeError("解決できないnamed destinationがあります")
            return self._offset_outline_destination(
                named_destinations[name],
                output_start_index=output_start_index,
                page_objgen_to_index=page_objgen_to_index,
                named_destinations=named_destinations,
            )
        raise PdfMergeError("未対応のbookmark destinationがあります")

    @staticmethod
    def _validate_destination_array(destination: pikepdf.Array) -> None:
        if len(destination) < 2:
            raise PdfMergeError("bookmark destinationが不正です")
        destination_type = str(destination[1])
        expected_lengths = {
            "/Fit": 2,
            "/FitB": 2,
            "/FitH": 3,
            "/FitBH": 3,
            "/FitV": 3,
            "/FitBV": 3,
            "/FitR": 6,
            "/XYZ": 5,
        }
        if destination_type not in expected_lengths:
            raise PdfMergeError(f"{destination_type} bookmark destinationは未対応です")
        if len(destination) != expected_lengths[destination_type]:
            raise PdfMergeError("bookmark destinationのparameter数が不正です")

    def _source_named_destinations(self, source_pdf: pikepdf.Pdf) -> dict[str, object]:
        resolved: dict[str, object] = {}
        legacy = source_pdf.Root.get("/Dests", None)
        if legacy is not None:
            legacy_dict = self._import_inspector.dereference(legacy)
            if not isinstance(legacy_dict, pikepdf.Dictionary):
                raise PdfMergeError("legacy Dests辞書が不正です")
            for key, value in legacy_dict.items():
                resolved[str(key)] = self._destination_value(value)
        names = source_pdf.Root.get("/Names", None)
        if names is None:
            return resolved
        names_dict = self._import_inspector.dereference(names)
        if not isinstance(names_dict, pikepdf.Dictionary):
            raise PdfMergeError("Names辞書が不正です")
        dests = names_dict.get("/Dests", None)
        if dests is None:
            return resolved
        self._collect_name_tree_dests(dests, resolved, visited=set(), depth=0)
        return resolved

    def _collect_name_tree_dests(
        self,
        node_object: object,
        destinations: dict[str, object],
        *,
        visited: set[tuple[int, int]],
        depth: int,
    ) -> None:
        if depth > 32:
            raise PdfMergeError("named destination treeが深すぎます")
        node = self._import_inspector.dereference(node_object)
        if not isinstance(node, pikepdf.Dictionary):
            raise PdfMergeError("named destination treeが不正です")
        objgen = getattr(node, "objgen", None)
        if isinstance(objgen, tuple) and objgen != (0, 0):
            if objgen in visited:
                raise PdfMergeError("named destination treeにcycleがあります")
            visited.add(objgen)
        names = node.get("/Names", None)
        if names is not None:
            names_array = self._import_inspector.dereference(names)
            if not isinstance(names_array, pikepdf.Array) or len(names_array) % 2 != 0:
                raise PdfMergeError("named destination treeのNames配列が不正です")
            for index in range(0, len(names_array), 2):
                name_object = names_array[index]
                if not isinstance(name_object, (pikepdf.String, pikepdf.Name)):
                    raise PdfMergeError("named destination名が不正です")
                destinations[str(name_object)] = self._destination_value(names_array[index + 1])
        kids = node.get("/Kids", None)
        if kids is not None:
            kids_array = self._import_inspector.dereference(kids)
            if not isinstance(kids_array, pikepdf.Array):
                raise PdfMergeError("named destination treeのKids配列が不正です")
            for kid in kids_array:
                self._collect_name_tree_dests(
                    kid,
                    destinations,
                    visited=visited,
                    depth=depth + 1,
                )
        if isinstance(objgen, tuple) and objgen != (0, 0):
            visited.remove(objgen)

    def _destination_value(self, value: object) -> object:
        destination = self._import_inspector.dereference(value)
        if isinstance(destination, pikepdf.Dictionary):
            destination = destination.get("/D", None)
        if destination is None:
            raise PdfMergeError("named destinationの値が不正です")
        return destination

    @staticmethod
    def _page_objgen_to_index(pdf: pikepdf.Pdf) -> dict[tuple[int, int], int]:
        mapping: dict[tuple[int, int], int] = {}
        for index, page in enumerate(pdf.pages):
            objgen = getattr(page.obj, "objgen", None)
            if (
                isinstance(objgen, tuple)
                and len(objgen) == 2
                and all(isinstance(item, int) for item in objgen)
            ):
                mapping[objgen] = index
        return mapping

    @staticmethod
    def _unique_group_titles(inputs: tuple[PdfMergeInput, ...]) -> dict[Path, str]:
        counts: dict[str, int] = {}
        titles: dict[Path, str] = {}
        for input_item in inputs:
            base_title = input_item.label
            next_count = counts.get(base_title, 0) + 1
            counts[base_title] = next_count
            titles[input_item.path] = (
                base_title if next_count == 1 else f"{base_title} ({next_count})"
            )
        return titles

    @staticmethod
    def _metadata_source_label(plan: PdfMergePlan) -> str | None:
        if plan.metadata_policy is PdfMergeMetadataPolicy.NONE:
            return None
        for input_item in plan.inputs:
            if input_item.path == plan.metadata_source_path:
                return input_item.label
        return None

    def _emit_progress(
        self,
        callback: Callable[[PdfMergeProgress], None] | None,
        plan: PdfMergePlan,
        stage: PdfMergeStage,
        index: int,
        filename: str,
        *,
        completed_pages: int = 0,
    ) -> None:
        if callback is None:
            return
        safe_index = min(max(index, 0), len(plan.inputs) - 1)
        input_item = plan.inputs[safe_index]
        callback(
            PdfMergeProgress(
                stage=stage,
                input_number=safe_index + 1,
                input_count=len(plan.inputs),
                filename=filename or input_item.label,
                source_page_count=input_item.page_count,
                completed_output_pages=completed_pages,
                total_output_pages=plan.total_page_count,
                message=stage.value,
            )
        )

    @staticmethod
    def _check_cancelled(should_cancel: Callable[[], bool] | None) -> None:
        if should_cancel is not None and should_cancel():
            raise PdfMergeCancelled("PDF結合をキャンセルしました")

    @staticmethod
    def _ensure_target_snapshot_matches(
        target_path: Path,
        target_snapshot: TargetSnapshot,
    ) -> None:
        try:
            current_snapshot = TargetSnapshot.capture(target_path)
        except OSError as exc:
            raise TargetChangedError(
                "出力先の状態を再確認できなかったため、結合を中止しました"
            ) from exc
        if current_snapshot != target_snapshot:
            raise TargetChangedError("結合中に出力先が別のプロセスで変更されました")

    @staticmethod
    def _apply_existing_target_mode(candidate_path: Path, target_path: Path) -> None:
        if not target_path.exists() or os.name == "nt":
            return
        try:
            target_mode = stat.S_IMODE(target_path.stat().st_mode)
            os.chmod(candidate_path, target_mode)
        except OSError as exc:
            raise PdfMergeError("出力先ファイル属性の適用に失敗しました") from exc

    @staticmethod
    def _replace_atomically(candidate_path: Path, target_path: Path) -> None:
        try:
            os.replace(candidate_path, target_path)
        except OSError as exc:
            raise PdfMergeError("検証済み結合PDFの置換に失敗しました") from exc

    @staticmethod
    def _fsync_file(path: Path) -> None:
        try:
            with path.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PdfMergeError("結合PDF候補の同期に失敗しました") from exc

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
            logger.warning("Failed to fsync parent directory after merge: %s (%s)", directory, exc)
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
                logger.warning("Failed to remove merge candidate: %s (%s)", candidate_path, exc)
            else:
                logger.warning(
                    "Failed to remove merge candidate after error: candidate=%s "
                    "primary_error=%s cleanup_error=%s",
                    candidate_path,
                    type(primary_error).__name__,
                    exc,
                )

    @staticmethod
    def _cleanup_source_snapshot(
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
                raise PdfMergeError(
                    f"結合元PDF snapshotを削除できませんでした: {snapshot_path}"
                ) from exc
            else:
                logger.warning(
                    "Failed to remove merge source snapshot after error: snapshot=%s "
                    "primary_error=%s cleanup_error=%s",
                    snapshot_path,
                    type(primary_error).__name__,
                    exc,
                )
