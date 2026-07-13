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
            page = pdf_document[0]
            bitmap: Any | None = None
            pil_image: Any | None = None
            try:
                bitmap = page.render(scale=0.2)
                pil_image = bitmap.to_pil()
                if pil_image.width <= 0 or pil_image.height <= 0:
                    raise PdfDocumentValidationError("先頭ページの描画検証に失敗しました")
            except PdfDocumentValidationError:
                raise
            except Exception as exc:
                raise PdfDocumentValidationError("先頭ページの描画検証に失敗しました") from exc
            finally:
                if pil_image is not None and hasattr(pil_image, "close"):
                    pil_image.close()
                if bitmap is not None and hasattr(bitmap, "close"):
                    bitmap.close()
                page.close()
        finally:
            pdf_document.close()

        return PdfValidationResult(page_count=pike_page_count)
