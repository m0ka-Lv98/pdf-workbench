from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QCloseEvent, QColor, QMouseEvent, QPainter, QPaintEvent, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from pdf_workbench.services.page_coordinates import (
    PageCoordinateMapper,
    PageMetadata,
    PdfPoint,
    PdfRect,
)
from pdf_workbench.services.pdf_renderer import (
    DocumentMetadata,
    DocumentRevision,
    PageTextIndex,
    PdfRenderServiceProtocol,
    RenderRequest,
    SearchMatch,
)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


_CONTENT_MARGIN = 8.0
_MINIMUM_PAGE_EXTENT = 200
_MIN_USER_ZOOM = 0.25
_MAX_USER_ZOOM = 5.0
_BASE_RENDER_SCALE = 1.5
_MIN_LOGICAL_ZOOM = _BASE_RENDER_SCALE * _MIN_USER_ZOOM
_MAX_LOGICAL_ZOOM = _BASE_RENDER_SCALE * _MAX_USER_ZOOM
_PAGE_CORNER_RADIUS = 6.0
_MATCH_COLOR = QColor(255, 214, 51, 120)
_MATCH_OUTLINE = QColor(184, 134, 11, 180)
_CURRENT_MATCH_COLOR = QColor(76, 175, 80, 140)
_CURRENT_MATCH_OUTLINE = QColor(27, 94, 32, 220)
_SELECTION_COLOR = QColor(33, 150, 243, 100)
_SELECTION_OUTLINE = QColor(13, 71, 161, 200)


@dataclass(frozen=True, slots=True)
class PageTextSelection:
    page_index: int
    start: int
    end: int

    def normalized(self) -> PageTextSelection:
        if (self.page_index, self.start) <= (self.page_index, self.end):
            return self
        return PageTextSelection(self.page_index, self.end, self.start)


class PlaceholderState(StrEnum):
    NOT_REQUESTED = "not_requested"
    QUEUED = "queued"
    RENDERING = "rendering"
    DISPLAYED = "displayed"
    ERROR = "error"


class PagePlaceholder(QFrame):
    mouse_selection_started = Signal(int, object)
    mouse_selection_changed = Signal(int, object)
    mouse_selection_finished = Signal(int, object)

    def __init__(self, page_index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.page_index = page_index
        self._state = PlaceholderState.NOT_REQUESTED
        self._metadata: PageMetadata | None = None
        self._logical_zoom = _BASE_RENDER_SCALE
        self._rotation = 0
        self._device_pixel_ratio = 1.0
        self._pixmap: QPixmap | None = None
        self._message = f"Page {page_index + 1}"
        self._match_boxes: list[PageSelectionBox] = []
        self._current_match_boxes: list[PageSelectionBox] = []
        self._selection_boxes: list[PageSelectionBox] = []
        self.setObjectName("pageCard")
        self.setFrameShape(QFrame.Shape.Box)
        self.setLineWidth(1)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setProperty("renderState", self._state.value)

    @property
    def state(self) -> PlaceholderState:
        return self._state

    def configure(
        self,
        metadata: PageMetadata,
        logical_zoom: float,
        rotation: int,
        device_pixel_ratio: float,
    ) -> None:
        self._metadata = metadata
        self._logical_zoom = logical_zoom
        self._rotation = rotation
        self._device_pixel_ratio = device_pixel_ratio
        self._update_size()

    def set_state(self, state: PlaceholderState, message: str | None = None) -> None:
        self._state = state
        self.setProperty("renderState", state.value)
        self._repolish()
        if message is not None:
            self._message = message
        self.update()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self._state = PlaceholderState.DISPLAYED
        self.setProperty("renderState", self._state.value)
        self._repolish()
        self.update()

    def clear_pixmap(self, message: str) -> None:
        self._pixmap = None
        self._message = message
        self._state = PlaceholderState.NOT_REQUESTED
        self.setProperty("renderState", self._state.value)
        self._repolish()
        self.update()

    def sizeHint(self) -> QSize:
        mapper = self._coordinate_mapper()
        width = max(
            _MINIMUM_PAGE_EXTENT,
            math.ceil(mapper.view_size.width() + 2 * _CONTENT_MARGIN),
        )
        height = max(
            _MINIMUM_PAGE_EXTENT,
            math.ceil(mapper.view_size.height() + 2 * _CONTENT_MARGIN),
        )
        return QSize(width, height)

    def _coordinate_mapper(self) -> PageCoordinateMapper:
        if self._metadata is None:
            raise RuntimeError("page metadata is not configured")
        mapper = PageCoordinateMapper(
            geometry=self._metadata.geometry,
            additional_rotation=self._rotation,
            logical_zoom=self._logical_zoom,
            device_pixel_ratio=self._device_pixel_ratio,
        )
        return mapper

    def page_content_rect(self) -> QRectF:
        mapper = self._coordinate_mapper()
        content_size = mapper.view_size
        x = (self.width() - content_size.width()) / 2.0
        y = (self.height() - content_size.height()) / 2.0
        return QRectF(x, y, content_size.width(), content_size.height())

    def set_highlights(
        self,
        match_boxes: list[PdfRect],
        selection_boxes: list[PdfRect],
        current_match_boxes: list[PdfRect] | None = None,
    ) -> None:
        self._match_boxes = [PageSelectionBox(box, False) for box in match_boxes]
        self._selection_boxes = [PageSelectionBox(box, True) for box in selection_boxes]
        self._current_match_boxes = [
            PageSelectionBox(box, False) for box in (current_match_boxes or [])
        ]
        self.update()

    def _update_size(self) -> None:
        hint = self.sizeHint()
        self.setMinimumSize(hint)
        self.resize(hint)
        self.updateGeometry()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(2, 2, -2, -2)
        shadow = rect.translated(2, 3)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 28))
        painter.drawRoundedRect(shadow, _PAGE_CORNER_RADIUS, _PAGE_CORNER_RADIUS)

        surface = rect
        painter.setBrush(self.palette().base())
        painter.drawRoundedRect(surface, _PAGE_CORNER_RADIUS, _PAGE_CORNER_RADIUS)

        if self._pixmap is not None:
            target = self.page_content_rect()
            painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
            painter.save()
            painter.translate(target.topLeft())
            self._draw_overlays(painter)
            painter.restore()
        else:
            painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status_text())

        painter.setPen(self.palette().color(QPalette.ColorRole.Mid))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(surface, _PAGE_CORNER_RADIUS, _PAGE_CORNER_RADIUS)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouse_selection_started.emit(self.page_index, event.position())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.mouse_selection_changed.emit(self.page_index, event.position())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.mouse_selection_finished.emit(self.page_index, event.position())
        super().mouseReleaseEvent(event)

    def _draw_overlays(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for box in self._match_boxes:
            self._draw_box(painter, box.box, _MATCH_COLOR, _MATCH_OUTLINE)
        for box in self._current_match_boxes:
            self._draw_box(painter, box.box, _CURRENT_MATCH_COLOR, _CURRENT_MATCH_OUTLINE)
        for box in self._selection_boxes:
            self._draw_box(painter, box.box, _SELECTION_COLOR, _SELECTION_OUTLINE)

    def _draw_box(
        self,
        painter: QPainter,
        box: PdfRect,
        fill: QColor,
        outline: QColor,
    ) -> None:
        mapper = self._coordinate_mapper()
        rect = mapper.pdf_to_view_rect(box)
        painter.setPen(outline)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 3.0, 3.0)

    def _status_text(self) -> str:
        labels = {
            PlaceholderState.NOT_REQUESTED: "待機中",
            PlaceholderState.QUEUED: "レンダリング待ち",
            PlaceholderState.RENDERING: "レンダリング中",
            PlaceholderState.ERROR: self._message,
            PlaceholderState.DISPLAYED: "",
        }
        if self._state == PlaceholderState.DISPLAYED:
            return ""
        return f"{self.page_index + 1}\n{labels[self._state]}"

    def _repolish(self) -> None:
        style = self.style()
        style.unpolish(self)
        style.polish(self)


@dataclass(frozen=True, slots=True)
class PageSelectionBox:
    box: PdfRect
    selection: bool


class ContinuousPageCanvas(QWidget):
    viewport_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(24, 24, 24, 24)
        self._layout.setSpacing(24)
        self._pages: list[PagePlaceholder] = []

    @property
    def pages(self) -> list[PagePlaceholder]:
        return self._pages

    def set_pages(self, pages: list[PagePlaceholder]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
        self._pages = pages
        for page in pages:
            self._layout.addWidget(page, alignment=Qt.AlignmentFlag.AlignHCenter)
        self._layout.addStretch(1)
        self._layout.activate()
        self.adjustSize()
        self.updateGeometry()
        self.viewport_changed.emit()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            self.viewport_changed.emit()
        return super().event(event)


@dataclass(frozen=True, slots=True)
class VisiblePageRequest:
    page_index: int
    priority: int


class PdfView(QWidget):
    state_changed = Signal()
    error_occurred = Signal(str)

    def __init__(
        self,
        render_service: PdfRenderServiceProtocol,
        parent: QWidget | None = None,
        *,
        debounce_interval_ms: int = 40,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("pdfView")
        self._render_service = render_service
        self._document_id = uuid.uuid4().hex
        self._path: Path | None = None
        self._metadata: DocumentMetadata | None = None
        self._generation = 0
        self._current_page_index = 0
        self._logical_zoom = 1.5
        self._rotation = 0
        self._closed = False
        self._desired_pages: set[int] = set()
        self._text_indexes: dict[int, PageTextIndex] = {}
        self._search_query: str = ""
        self._search_matches: list[SearchMatch] = []
        self._current_match_index = -1
        self._selection: PageTextSelection | None = None
        self._selection_anchor: tuple[int, int] | None = None
        self._selection_active = False

        self._content = QWidget(self)
        self._content.setObjectName("pdfContent")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)

        self._status_label = QLabel("PDFを開いてください", self._content)
        self._status_label.setObjectName("pdfStatusLabel")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setProperty("renderState", "not_requested")

        self._canvas = ContinuousPageCanvas(self._content)
        self._canvas.setObjectName("pdfCanvas")
        self._canvas.hide()
        self._content_layout.addWidget(self._status_label)
        self._content_layout.addWidget(self._canvas)

        self._scroll_area = QScrollArea(self)
        self._scroll_area.setObjectName("pdfScrollArea")
        self._scroll_area.setWidgetResizable(False)
        self._scroll_area.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._scroll_area.setWidget(self._content)
        self._scroll_area.viewport().installEventFilter(self)
        self._scroll_area.verticalScrollBar().valueChanged.connect(
            self._schedule_visible_page_update
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._scroll_area)

        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.setInterval(debounce_interval_ms)
        self._visible_timer.timeout.connect(self._request_visible_pages)

        self._canvas.viewport_changed.connect(self._schedule_visible_page_update)
        self._render_service.document_loaded.connect(self._on_document_loaded)
        self._render_service.document_failed.connect(self._on_document_failed)
        self._render_service.render_succeeded.connect(self._on_render_succeeded)
        self._render_service.render_failed.connect(self._on_render_failed)
        text_index_ready = getattr(self._render_service, "text_page_indexed", None)
        if text_index_ready is not None:
            text_index_ready.connect(self._on_text_index_ready)
        text_index_failed = getattr(self._render_service, "text_index_failed", None)
        if text_index_failed is not None:
            text_index_failed.connect(self._on_text_index_failed)

    @property
    def page_count(self) -> int:
        return 0 if self._metadata is None else self._metadata.page_count

    @property
    def page_index(self) -> int:
        return self._current_page_index

    @property
    def zoom_factor(self) -> float:
        return self._logical_zoom

    @property
    def rotation(self) -> int:
        return self._rotation

    @property
    def path(self) -> Path | None:
        return self._path

    def open_document(self, path: Path) -> None:
        resolved_path = path.expanduser().resolve()
        self._path = resolved_path
        self._current_page_index = 0
        self._metadata = None
        self._text_indexes.clear()
        self._search_query = ""
        self._search_matches.clear()
        self._current_match_index = -1
        self._selection = None
        self._selection_anchor = None
        self._selection_active = False
        self._bump_generation()
        self._show_status("PDFを読み込み中です")
        try:
            revision = DocumentRevision.from_path(resolved_path)
        except OSError as exc:
            self._show_error(str(exc))
            return
        self._render_service.open_document(
            self._document_id,
            resolved_path,
            self._generation,
            revision,
        )

    def _advance_render_generation(self) -> None:
        self._generation += 1
        if self._metadata is not None:
            self._render_service.update_document_generation(
                self._document_id,
                self._generation,
                self._metadata.revision,
            )

    def set_page(self, page_index: int) -> None:
        if self._metadata is None:
            return
        target = _clamp(page_index, 0, self.page_count - 1)
        self._scroll_to_page(target)
        self._current_page_index = target
        self._schedule_visible_page_update()
        self.state_changed.emit()

    def set_zoom(self, scale: float) -> None:
        scale = float(scale)
        if not math.isfinite(scale):
            raise ValueError("scale must be finite")
        if self._metadata is None:
            self._logical_zoom = max(_MIN_LOGICAL_ZOOM, min(scale, _MAX_LOGICAL_ZOOM))
            return
        self._logical_zoom = max(_MIN_LOGICAL_ZOOM, min(scale, _MAX_LOGICAL_ZOOM))
        self._desired_pages.clear()
        self._advance_render_generation()
        for index, page in enumerate(self._canvas.pages):
            page.configure(
                self._metadata.pages[index],
                self._logical_zoom,
                self._rotation,
                self.devicePixelRatioF(),
            )
            page.clear_pixmap("待機中")
        self._content.adjustSize()
        self._schedule_visible_page_update()
        self.state_changed.emit()

    def set_rotation(self, rotation: int) -> None:
        if rotation not in {0, 90, 180, 270}:
            raise ValueError("rotation must be one of 0, 90, 180, 270")
        if rotation == self._rotation:
            return
        self._rotation = rotation
        if self._metadata is None:
            return
        self._desired_pages.clear()
        self._advance_render_generation()
        for index, page in enumerate(self._canvas.pages):
            page.configure(
                self._metadata.pages[index],
                self._logical_zoom,
                self._rotation,
                self.devicePixelRatioF(),
            )
            page.clear_pixmap("待機中")
        self._content.adjustSize()
        self._schedule_visible_page_update()
        self.state_changed.emit()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bump_generation()
        self._render_service.close_document(self._document_id, self._generation)

    def close_document(self) -> None:
        if self._closed:
            return
        self._path = None
        self._metadata = None
        self._desired_pages.clear()
        self._text_indexes.clear()
        self._search_matches.clear()
        self._selection = None
        self._selection_anchor = None
        self._bump_generation()
        self._render_service.close_document(self._document_id, self._generation)
        self._show_status("PDFを開いてください")

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._scroll_area.viewport() and event.type() == QEvent.Type.Resize:
            self._schedule_visible_page_update()
        return super().eventFilter(watched, event)

    def _show_status(self, message: str, *, render_state: str = "not_requested") -> None:
        self._status_label.setText(message)
        self._status_label.setProperty("renderState", render_state)
        self._repolish(self._status_label)
        self._status_label.show()
        self._canvas.hide()
        self._content.adjustSize()
        self.state_changed.emit()

    def _show_error(self, message: str) -> None:
        self._metadata = None
        self._show_status(message, render_state="error")
        self.error_occurred.emit(message)

    def _on_document_loaded(self, document_id: object, generation: int, metadata: object) -> None:
        if document_id != self._document_id or generation != self._generation or self._closed:
            return
        if not isinstance(metadata, DocumentMetadata):
            raise TypeError("metadata must be DocumentMetadata")
        self._metadata = metadata
        pages = [PagePlaceholder(index, self._canvas) for index in range(metadata.page_count)]
        for index, page in enumerate(pages):
            page.configure(
                metadata.pages[index],
                self._logical_zoom,
                self._rotation,
                self.devicePixelRatioF(),
            )
            page.mouse_selection_started.connect(self._begin_selection)
            page.mouse_selection_changed.connect(self._update_selection)
            page.mouse_selection_finished.connect(self._finish_selection)
        self._canvas.set_pages(pages)
        self._status_label.hide()
        self._canvas.show()
        self._content.adjustSize()
        self._schedule_visible_page_update()
        self._refresh_page_overlays()
        self.state_changed.emit()

    def _on_document_failed(self, document_id: object, generation: int, message: str) -> None:
        if document_id != self._document_id or generation != self._generation or self._closed:
            return
        self._show_error(message)

    def _on_render_succeeded(self, result: object) -> None:
        from pdf_workbench.services.pdf_renderer import RenderResult

        if not isinstance(result, RenderResult):
            raise TypeError("result must be RenderResult")
        if (
            result.document_id != self._document_id
            or result.generation != self._generation
            or self._closed
            or self._metadata is None
            or result.page_index not in self._desired_pages
            or not 0 <= result.page_index < len(self._canvas.pages)
        ):
            return
        placeholder = self._canvas.pages[result.page_index]
        pixmap = QPixmap.fromImage(result.image)
        placeholder.set_pixmap(pixmap)
        self._refresh_page_overlays(result.page_index)
        self._update_current_page()
        self.state_changed.emit()

    def _on_render_failed(
        self,
        document_id: object,
        generation: int,
        page_index: int,
        message: str,
    ) -> None:
        if (
            document_id != self._document_id
            or generation != self._generation
            or self._closed
            or self._metadata is None
            or not 0 <= page_index < len(self._canvas.pages)
        ):
            return
        self._canvas.pages[page_index].set_state(PlaceholderState.ERROR, message)
        self.error_occurred.emit(message)
        self.state_changed.emit()

    def _on_text_index_ready(
        self,
        document_id: object,
        _generation: int,
        page_text: object,
    ) -> None:
        if document_id != self._document_id or self._closed:
            return
        if not isinstance(page_text, PageTextIndex):
            raise TypeError("page_text must be PageTextIndex")
        if self._metadata is None or page_text.revision != self._metadata.revision:
            return
        self._text_indexes[page_text.page_index] = page_text
        if self._search_query:
            self._recompute_search_matches()
        self._refresh_page_overlays(page_text.page_index)

    def _on_text_index_failed(
        self,
        document_id: object,
        _generation: int,
        page_index: int,
        message: str,
    ) -> None:
        if document_id != self._document_id or self._closed:
            return
        if 0 <= page_index < len(self._canvas.pages):
            self._canvas.pages[page_index].set_state(PlaceholderState.ERROR, message)

    def search(self, query: str) -> int:
        self._search_query = query.strip()
        self._recompute_search_matches()
        self._refresh_page_overlays()
        return len(self._search_matches)

    def next_match(self) -> bool:
        if not self._search_matches:
            return False
        self._current_match_index = (self._current_match_index + 1) % len(self._search_matches)
        self._goto_match(self._current_match_index)
        return True

    def previous_match(self) -> bool:
        if not self._search_matches:
            return False
        self._current_match_index = (self._current_match_index - 1) % len(self._search_matches)
        self._goto_match(self._current_match_index)
        return True

    def copy_selected_text(self) -> bool:
        selected_text = self.selected_text
        if not selected_text:
            return False
        clipboard = QApplication.clipboard()
        clipboard.setText(selected_text)
        return True

    @property
    def selected_text(self) -> str:
        if self._selection is None:
            return ""
        selection = self._selection.normalized()
        if selection.page_index not in self._text_indexes:
            return ""
        text_index = self._text_indexes[selection.page_index]
        return text_index.text[selection.start : selection.end]

    def _goto_match(self, match_index: int) -> None:
        if not 0 <= match_index < len(self._search_matches):
            return
        match = self._search_matches[match_index]
        self._current_page_index = match.page_index
        self._scroll_to_page(match.page_index)
        self._refresh_page_overlays(match.page_index)
        self.state_changed.emit()

    def _recompute_search_matches(self) -> None:
        query = self._search_query.strip()
        self._search_matches.clear()
        self._current_match_index = -1
        if not query or not self._text_indexes:
            return
        lowered = query.casefold()
        for page_index in sorted(self._text_indexes):
            text_index = self._text_indexes[page_index]
            text = text_index.text.casefold()
            start = 0
            while True:
                found = text.find(lowered, start)
                if found == -1:
                    break
                end = found + len(lowered)
                boxes = self._boxes_for_range(text_index, found, end)
                self._search_matches.append(
                    SearchMatch(
                        page_index=page_index,
                        start=found,
                        end=end,
                        text=text_index.text[found:end],
                        boxes=tuple(boxes),
                    )
                )
                start = found + max(1, len(lowered))

    def _boxes_for_range(
        self,
        text_index: PageTextIndex,
        start: int,
        end: int,
    ) -> list[PdfRect]:
        boxes: list[PdfRect] = []
        for character in text_index.characters:
            if start <= character.pdfium_index < end and character.box is not None:
                boxes.append(character.box)
        return boxes

    def _refresh_page_overlays(self, page_index: int | None = None) -> None:
        if self._metadata is None or not self._canvas.pages:
            return
        match_boxes_by_page: dict[int, list[PdfRect]] = {}
        current_boxes_by_page: dict[int, list[PdfRect]] = {}
        if self._search_matches:
            for index, match in enumerate(self._search_matches):
                match_boxes_by_page.setdefault(match.page_index, []).extend(match.boxes)
                if index == self._current_match_index:
                    current_boxes_by_page.setdefault(match.page_index, []).extend(match.boxes)
        selection_boxes_by_page: dict[int, list[PdfRect]] = {}
        if self._selection is not None:
            selection = self._selection.normalized()
            text_index = self._text_indexes.get(selection.page_index)
            if text_index is not None:
                selection_boxes_by_page[selection.page_index] = self._boxes_for_range(
                    text_index,
                    selection.start,
                    selection.end,
                )
        pages = [page_index] if page_index is not None else list(range(len(self._canvas.pages)))
        for index in pages:
            if not 0 <= index < len(self._canvas.pages):
                continue
            page = self._canvas.pages[index]
            page.set_highlights(
                match_boxes_by_page.get(index, []),
                selection_boxes_by_page.get(index, []),
                current_boxes_by_page.get(index, []),
            )

    def _begin_selection(self, page_index: int, position: object) -> None:
        point = self._position_to_page_point(page_index, position)
        if point is None:
            return
        char_index = self._character_index_for_point(page_index, point)
        if char_index is None:
            return
        self._selection_anchor = (page_index, char_index)
        self._selection_active = True
        self._selection = PageTextSelection(page_index, char_index, char_index + 1)
        self._refresh_page_overlays(page_index)

    def _update_selection(self, page_index: int, position: object) -> None:
        if not self._selection_active or self._selection_anchor is None:
            return
        point = self._position_to_page_point(page_index, position)
        if point is None:
            return
        char_index = self._character_index_for_point(page_index, point)
        if char_index is None:
            return
        anchor_page, anchor_index = self._selection_anchor
        if page_index != anchor_page:
            return
        start = min(anchor_index, char_index)
        end = max(anchor_index, char_index) + 1
        self._selection = PageTextSelection(page_index, start, end)
        self._refresh_page_overlays(page_index)

    def _finish_selection(self, page_index: int, position: object) -> None:
        self._update_selection(page_index, position)
        self._selection_active = False
        self._selection_anchor = None

    def _position_to_page_point(self, page_index: int, position: object) -> PdfPoint | None:
        if not isinstance(position, QPointF):
            return None
        if not 0 <= page_index < len(self._canvas.pages):
            return None
        page = self._canvas.pages[page_index]
        local = position - page.page_content_rect().topLeft()
        mapper = page._coordinate_mapper()
        return mapper.view_to_pdf_point(local)

    def _character_index_for_point(self, page_index: int, point: PdfPoint) -> int | None:
        text_index = self._text_indexes.get(page_index)
        if text_index is None or not text_index.characters:
            return None
        best_index = None
        best_distance = float("inf")
        for character in text_index.characters:
            box = character.box
            if box is None:
                continue
            center_x = (box.left + box.right) / 2.0
            center_y = (box.bottom + box.top) / 2.0
            distance = math.hypot(center_x - point.x, center_y - point.y)
            if box.left <= point.x <= box.right and box.bottom <= point.y <= box.top:
                return character.pdfium_index
            if distance < best_distance:
                best_distance = distance
                best_index = character.pdfium_index
        return best_index

    def _schedule_visible_page_update(self) -> None:
        if self._metadata is None or self._closed:
            return
        self._visible_timer.start()

    def _request_visible_pages(self) -> None:
        if self._metadata is None or self._path is None or self._closed:
            return
        try:
            revision = DocumentRevision.from_path(self._path)
        except OSError as exc:
            self._show_error(str(exc))
            return
        if revision != self._metadata.revision:
            self._metadata = None
            self._bump_generation()
            self._show_status("PDFを再読み込み中です")
            self._render_service.open_document(
                self._document_id,
                self._path,
                self._generation,
                revision,
            )
            return

        requests = self._visible_page_requests()
        if not requests:
            return
        self._desired_pages = {request.page_index for request in requests}
        for request in requests:
            page = self._canvas.pages[request.page_index]
            if page.state is PlaceholderState.DISPLAYED:
                continue
            if page.state is PlaceholderState.NOT_REQUESTED:
                page.set_state(PlaceholderState.QUEUED)
            else:
                page.set_state(PlaceholderState.RENDERING)
            self._render_service.request_render(
                RenderRequest(
                    document_id=self._document_id,
                    generation=self._generation,
                    page_index=request.page_index,
                    logical_zoom=self._logical_zoom,
                    rotation=self._rotation,
                    device_pixel_ratio=self.devicePixelRatioF(),
                    priority=request.priority,
                    revision=revision,
                )
            )
        self._update_current_page()
        self.state_changed.emit()

    def _visible_page_requests(self) -> list[VisiblePageRequest]:
        visible = self._visible_pages()
        if not visible:
            return []

        requested: list[VisiblePageRequest] = []
        for page_index in visible:
            requested.append(VisiblePageRequest(page_index=page_index, priority=0))

        first = visible[0]
        last = visible[-1]
        if first > 0:
            requested.append(VisiblePageRequest(page_index=first - 1, priority=1))
        if last + 1 < self.page_count:
            requested.append(VisiblePageRequest(page_index=last + 1, priority=1))

        unique: dict[int, VisiblePageRequest] = {}
        for request in requested:
            existing = unique.get(request.page_index)
            if existing is None or request.priority < existing.priority:
                unique[request.page_index] = request
        return [unique[index] for index in sorted(unique)]

    def _visible_pages(self) -> list[int]:
        scroll_value = self._scroll_area.verticalScrollBar().value()
        viewport_height = self._scroll_area.viewport().height()
        viewport_top = scroll_value
        viewport_bottom = scroll_value + viewport_height
        visible: list[int] = []
        for page in self._canvas.pages:
            rect = self.page_content_rect(page.page_index)
            page_top = rect.top()
            page_bottom = rect.bottom()
            if page_bottom >= viewport_top and page_top <= viewport_bottom:
                visible.append(page.page_index)
        return visible

    def _update_current_page(self) -> None:
        if self._metadata is None or not self._canvas.pages:
            self._current_page_index = 0
            return
        scroll_value = self._scroll_area.verticalScrollBar().value()
        viewport_height = self._scroll_area.viewport().height()
        viewport_top = scroll_value
        viewport_bottom = scroll_value + viewport_height
        viewport_center_y = scroll_value + (viewport_height // 2)
        best_page = self._current_page_index
        best_area = -1.0
        best_center_distance = 1_000_000_000.0
        for page in self._canvas.pages:
            rect = self.page_content_rect(page.page_index)
            overlap_top = max(viewport_top, rect.top())
            overlap_bottom = min(viewport_bottom, rect.bottom())
            overlap_height = overlap_bottom - overlap_top
            area = rect.width() * max(0, overlap_height)
            if area <= 0:
                continue
            center_distance = abs(rect.center().y() - viewport_center_y)
            if area > best_area or (area == best_area and center_distance < best_center_distance):
                best_page = page.page_index
                best_area = area
                best_center_distance = center_distance
        self._current_page_index = best_page

    def _scroll_to_page(self, page_index: int) -> None:
        if not 0 <= page_index < len(self._canvas.pages):
            return
        self._scroll_area.verticalScrollBar().setValue(
            round(self.page_content_rect(page_index).top())
        )

    def page_content_rect(self, page_index: int) -> QRectF:
        if self._metadata is None:
            return QRectF()
        page = self._canvas.pages[page_index]
        local_rect = page.page_content_rect()
        page_origin_in_content = page.mapTo(self._content, QPoint(0, 0))
        top_left = QPointF(page_origin_in_content) + local_rect.topLeft()
        return QRectF(top_left, local_rect.size())

    def _bump_generation(self) -> None:
        self._generation += 1

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
