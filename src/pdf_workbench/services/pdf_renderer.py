from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL.ImageQt import ImageQt
from PySide6.QtGui import QImage


@dataclass(frozen=True, slots=True)
class RenderedPage:
    image: QImage
    page_count: int
    page_index: int


class PdfiumRenderer:
    """Small PDFium adapter used by the initial viewer implementation."""

    def render_page(self, path: Path, page_index: int, scale: float = 1.5) -> RenderedPage:
        if scale <= 0:
            raise ValueError("scale must be positive")

        document = pdfium.PdfDocument(str(path))
        try:
            page_count = len(document)
            if not 0 <= page_index < page_count:
                raise IndexError(f"page_index {page_index} is outside 0..{page_count - 1}")

            page = document[page_index]
            try:
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil().convert("RGBA")
                qimage = QImage(ImageQt(pil_image)).copy()
            finally:
                page.close()
        finally:
            document.close()

        return RenderedPage(image=qimage, page_count=page_count, page_index=page_index)
