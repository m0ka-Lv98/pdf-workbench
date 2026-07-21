from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pdf_workbench.domain.document_session import FileFingerprint
from pdf_workbench.domain.page_split import (
    PageSplitChunk,
    PageSplitPlan,
    build_split_target_path,
)
from pdf_workbench.services.pdf_page_export import (
    PageExtractionResult,
    PdfPageExportError,
    PdfPageExportService,
    SourcePdfChangedError,
)
from pdf_workbench.services.pdf_page_mutation import SourcePdfRevision
from pdf_workbench.services.pdf_save_service import TargetChangedError, TargetSnapshot


class PageSplitError(RuntimeError):
    """Raised when split preflight fails before any output is produced."""


class PageSplitOutputStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class PageSplitProgress:
    chunk: PageSplitChunk
    target_path: Path
    output_number: int
    output_count: int
    status: PageSplitOutputStatus
    message: str


@dataclass(frozen=True, slots=True)
class PageSplitOutputResult:
    chunk: PageSplitChunk
    target_path: Path
    status: PageSplitOutputStatus
    fingerprint: FileFingerprint | None
    error_message: str
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class PageSplitBatchResult:
    outputs: tuple[PageSplitOutputResult, ...]
    started_at: datetime
    completed_at: datetime
    source_revision: SourcePdfRevision

    @property
    def success_count(self) -> int:
        return self._count(PageSplitOutputStatus.SUCCESS)

    @property
    def failure_count(self) -> int:
        return self._count(PageSplitOutputStatus.FAILED)

    @property
    def skipped_count(self) -> int:
        return self._count(PageSplitOutputStatus.SKIPPED)

    @property
    def cancelled_count(self) -> int:
        return self._count(PageSplitOutputStatus.CANCELLED)

    def _count(self, status: PageSplitOutputStatus) -> int:
        return sum(1 for output in self.outputs if output.status is status)


class PageExporter(Protocol):
    def read_source_pdf_revision(self, path: Path) -> SourcePdfRevision: ...

    def extract_pages(
        self,
        source_path: Path,
        target_path: Path,
        plan: object,
        *,
        working_copy_path: Path | None = None,
        expected_source_revision: SourcePdfRevision | None = None,
        expected_target_snapshot: TargetSnapshot | None = None,
    ) -> PageExtractionResult: ...


class PdfPageSplitService:
    def __init__(self, exporter: PageExporter | None = None) -> None:
        self._exporter = exporter if exporter is not None else PdfPageExportService()

    def read_source_pdf_revision(self, path: Path) -> SourcePdfRevision:
        return self._exporter.read_source_pdf_revision(path)

    def split_pdf(
        self,
        source_path: Path,
        output_directory: Path,
        plan: PageSplitPlan,
        *,
        working_copy_path: Path | None = None,
        expected_source_revision: SourcePdfRevision | None = None,
        overwrite: bool = False,
        expected_target_snapshots: Mapping[Path, TargetSnapshot] | None = None,
        is_managed_path: Callable[[Path], bool] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        progress_callback: Callable[[PageSplitProgress], None] | None = None,
    ) -> PageSplitBatchResult:
        started_at = datetime.now(UTC)
        resolved_source = source_path.expanduser().resolve()
        resolved_output_directory = output_directory.expanduser().resolve()
        resolved_working_copy = (
            working_copy_path.expanduser().resolve() if working_copy_path is not None else None
        )
        targets = tuple(
            build_split_target_path(resolved_output_directory, chunk) for chunk in plan.chunks
        )
        self._preflight(
            resolved_source,
            resolved_output_directory,
            resolved_working_copy,
            targets,
            is_managed_path=is_managed_path,
        )
        source_revision = self._exporter.read_source_pdf_revision(resolved_source)
        if source_revision.page_count != plan.source_page_count:
            raise PageSplitError("分割元PDFのページ数が変更されました")
        if expected_source_revision is not None and source_revision != expected_source_revision:
            raise PageSplitError("分割元PDFが変更されたため、分割を中止しました")
        target_snapshots = self._prepare_target_snapshots(
            targets,
            overwrite=overwrite,
            expected_target_snapshots=expected_target_snapshots,
        )
        outputs: list[PageSplitOutputResult] = []
        for index, (chunk, target_path) in enumerate(zip(plan.chunks, targets, strict=True)):
            if should_cancel is not None and should_cancel():
                outputs.extend(
                    self._remaining_results(
                        plan.chunks[index:],
                        targets[index:],
                        PageSplitOutputStatus.CANCELLED,
                        "キャンセルされました",
                    )
                )
                break
            try:
                current_revision = self._exporter.read_source_pdf_revision(resolved_source)
            except Exception as exc:
                outputs.extend(
                    self._source_changed_results(
                        chunk,
                        target_path,
                        plan.chunks[index + 1 :],
                        targets[index + 1 :],
                        f"分割元PDFの状態を確認できませんでした: {exc}",
                    )
                )
                break
            if current_revision != source_revision:
                outputs.extend(
                    self._source_changed_results(
                        chunk,
                        target_path,
                        plan.chunks[index + 1 :],
                        targets[index + 1 :],
                        "分割元PDFが変更されたため、この出力を中止しました",
                    )
                )
                break
            started_output_at = datetime.now(UTC)
            self._emit_progress(
                progress_callback,
                chunk,
                target_path,
                index,
                plan.output_count,
                PageSplitOutputStatus.SKIPPED,
                "処理中",
            )
            try:
                result = self._exporter.extract_pages(
                    resolved_source,
                    target_path,
                    chunk.extraction_plan,
                    working_copy_path=resolved_working_copy,
                    expected_source_revision=source_revision,
                    expected_target_snapshot=target_snapshots[target_path],
                )
            except SourcePdfChangedError as exc:
                outputs.extend(
                    self._source_changed_results(
                        chunk,
                        target_path,
                        plan.chunks[index + 1 :],
                        targets[index + 1 :],
                        str(exc),
                    )
                )
                break
            except (PdfPageExportError, TargetChangedError, OSError) as exc:
                output = PageSplitOutputResult(
                    chunk=chunk,
                    target_path=target_path,
                    status=PageSplitOutputStatus.FAILED,
                    fingerprint=None,
                    error_message=str(exc),
                    started_at=started_output_at,
                    completed_at=datetime.now(UTC),
                )
            else:
                output = PageSplitOutputResult(
                    chunk=chunk,
                    target_path=target_path,
                    status=PageSplitOutputStatus.SUCCESS,
                    fingerprint=result.fingerprint,
                    error_message="",
                    started_at=started_output_at,
                    completed_at=datetime.now(UTC),
                )
            outputs.append(output)
            self._emit_progress(
                progress_callback,
                chunk,
                target_path,
                index,
                plan.output_count,
                output.status,
                output.error_message or "完了",
            )
        return PageSplitBatchResult(
            outputs=tuple(outputs),
            started_at=started_at,
            completed_at=datetime.now(UTC),
            source_revision=source_revision,
        )

    @staticmethod
    def _preflight(
        source_path: Path,
        output_directory: Path,
        working_copy_path: Path | None,
        targets: tuple[Path, ...],
        *,
        is_managed_path: Callable[[Path], bool] | None,
    ) -> None:
        if not source_path.exists() or not source_path.is_file():
            raise PageSplitError("分割元PDFが存在しません")
        if not output_directory.exists() or not output_directory.is_dir():
            raise PageSplitError("出力先フォルダが存在しません")
        if not os.access(output_directory, os.W_OK):
            raise PageSplitError("出力先フォルダに書き込めません")
        if len(set(targets)) != len(targets):
            raise PageSplitError("分割後の出力先が重複しています")
        for target_path in targets:
            if target_path.parent != output_directory:
                raise PageSplitError("出力先フォルダ外にはPDFを作成できません")
            if target_path == source_path:
                raise PageSplitError("分割元PDFと同じ場所には出力できません")
            if working_copy_path is not None and target_path == working_copy_path:
                raise PageSplitError("現在の作業コピーには出力できません")
            if is_managed_path is not None and is_managed_path(target_path):
                raise PageSplitError("アプリの一時作業フォルダ内には出力できません")

    @staticmethod
    def _prepare_target_snapshots(
        targets: tuple[Path, ...],
        *,
        overwrite: bool,
        expected_target_snapshots: Mapping[Path, TargetSnapshot] | None,
    ) -> dict[Path, TargetSnapshot]:
        if expected_target_snapshots is not None:
            normalized = {
                key.expanduser().resolve(): value
                for key, value in expected_target_snapshots.items()
            }
            expected_keys = set(targets)
            actual_keys = set(normalized)
            missing = expected_keys - actual_keys
            extra = actual_keys - expected_keys
            if missing:
                missing_names = ", ".join(sorted(path.name for path in missing)[:5])
                raise PageSplitError("出力先snapshotが不足しています: " + missing_names)
            if extra:
                extra_names = ", ".join(sorted(path.name for path in extra)[:5])
                raise PageSplitError("未知の出力先snapshotがあります: " + extra_names)
            snapshots = {target: normalized[target] for target in targets}
        else:
            snapshots = {target: TargetSnapshot.capture(target) for target in targets}
        if not overwrite:
            existing = [target.name for target, snapshot in snapshots.items() if snapshot.exists]
            if existing:
                raise PageSplitError("既存の同名ファイルがあります: " + ", ".join(existing[:5]))
        return snapshots

    @staticmethod
    def _remaining_results(
        chunks: tuple[PageSplitChunk, ...],
        targets: tuple[Path, ...],
        status: PageSplitOutputStatus,
        message: str,
    ) -> list[PageSplitOutputResult]:
        now = datetime.now(UTC)
        return [
            PageSplitOutputResult(
                chunk=chunk,
                target_path=target_path,
                status=status,
                fingerprint=None,
                error_message=message,
                started_at=now,
                completed_at=now,
            )
            for chunk, target_path in zip(chunks, targets, strict=True)
        ]

    @classmethod
    def _source_changed_results(
        cls,
        chunk: PageSplitChunk,
        target_path: Path,
        remaining_chunks: tuple[PageSplitChunk, ...],
        remaining_targets: tuple[Path, ...],
        failed_message: str,
    ) -> list[PageSplitOutputResult]:
        now = datetime.now(UTC)
        return [
            PageSplitOutputResult(
                chunk=chunk,
                target_path=target_path,
                status=PageSplitOutputStatus.FAILED,
                fingerprint=None,
                error_message=failed_message,
                started_at=now,
                completed_at=now,
            ),
            *cls._remaining_results(
                remaining_chunks,
                remaining_targets,
                PageSplitOutputStatus.SKIPPED,
                "分割元PDFが変更されたためスキップしました",
            ),
        ]

    @staticmethod
    def _emit_progress(
        callback: Callable[[PageSplitProgress], None] | None,
        chunk: PageSplitChunk,
        target_path: Path,
        index: int,
        output_count: int,
        status: PageSplitOutputStatus,
        message: str,
    ) -> None:
        if callback is None:
            return
        callback(
            PageSplitProgress(
                chunk=chunk,
                target_path=target_path,
                output_number=index + 1,
                output_count=output_count,
                status=status,
                message=message,
            )
        )
