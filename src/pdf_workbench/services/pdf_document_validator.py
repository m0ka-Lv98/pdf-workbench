from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]


class PdfDocumentValidationError(RuntimeError):
    """Raised when a PDF cannot be reopened and rendered safely."""


@dataclass(frozen=True, slots=True)
class PdfValidationResult:
    page_count: int


class PdfDocumentValidator:
    def validate(
        self,
        path: str,
        *,
        expected_page_count: int | None = None,
        render_page_indexes: range | tuple[int, ...] | None = None,
    ) -> PdfValidationResult:
        try:
            with pikepdf.open(path) as pdf:
                pike_page_count = len(pdf.pages)
        except Exception as exc:
            raise PdfDocumentValidationError("PDFを再オープンできません") from exc

        if pike_page_count <= 0:
            raise PdfDocumentValidationError("PDFのページ数が不正です")
        if expected_page_count is not None and pike_page_count != expected_page_count:
            raise PdfDocumentValidationError("PDFのページ数が一致しません")

        try:
            pdf_document = pdfium.PdfDocument(path)
        except Exception as exc:
            raise PdfDocumentValidationError("PDFをPDFiumで開けません") from exc

        try:
            pdfium_page_count = len(pdf_document)
            if pdfium_page_count != pike_page_count:
                raise PdfDocumentValidationError("PDFのページ数が一致しません")
            if expected_page_count is not None and pdfium_page_count != expected_page_count:
                raise PdfDocumentValidationError("PDFのページ数が一致しません")
            page_indexes = (0,) if render_page_indexes is None else tuple(render_page_indexes)
            for page_index in page_indexes:
                if page_index < 0 or page_index >= pdfium_page_count:
                    raise PdfDocumentValidationError(
                        f"{page_index + 1}ページ目の描画検証範囲が不正です"
                    )
                self._render_page(pdf_document, page_index)
        finally:
            pdf_document.close()

        return PdfValidationResult(page_count=pike_page_count)

    @staticmethod
    def _render_page(pdf_document: Any, page_index: int) -> None:
        page = pdf_document[page_index]
        bitmap: Any | None = None
        pil_image: Any | None = None
        try:
            bitmap = page.render(scale=0.2)
            pil_image = bitmap.to_pil()
            if pil_image.width <= 0 or pil_image.height <= 0:
                raise PdfDocumentValidationError(
                    f"{page_index + 1}ページ目の描画検証に失敗しました"
                )
        except PdfDocumentValidationError:
            raise
        except Exception as exc:
            raise PdfDocumentValidationError(
                f"{page_index + 1}ページ目の描画検証に失敗しました"
            ) from exc
        finally:
            if pil_image is not None and hasattr(pil_image, "close"):
                pil_image.close()
            if bitmap is not None and hasattr(bitmap, "close"):
                bitmap.close()
            page.close()
