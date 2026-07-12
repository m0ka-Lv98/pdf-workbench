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
        saved_at = datetime.now(UTC)
        temp_path: Path | None = None
        primary_error: BaseException | None = None

        logger.info(
            "Starting atomic PDF save: session_id=%s working_copy=%s target=%s",
            session.session_id,
            session.document_path,
            resolved_target,
        )

        try:
            self._ensure_save_target_is_allowed(session, resolved_target)
            self._ensure_target_directory(resolved_target.parent)
            temp_path = self._create_temp_output_path(resolved_target)
            logger.info("Created temp save candidate: temp=%s", temp_path)
            self._write_temp_pdf(session.document_path, temp_path)
            self._fsync_file(temp_path)
            logger.info("Validating saved candidate PDF: temp=%s", temp_path)
            self._validate_saved_pdf(temp_path, expected_page_count)
            logger.info("Validation succeeded: temp=%s", temp_path)
            fingerprint = self._build_fingerprint(temp_path)
            self._apply_existing_target_mode(temp_path, resolved_target)
            self._replace_atomically(temp_path, resolved_target)
            self._fsync_parent_directory(resolved_target.parent)
            logger.info("Atomic replace succeeded: target=%s", resolved_target)
            session.mark_saved(resolved_target, fingerprint, saved_at)
            return SaveResult(
                target_path=resolved_target,
                fingerprint=fingerprint,
                saved_at=saved_at,
            )
        except (PdfSaveError, PdfValidationError, AtomicReplaceError) as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise PdfSaveError("PDFの保存準備に失敗しました") from exc
        finally:
            self._cleanup_temp_file(temp_path, primary_error=primary_error)

    def _ensure_save_target_is_allowed(
        self,
        session: DocumentSession,
        target_path: Path,
    ) -> None:
        if target_path == session.working_copy_path or target_path.is_relative_to(
            session.workspace_directory
        ):
            raise PdfSaveError(
                "アプリの一時作業フォルダ内には保存できません。別の保存先を選択してください。"
            )

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
                bitmap: Any | None = None
                pil_image: Any | None = None
                try:
                    bitmap = page.render(scale=0.2)
                    pil_image = bitmap.to_pil()
                    if pil_image.width <= 0 or pil_image.height <= 0:
                        raise PdfValidationError("先頭ページの描画検証に失敗しました")
                finally:
                    if pil_image is not None and hasattr(pil_image, "close"):
                        pil_image.close()
                    if bitmap is not None and hasattr(bitmap, "close"):
                        bitmap.close()
                    page.close()
        except PdfValidationError:
            raise
        except Exception as exc:
            raise PdfValidationError("保存候補PDFのレンダリング検証に失敗しました") from exc
        finally:
            pdf_document.close()

    def _build_fingerprint(self, path: Path) -> FileFingerprint:
        try:
            return FileFingerprint.from_path(path)
        except OSError as exc:
            raise PdfSaveError("保存候補PDFのメタデータ取得に失敗しました") from exc

    def _apply_existing_target_mode(self, temp_path: Path, target_path: Path) -> None:
        if not target_path.exists() or os.name == "nt":
            return
        try:
            target_mode = stat.S_IMODE(target_path.stat().st_mode)
            os.chmod(temp_path, target_mode)
        except OSError as exc:
            raise PdfSaveError("保存先ファイル属性の適用に失敗しました") from exc

    def _replace_atomically(self, temp_path: Path, target_path: Path) -> None:
        try:
            os.replace(temp_path, target_path)
        except OSError as exc:
            raise AtomicReplaceError("検証済みPDFの置換に失敗しました") from exc

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
                "Failed to fsync parent directory after replace: %s (%s)",
                directory,
                exc,
            )
        finally:
            os.close(directory_handle)

    def _cleanup_temp_file(
        self,
        temp_path: Path | None,
        *,
        primary_error: BaseException | None,
    ) -> None:
        if temp_path is None or not temp_path.exists():
            return
        try:
            temp_path.unlink()
        except OSError as exc:
            if primary_error is None:
                logger.warning("Failed to remove temp PDF after save: %s (%s)", temp_path, exc)
            else:
                logger.warning(
                    "Failed to remove temp PDF after save error: temp=%s primary_error=%s "
                    "cleanup_error=%s",
                    temp_path,
                    type(primary_error).__name__,
                    exc,
                )
