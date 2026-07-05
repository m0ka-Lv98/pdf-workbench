from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from pdf_workbench.services.pdf_renderer import PdfiumRenderer


class PdfView(QWidget):
    state_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._renderer = PdfiumRenderer()
        self._path: Path | None = None
        self._page_index = 0
        self._scale = 1.5
        self._page_count = 0

        self._image_label = QLabel("PDFを開いてください")
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setMinimumSize(400, 500)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(self._image_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    @property
    def page_count(self) -> int:
        return self._page_count

    @property
    def page_index(self) -> int:
        return self._page_index

    @property
    def path(self) -> Path | None:
        return self._path

    def open_document(self, path: Path) -> None:
        self._path = path
        self._page_index = 0
        self._render_current_page()

    def set_page(self, page_index: int) -> None:
        if self._path is None:
            return
        if not 0 <= page_index < self._page_count:
            return
        self._page_index = page_index
        self._render_current_page()

    def set_zoom(self, scale: float) -> None:
        self._scale = max(0.25, min(scale, 5.0))
        if self._path is not None:
            self._render_current_page()

    def _render_current_page(self) -> None:
        if self._path is None:
            return
        rendered = self._renderer.render_page(self._path, self._page_index, self._scale)
        self._page_count = rendered.page_count
        self._image_label.setPixmap(QPixmap.fromImage(rendered.image))
        self._image_label.adjustSize()
        self.state_changed.emit()
