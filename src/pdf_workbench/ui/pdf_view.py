from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from pdf_workbench.services.page_coordinates import PageCoordinateMapper, PageGeometry, PdfRect
from pdf_workbench.services.pdf_renderer import (
    DocumentMetadata,
    DocumentRevision,
    PageMetadata,
    PdfRenderServiceProtocol,
    RenderRequest,
)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


class PlaceholderState(StrEnum):
    NOT_REQUESTED = "not_requested"
    QUEUED = "queued"
    RENDERING = "rendering"
    DISPLAYED = "displayed"
    ERROR = "error"


class PagePlaceholder(QFrame):
    def __init__(self, page_index: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.page_index = page_index
        self._state = PlaceholderState.NOT_REQUESTED
        self._metadata = PageMetadata(595.0, 842.0)
        self._logical_zoom = 1.5
        self._rotation = 0
        self._device_pixel_ratio = 1.0
        self._pixmap: QPixmap | None = None
        self._message = f"Page {page_index + 1}"
        self.setFrameShape(QFrame.Shape.Box)
        self.setLineWidth(1)
        self.setStyleSheet("background: white; border: 1px solid #c8c8c8;")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

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
        if message is not None:
            self._message = message
        self.update()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self._state = PlaceholderState.DISPLAYED
        self.update()

    def clear_pixmap(self, message: str) -> None:
        self._pixmap = None
        self._message = message
        self._state = PlaceholderState.NOT_REQUESTED
        self.update()

    def sizeHint(self) -> QSize:
        content_rect = self._content_rect()
        width = max(200, round(content_rect.width()))
        height = max(200, round(content_rect.height()))
        return QSize(width, height)

    def _content_rect(self) -> QRectF:
        geometry = self._metadata.geometry
        if geometry is None:
            geometry = PageGeometry(
                media_box=PdfRect(
                    0.0,
                    0.0,
                    self._metadata.width_points,
                    self._metadata.height_points,
                ),
                crop_box=PdfRect(
                    0.0,
                    0.0,
                    self._metadata.width_points,
                    self._metadata.height_points,
                ),
                visible_box=PdfRect(
                    0.0,
                    0.0,
                    self._metadata.width_points,
                    self._metadata.height_points,
                ),
                intrinsic_rotation=0,
            )
        mapper = PageCoordinateMapper(
            geometry=geometry,
            additional_rotation=self._rotation,
            logical_zoom=self._logical_zoom,
            device_pixel_ratio=self._device_pixel_ratio,
        )
        return QRectF(0.0, 0.0, mapper.view_size.width(), mapper.view_size.height())

    def _update_size(self) -> None:
        hint = self.sizeHint()
        self.setMinimumSize(hint)
        self.resize(hint)
        self.updateGeometry()
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect().adjusted(1, 1, -1, -1), QColor("#f7f7f7"))
        if self._pixmap is not None:
            target = self.rect().adjusted(8, 8, -8, -8)
            scaled = self._pixmap.scaled(
                target.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x_pos = target.x() + max(0, (target.width() - scaled.width()) // 2)
            y_pos = target.y() + max(0, (target.height() - scaled.height()) // 2)
            painter.drawPixmap(x_pos, y_pos, scaled)
            return

        painter.setPen(QColor("#666666"))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status_text())

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

        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)

        self._status_label = QLabel("PDFを開いてください", self._content)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._canvas = ContinuousPageCanvas(self._content)
        self._canvas.hide()
        self._content_layout.addWidget(self._status_label)
        self._content_layout.addWidget(self._canvas)

        self._scroll_area = QScrollArea(self)
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

    @property
    def page_count(self) -> int:
        return 0 if self._metadata is None else self._metadata.page_count

    @property
    def page_index(self) -> int:
        return self._current_page_index

    @property
    def path(self) -> Path | None:
        return self._path

    def open_document(self, path: Path) -> None:
        resolved_path = path.expanduser().resolve()
        self._path = resolved_path
        self._current_page_index = 0
        self._metadata = None
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
        if self._metadata is None:
            self._logical_zoom = max(0.25, min(scale, 5.0))
            return
        self._logical_zoom = max(0.25, min(scale, 5.0))
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
        normalized = rotation % 360
        if normalized == self._rotation:
            return
        self._rotation = normalized
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

    def _show_status(self, message: str) -> None:
        self._status_label.setText(message)
        self._status_label.show()
        self._canvas.hide()
        self._content.adjustSize()
        self.state_changed.emit()

    def _show_error(self, message: str) -> None:
        self._metadata = None
        self._show_status(message)
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
        self._canvas.set_pages(pages)
        self._status_label.hide()
        self._canvas.show()
        self._content.adjustSize()
        self._schedule_visible_page_update()
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
        rect = page.geometry()
        return QRectF(rect)

    def _bump_generation(self) -> None:
        self._generation += 1
