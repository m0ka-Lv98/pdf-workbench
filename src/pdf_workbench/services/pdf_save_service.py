from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]

from pdf_workbench.domain.document_session import DocumentSession, FileFingerprint

logger = logging.getLogger(__name__)


class PdfSaveError(RuntimeError):
    """Raised when a working copy cannot be saved."""


class PdfValidationError(PdfSaveError):
    """Raised when a saved candidate PDF does not validate."""


class AtomicReplaceError(PdfSaveError):
    """Raised when a validated candidate cannot replace the target path."""


@dataclass(frozen=True, slots=True)
class SaveResult:
    target_path: Path
    fingerprint: FileFingerprint
    saved_at: datetime


class PdfSaveService:
    def save_atomic(
        self,
        session: DocumentSession,
        target_path: Path,
        expected_page_count: int,
    ) -> SaveResult:
        resolved_target = target_path.expanduser().resolve()
        parent_directory = resolved_target.parent
        self._ensure_target_directory(parent_directory)
        temp_path = self._create_temp_output_path(resolved_target)
        logger.info(
            "Starting atomic PDF save: session_id=%s working_copy=%s target=%s temp=%s",
            session.session_id,
            session.document_path,
            resolved_target,
            temp_path,
        )
        try:
            self._write_temp_pdf(session.document_path, temp_path)
            self._fsync_file(temp_path)
            logger.info("Validating saved candidate PDF: temp=%s", temp_path)
            self._validate_saved_pdf(temp_path, expected_page_count)
            logger.info("Validation succeeded: temp=%s", temp_path)
            self._replace_atomically(temp_path, resolved_target)
            logger.info("Atomic replace succeeded: target=%s", resolved_target)
            fingerprint = FileFingerprint.from_path(resolved_target)
            saved_at = datetime.now(UTC)
            session.mark_saved(resolved_target, fingerprint, saved_at)
            return SaveResult(
                target_path=resolved_target,
                fingerprint=fingerprint,
                saved_at=saved_at,
            )
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def _ensure_target_directory(self, directory: Path) -> None:
        if not directory.exists():
            raise PdfSaveError("保存先フォルダが存在しません")
        if not directory.is_dir():
            raise PdfSaveError("保存先フォルダが不正です")
        if not os.access(directory, os.W_OK):
            raise PdfSaveError("保存先フォルダに書き込めません")

    def _create_temp_output_path(self, target_path: Path) -> Path:
        prefix = f".{target_path.stem}."
        file_descriptor, temp_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=prefix,
            suffix=".tmp.pdf",
        )
        os.close(file_descriptor)
        return Path(temp_name)

    def _write_temp_pdf(self, source_path: Path, temp_path: Path) -> None:
        try:
            with pikepdf.open(str(source_path)) as pdf:
                pdf.save(str(temp_path))
        except Exception as exc:
            raise PdfSaveError("一時ファイルへの保存に失敗しました") from exc

    def _fsync_file(self, path: Path) -> None:
        try:
            with path.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PdfSaveError("保存データの同期に失敗しました") from exc

    def _validate_saved_pdf(self, path: Path, expected_page_count: int) -> None:
        if not path.exists():
            raise PdfValidationError("保存候補ファイルが作成されていません")
        if path.stat().st_size <= 0:
            raise PdfValidationError("保存候補ファイルのサイズが不正です")

        try:
            with pikepdf.open(str(path)) as pdf:
                if len(pdf.pages) != expected_page_count:
                    raise PdfValidationError("保存候補PDFのページ数が一致しません")
        except PdfValidationError:
            raise
        except Exception as exc:
            raise PdfValidationError("保存候補PDFを再オープンできません") from exc

        try:
            pdf_document = pdfium.PdfDocument(str(path))
        except Exception as exc:
            raise PdfValidationError("保存候補PDFをPDFiumで開けません") from exc

        try:
            if len(pdf_document) != expected_page_count:
                raise PdfValidationError("PDFium上のページ数が一致しません")
            if len(pdf_document) > 0:
                page = pdf_document[0]
                try:
                    bitmap = page.render(scale=0.2)
                    pil_image = bitmap.to_pil()
                    if pil_image.width <= 0 or pil_image.height <= 0:
                        raise PdfValidationError("先頭ページの描画検証に失敗しました")
                finally:
                    page.close()
        except PdfValidationError:
            raise
        except Exception as exc:
            raise PdfValidationError("保存候補PDFのレンダリング検証に失敗しました") from exc
        finally:
            pdf_document.close()

    def _replace_atomically(self, temp_path: Path, target_path: Path) -> None:
        try:
            os.replace(temp_path, target_path)
        except OSError as exc:
            raise AtomicReplaceError("検証済みPDFの置換に失敗しました") from exc
