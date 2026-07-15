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
    QSplitter,
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
    DocumentReleaseResult,
    DocumentRevision,
    NormalizedPageText,
    PageTextIndex,
    PdfRenderServiceProtocol,
    RenderCacheKey,
    RenderFailure,
    RenderRequest,
    SearchMatch,
    TextCharacterBox,
)
from pdf_workbench.ui.widgets.page_organizer import PageOrganizer


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


_CONTENT_MARGIN = 8.0
_MINIMUM_PAGE_EXTENT = 200
_MIN_USER_ZOOM = 0.25
_MAX_USER_ZOOM = 5.0
_BASE_RENDER_SCALE = 1.5
_MIN_LOGICAL_ZOOM = _BASE_RENDER_SCALE * _MIN_USER_ZOOM
_MAX_LOGICAL_ZOOM = _BASE_RENDER_SCALE * _MAX_USER_ZOOM
_THUMBNAIL_TARGET_WIDTH = 140.0
_THUMBNAIL_TARGET_HEIGHT = 182.0
_THUMBNAIL_MAX_LOGICAL_ZOOM = 0.25
_THUMBNAIL_PRIORITY_VISIBLE = 10
_THUMBNAIL_PRIORITY_PREFETCH = 11
_MATCH_COLOR = QColor(255, 214, 51, 120)
_CURRENT_MATCH_COLOR = QColor(255, 167, 38, 160)
_SELECTION_COLOR = QColor(33, 150, 243, 96)
_SELECTION_OUTLINE = QColor(13, 71, 161, 90)
_LINE_RUN_VERTICAL_OVERLAP_RATIO = 0.55
_LINE_RUN_GAP_MULTIPLIER = 1.5


@dataclass(frozen=True, slots=True)
class TextPosition:
    page_index: int
    pdfium_index: int


@dataclass(frozen=True, slots=True)
class DocumentTextSelection:
    anchor: TextPosition
    focus: TextPosition

    def normalized(self) -> tuple[TextPosition, TextPosition]:
        if (self.anchor.page_index, self.anchor.pdfium_index) <= (
            self.focus.page_index,
            self.focus.pdfium_index,
        ):
            return self.anchor, self.focus
        return self.focus, self.anchor


class PlaceholderState(StrEnum):
    NOT_REQUESTED = "not_requested"
    QUEUED = "queued"
    RENDERING = "rendering"
    DISPLAYED = "displayed"
    ERROR = "error"


class PagePlaceholder(QFrame):
    mouse_selection_started = Signal(object)
    mouse_selection_changed = Signal(object)
    mouse_selection_finished = Signal(object)

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
        painter.drawRoundedRect(shadow, 6.0, 6.0)

        surface = rect
        painter.setBrush(self.palette().base())
        painter.drawRoundedRect(surface, 6.0, 6.0)

        if self._pixmap is not None:
            target = self.page_content_rect()
            painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
            painter.save()
            painter.translate(target.topLeft())
            painter.setClipRect(
                QRectF(
                    QPointF(0.0, 0.0),
                    target.size(),
                )
            )
            self._draw_overlays(painter)
            painter.restore()
        else:
            painter.setPen(self.palette().color(QPalette.ColorRole.WindowText))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._status_text())

        painter.setPen(self.palette().color(QPalette.ColorRole.Mid))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(surface, 6.0, 6.0)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.grabMouse()
            self.mouse_selection_started.emit(event.globalPosition())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.mouse_selection_changed.emit(event.globalPosition())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if QWidget.mouseGrabber() is self:
                self.releaseMouse()
            self.mouse_selection_finished.emit(event.globalPosition())
        super().mouseReleaseEvent(event)

    def _draw_overlays(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for box in self._match_boxes:
            self._draw_box(painter, box.box, _MATCH_COLOR)
        for box in self._current_match_boxes:
            self._draw_box(painter, box.box, _CURRENT_MATCH_COLOR)
        for box in self._selection_boxes:
            self._draw_box(painter, box.box, _SELECTION_COLOR, _SELECTION_OUTLINE)

    def _draw_box(
        self,
        painter: QPainter,
        box: PdfRect,
        fill: QColor,
        outline: QColor | None = None,
    ) -> None:
        mapper = self._coordinate_mapper()
        rect = mapper.pdf_to_view_rect(box)
        painter.setPen(outline if outline is not None else Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRect(rect)

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

    def set_top_inset(self, inset: int) -> None:
        margins = self._layout.contentsMargins()
        self._layout.setContentsMargins(margins.left(), inset, margins.right(), margins.bottom())
        self._layout.activate()
        self.adjustSize()
        self.updateGeometry()
        self.viewport_changed.emit()

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


@dataclass(frozen=True, slots=True)
class PdfViewMutationSnapshot:
    current_page_index: int
    selected_page_indexes: tuple[int, ...]
    logical_zoom: float
    search_query: str


@dataclass(frozen=True, slots=True)
class PdfSearchState:
    query: str
    current_index: int
    total_count: int
    indexed_pages: int
    total_pages: int
    failed_pages: int
    indexing_completed: bool
    text_pages_with_content: int = 0
    image_only_pages: int = 0
    empty_text_pages: int = 0


class PdfView(QWidget):
    state_changed = Signal()
    error_occurred = Signal(str)
    search_state_changed = Signal()
    selection_changed = Signal()
    document_loaded = Signal()
    mutation_reload_completed = Signal(bool)
    current_page_changed = Signal(int)

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
        self._expected_main_render_keys: dict[int, RenderCacheKey] = {}
        self._text_indexes: dict[int, PageTextIndex] = {}
        self._search_query: str = ""
        self._search_matches: list[SearchMatch] = []
        self._current_match_index = -1
        self._selection: DocumentTextSelection | None = None
        self._selection_anchor: TextPosition | None = None
        self._selection_active = False
        self._normalized_page_texts: dict[int, NormalizedPageText] = {}
        self._indexed_page_count = 0
        self._failed_page_count = 0
        self._index_total_page_count = 0
        self._indexing_completed = False
        self._text_page_indexes: set[int] = set()
        self._empty_text_page_indexes: set[int] = set()
        self._image_only_page_indexes: set[int] = set()
        self._search_top_inset = 24
        self._pending_restore_page_index: int | None = None
        self._pending_restore_selected_page_indexes: tuple[int, ...] | None = None
        self._pending_restore_query: str | None = None
        self._mutation_suspended = False
        self._mutation_reload_active = False

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

        self._page_organizer = PageOrganizer(self)
        self._page_organizer.page_requested.connect(self._on_organizer_page_requested)
        self._page_organizer.page_selection_changed.connect(self._on_organizer_selection_changed)
        self._page_organizer.visible_thumbnail_pages_changed.connect(
            self._on_visible_thumbnail_pages_changed
        )

        self._workspace_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._workspace_splitter.setObjectName("documentWorkspaceSplitter")
        self._workspace_splitter.addWidget(self._page_organizer)
        self._workspace_splitter.addWidget(self._scroll_area)
        self._workspace_splitter.setStretchFactor(0, 0)
        self._workspace_splitter.setStretchFactor(1, 1)
        self._workspace_splitter.setCollapsible(0, False)
        self._workspace_splitter.setCollapsible(1, False)
        self._workspace_splitter.setSizes([208, 892])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._workspace_splitter)

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
        text_index_progress = getattr(self._render_service, "text_index_progress", None)
        if text_index_progress is not None:
            text_index_progress.connect(self._on_text_index_progress)
        text_index_completed = getattr(self._render_service, "text_index_completed", None)
        if text_index_completed is not None:
            text_index_completed.connect(self._on_text_index_completed)

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

    @property
    def selected_page_indexes(self) -> tuple[int, ...]:
        return self._page_organizer.selected_page_indexes

    @property
    def document_id(self) -> str:
        return self._document_id

    @property
    def generation(self) -> int:
        return self._generation

    def open_document(self, path: Path) -> None:
        resolved_path = path.expanduser().resolve()
        self._path = resolved_path
        self._pending_restore_page_index = None
        self._pending_restore_selected_page_indexes = None
        self._pending_restore_query = None
        self._mutation_reload_active = False
        self._clear_document_render_state_for_reload(clear_query=True)
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

    def reload_document(
        self,
        *,
        restore_page_index: int | None = None,
        restore_selected_page_indexes: tuple[int, ...] | None = None,
        clear_query: bool = False,
    ) -> bool:
        if self._path is None:
            return False
        resolved_path = self._path.expanduser().resolve()
        target_page_index = (
            self._current_page_index if restore_page_index is None else restore_page_index
        )
        selected_page_indexes = (
            self.selected_page_indexes
            if restore_selected_page_indexes is None
            else tuple(restore_selected_page_indexes)
        )
        self._pending_restore_page_index = target_page_index
        self._pending_restore_selected_page_indexes = selected_page_indexes
        self._pending_restore_query = self._search_query if not clear_query else None
        self._clear_document_render_state_for_reload(clear_query=clear_query)
        self._bump_generation()
        self._show_status("PDFを再読み込み中です")
        try:
            revision = DocumentRevision.from_path(resolved_path)
        except OSError as exc:
            self._pending_restore_page_index = None
            self._pending_restore_selected_page_indexes = None
            self._pending_restore_query = None
            self._show_error(str(exc))
            return False
        self._render_service.open_document(
            self._document_id,
            resolved_path,
            self._generation,
            revision,
        )
        return True

    def release_renderer_backend(self, timeout_ms: int = 3000) -> DocumentReleaseResult:
        return self._render_service.release_document(
            self._document_id,
            self._generation,
            timeout_ms,
        )

    def suspend_for_working_copy_mutation(self) -> PdfViewMutationSnapshot:
        snapshot = PdfViewMutationSnapshot(
            current_page_index=self._current_page_index,
            selected_page_indexes=self.selected_page_indexes,
            logical_zoom=self._logical_zoom,
            search_query=self._search_query,
        )
        self._mutation_suspended = True
        self._visible_timer.stop()
        self._page_organizer.stop_pending_thumbnail_updates()
        self._desired_pages.clear()
        self._expected_main_render_keys.clear()
        self._page_organizer.set_desired_thumbnail_pages(())
        self._advance_render_generation()
        return snapshot

    def reload_after_working_copy_mutation(self, snapshot: PdfViewMutationSnapshot) -> bool:
        if self._path is None:
            self._mutation_suspended = False
            self.mutation_reload_completed.emit(False)
            return False
        self._logical_zoom = snapshot.logical_zoom
        self._mutation_reload_active = True
        self._pending_restore_query = snapshot.search_query
        reloaded = self.reload_document(
            restore_page_index=snapshot.current_page_index,
            restore_selected_page_indexes=snapshot.selected_page_indexes,
            clear_query=False,
        )
        if not reloaded:
            self._mutation_reload_active = False
            self._mutation_suspended = False
            self.mutation_reload_completed.emit(False)
            return False
        return True

    def transition_render_cache(
        self,
        old_revision: DocumentRevision,
        new_revision: DocumentRevision,
        *,
        affected_pages: frozenset[int],
    ) -> None:
        self._render_service.transition_cache_revision(
            old_revision,
            new_revision,
            affected_pages=affected_pages,
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
        self._set_current_page_index(target)
        self._page_organizer.set_current_page(target)
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
        self._expected_main_render_keys.clear()
        self._page_organizer.set_desired_thumbnail_pages(())
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
        self._page_organizer.schedule_visible_thumbnail_update(force=True)
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
        self._expected_main_render_keys.clear()
        self._page_organizer.set_desired_thumbnail_pages(())
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
        self._page_organizer.schedule_visible_thumbnail_update(force=True)
        self.state_changed.emit()

    def set_search_overlay_inset(self, inset: int) -> None:
        target = max(24, inset)
        if target == self._search_top_inset:
            return
        self._search_top_inset = target
        self._canvas.set_top_inset(target)
        self._content.adjustSize()
        self._schedule_visible_page_update()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._release_mouse_grab()
        self._bump_generation()
        self._render_service.close_document(self._document_id, self._generation)

    def close_document(self) -> None:
        if self._closed:
            return
        self._path = None
        self._clear_document_render_state_for_reload(clear_query=True)
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
        self._clear_document_render_state_for_reload(clear_query=True)
        self._show_status(message, render_state="error")
        self.error_occurred.emit(message)

    def _on_document_loaded(self, document_id: object, generation: int, metadata: object) -> None:
        if document_id != self._document_id or generation != self._generation or self._closed:
            return
        if not isinstance(metadata, DocumentMetadata):
            raise TypeError("metadata must be DocumentMetadata")
        self._metadata = metadata
        self._index_total_page_count = metadata.page_count
        self._indexed_page_count = 0
        self._failed_page_count = 0
        self._indexing_completed = False
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
        self._page_organizer.set_document(metadata.pages)
        self._set_current_page_index(0, emit=False)
        self._apply_pending_reload_restore()
        self._status_label.hide()
        self._canvas.show()
        self._content.adjustSize()
        self._schedule_visible_page_update()
        self._page_organizer.schedule_visible_thumbnail_update(force=True)
        self._refresh_page_overlays()
        self._mutation_suspended = False
        if self._mutation_reload_active:
            self._mutation_reload_active = False
            self.mutation_reload_completed.emit(True)
        self.document_loaded.emit()
        self.state_changed.emit()

    def _on_document_failed(self, document_id: object, generation: int, message: str) -> None:
        if document_id != self._document_id or generation != self._generation or self._closed:
            return
        self._pending_restore_page_index = None
        self._pending_restore_selected_page_indexes = None
        self._pending_restore_query = None
        self._mutation_suspended = False
        if self._mutation_reload_active:
            self._mutation_reload_active = False
            self.mutation_reload_completed.emit(False)
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
        ):
            return
        if self._expected_main_render_keys.get(
            result.page_index
        ) == result.cache_key and 0 <= result.page_index < len(self._canvas.pages):
            placeholder = self._canvas.pages[result.page_index]
            pixmap = QPixmap.fromImage(result.image)
            placeholder.set_pixmap(pixmap)
            self._refresh_page_overlays(result.page_index)
            self._update_current_page()
            self.state_changed.emit()
            return
        if self._page_organizer.apply_thumbnail(result.page_index, result.cache_key, result.image):
            self.state_changed.emit()

    def _on_render_failed(self, failure: object) -> None:
        if not isinstance(failure, RenderFailure):
            raise TypeError("failure must be RenderFailure")
        if (
            failure.document_id != self._document_id
            or failure.generation != self._generation
            or self._closed
            or self._metadata is None
        ):
            return
        if self._expected_main_render_keys.get(
            failure.page_index
        ) == failure.cache_key and 0 <= failure.page_index < len(self._canvas.pages):
            self._canvas.pages[failure.page_index].set_state(
                PlaceholderState.ERROR,
                failure.message,
            )
            self.error_occurred.emit(failure.message)
            self.state_changed.emit()
            return
        if self._page_organizer.apply_thumbnail_failure(
            failure.page_index,
            failure.cache_key,
            failure.message,
        ):
            self.state_changed.emit()

    def _on_text_index_ready(
        self,
        document_id: object,
        revision: object,
        page_text: object,
    ) -> None:
        if document_id != self._document_id or self._closed:
            return
        if not isinstance(revision, DocumentRevision):
            raise TypeError("revision must be DocumentRevision")
        if not isinstance(page_text, PageTextIndex):
            raise TypeError("page_text must be PageTextIndex")
        if self._metadata is None:
            return
        if revision != self._metadata.revision:
            return
        if page_text.revision != revision:
            return
        self._text_indexes[page_text.page_index] = page_text
        self._normalized_page_texts[page_text.page_index] = self._normalize_page_text(page_text)
        self._indexed_page_count = len(self._text_indexes)
        self._update_page_text_flags(page_text)
        should_focus = False
        if self._search_query:
            should_focus = self._recompute_search_matches(
                activate_first_match=self._current_match_index < 0,
                preserve_current=True,
            )
        self.search_state_changed.emit()
        if should_focus and self._current_match_index >= 0:
            self._goto_match(self._current_match_index)
        else:
            self._refresh_page_overlays()

    def _on_text_index_progress(
        self,
        document_id: object,
        revision: object,
        indexed_pages: int,
        total_pages: int,
        failed_pages: int,
    ) -> None:
        if document_id != self._document_id or self._closed:
            return
        if not isinstance(revision, DocumentRevision):
            raise TypeError("revision must be DocumentRevision")
        if self._metadata is None or revision != self._metadata.revision:
            return
        self._indexed_page_count = indexed_pages
        self._index_total_page_count = total_pages
        self._failed_page_count = failed_pages
        self._indexing_completed = indexed_pages + failed_pages >= total_pages
        self.search_state_changed.emit()

    def _on_text_index_completed(
        self,
        document_id: object,
        revision: object,
        indexed_pages: int,
        failed_pages: int,
    ) -> None:
        if document_id != self._document_id or self._closed:
            return
        if not isinstance(revision, DocumentRevision):
            raise TypeError("revision must be DocumentRevision")
        if self._metadata is None or revision != self._metadata.revision:
            return
        self._indexed_page_count = indexed_pages
        self._failed_page_count = failed_pages
        self._indexing_completed = True
        self.search_state_changed.emit()

    def _on_text_index_failed(
        self,
        document_id: object,
        revision: object,
        page_index: int,
        message: str,
    ) -> None:
        if document_id != self._document_id or self._closed:
            return
        if not isinstance(revision, DocumentRevision):
            raise TypeError("revision must be DocumentRevision")
        if self._metadata is None or revision != self._metadata.revision:
            return
        self._failed_page_count += 1
        self.error_occurred.emit(f"テキスト索引の作成に失敗しました: {page_index + 1}ページ")
        self.search_state_changed.emit()
        if 0 <= page_index < len(self._canvas.pages):
            self._canvas.pages[page_index].update()

    def search(self, query: str) -> int:
        previous_query = self._search_query
        self._search_query = query.strip()
        should_focus = self._recompute_search_matches(
            activate_first_match=bool(self._search_query),
            preserve_current=self._search_query == previous_query,
        )
        if should_focus and self._current_match_index >= 0:
            self._goto_match(self._current_match_index)
        else:
            self._refresh_page_overlays()
        self.search_state_changed.emit()
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
    def search_state(self) -> PdfSearchState:
        return PdfSearchState(
            query=self._search_query,
            current_index=0 if not self._search_matches else self._current_match_index + 1,
            total_count=len(self._search_matches),
            indexed_pages=self._indexed_page_count,
            total_pages=self._index_total_page_count,
            failed_pages=self._failed_page_count,
            indexing_completed=self._indexing_completed,
            text_pages_with_content=len(self._text_page_indexes),
            image_only_pages=len(self._image_only_page_indexes),
            empty_text_pages=len(self._empty_text_page_indexes),
        )

    @property
    def selected_text(self) -> str:
        if self._selection is None:
            return ""
        start, end = self._selection.normalized()
        parts: list[str] = []
        for page_index in range(start.page_index, end.page_index + 1):
            text_index = self._text_indexes.get(page_index)
            if text_index is None:
                parts.append("")
                continue
            if page_index == start.page_index == end.page_index:
                parts.append(
                    self._text_for_pdfium_range(
                        text_index,
                        start.pdfium_index,
                        end.pdfium_index,
                    )
                )
            elif page_index == start.page_index:
                parts.append(self._text_for_pdfium_range(text_index, start.pdfium_index, None))
            elif page_index == end.page_index:
                parts.append(self._text_for_pdfium_range(text_index, None, end.pdfium_index))
            else:
                parts.append(self._text_for_pdfium_range(text_index, None, None))
        return "\n".join(parts)

    def _goto_match(self, match_index: int) -> None:
        if not 0 <= match_index < len(self._search_matches):
            return
        match = self._search_matches[match_index]
        self._set_current_page_index(match.page_index)
        self._page_organizer.set_current_page(match.page_index)
        content_rect = self._match_rect_in_content(match)
        if content_rect is None:
            self._scroll_to_page(match.page_index)
        else:
            self._scroll_area.ensureVisible(
                round(content_rect.center().x()),
                round(content_rect.center().y()),
                60,
                60,
            )
        self._schedule_visible_page_update()
        self._refresh_page_overlays()
        self.state_changed.emit()

    def _match_rect_in_content(self, match: SearchMatch) -> QRectF | None:
        if self._metadata is None or not 0 <= match.page_index < len(self._canvas.pages):
            return None
        if not match.boxes:
            return None
        page = self._canvas.pages[match.page_index]
        box = next((candidate for candidate in match.boxes if candidate is not None), None)
        if box is None:
            return None
        mapper = page._coordinate_mapper()
        box_in_page_content = mapper.pdf_to_view_rect(box)
        page_origin_in_content = page.mapTo(self._content, QPoint(0, 0))
        page_content_origin_in_content = (
            QPointF(page_origin_in_content) + page.page_content_rect().topLeft()
        )
        return QRectF(
            page_content_origin_in_content + box_in_page_content.topLeft(),
            box_in_page_content.size(),
        )

    def _recompute_search_matches(
        self,
        *,
        activate_first_match: bool,
        preserve_current: bool,
    ) -> bool:
        query = self._search_query.strip()
        current_match_key: tuple[int, int, int] | None = None
        if preserve_current and 0 <= self._current_match_index < len(self._search_matches):
            current_match = self._search_matches[self._current_match_index]
            current_match_key = (
                current_match.page_index,
                current_match.start_pdfium_index,
                current_match.end_pdfium_index,
            )
        self._search_matches.clear()
        self._current_match_index = -1
        if not query or not self._text_indexes:
            return False
        lowered = query.casefold()
        for page_index in sorted(self._text_indexes):
            normalized = self._normalized_page_texts.get(page_index)
            text_index = self._text_indexes[page_index]
            if normalized is None:
                continue
            start = 0
            while True:
                found = normalized.text.find(lowered, start)
                if found == -1:
                    break
                end = found + len(lowered)
                source_indexes = normalized.source_character_offsets[found:end]
                unique_source_offsets = tuple(dict.fromkeys(source_indexes))
                source_characters = [
                    text_index.characters[offset] for offset in unique_source_offsets
                ]
                if not source_characters:
                    start = found + max(1, len(lowered))
                    continue
                boxes = tuple(self._merge_characters_into_runs(source_characters))
                self._search_matches.append(
                    SearchMatch(
                        page_index=page_index,
                        start_pdfium_index=source_characters[0].pdfium_index,
                        end_pdfium_index=source_characters[-1].pdfium_index + 1,
                        text="".join(character.text for character in source_characters).replace(
                            "\x00", ""
                        ),
                        boxes=boxes,
                    )
                )
                start = found + max(1, len(lowered))
        self._search_matches.sort(
            key=lambda match: (
                match.page_index,
                match.start_pdfium_index,
                match.end_pdfium_index,
            )
        )
        if current_match_key is not None:
            for index, match in enumerate(self._search_matches):
                if (
                    match.page_index,
                    match.start_pdfium_index,
                    match.end_pdfium_index,
                ) == current_match_key:
                    self._current_match_index = index
                    return False
        if activate_first_match and self._search_matches:
            self._current_match_index = 0
            return True
        return False

    def _normalize_page_text(self, text_index: PageTextIndex) -> NormalizedPageText:
        normalized_parts: list[str] = []
        source_character_offsets: list[int] = []
        for index, character in enumerate(text_index.characters):
            folded = character.text.replace("\x00", "").casefold()
            normalized_parts.append(folded)
            source_character_offsets.extend([index] * len(folded))
        return NormalizedPageText(
            text="".join(normalized_parts),
            source_character_offsets=tuple(source_character_offsets),
        )

    @staticmethod
    def _text_for_pdfium_range(
        text_index: PageTextIndex,
        start_pdfium_index: int | None,
        end_pdfium_index: int | None,
    ) -> str:
        characters = sorted(text_index.characters, key=lambda character: character.pdfium_index)
        selected = [
            character.text
            for character in characters
            if (
                (start_pdfium_index is None or character.pdfium_index >= start_pdfium_index)
                and (end_pdfium_index is None or character.pdfium_index <= end_pdfium_index)
            )
        ]
        return "".join(selected).replace("\x00", "")

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
            start_position, end_position = self._selection.normalized()
            for index, text_index in self._text_indexes.items():
                if start_position.page_index <= index <= end_position.page_index:
                    selection_boxes_by_page[index] = self._selection_boxes_for_page(
                        text_index,
                        index,
                        start_position,
                        end_position,
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

    def _begin_selection(self, global_position: object) -> None:
        text_position = self._text_position_from_global(
            global_position,
            allow_page_edge_fallback=False,
        )
        if text_position is None:
            self._release_mouse_grab()
            return
        self._selection_anchor = text_position
        self._selection_active = True
        self._selection = DocumentTextSelection(anchor=text_position, focus=text_position)
        self.selection_changed.emit()
        self._refresh_page_overlays()

    def _update_selection(self, global_position: object) -> None:
        if not self._selection_active or self._selection_anchor is None:
            return
        text_position = self._text_position_from_global(
            global_position,
            allow_page_edge_fallback=True,
        )
        if text_position is None:
            return
        self._selection = DocumentTextSelection(anchor=self._selection_anchor, focus=text_position)
        self.selection_changed.emit()
        self._refresh_page_overlays()

    def _finish_selection(self, global_position: object) -> None:
        self._update_selection(global_position)
        self._selection_active = False
        self._selection_anchor = None
        self._release_mouse_grab()
        self.selection_changed.emit()

    def _text_position_from_global(
        self,
        global_position: object,
        *,
        allow_page_edge_fallback: bool,
    ) -> TextPosition | None:
        if not isinstance(global_position, QPointF):
            return None
        global_point = global_position.toPoint()
        for page_index, page in enumerate(self._canvas.pages):
            page_local_point = page.mapFromGlobal(global_point)
            if not page.rect().contains(page_local_point):
                continue
            page_local = QPointF(page_local_point)
            content_local = page_local - page.page_content_rect().topLeft()
            content_rect = page.page_content_rect()
            mapper = page._coordinate_mapper()
            pdf_point = mapper.view_to_pdf_point(content_local)
            char_index = self._character_index_for_point(page_index, pdf_point, content_local)
            if (
                char_index is None
                and allow_page_edge_fallback
                and 0.0 <= content_local.x() <= content_rect.width()
            ):
                if content_local.y() < 0:
                    return self._page_edge_text_position(page_index, at_end=False)
                if content_local.y() > content_rect.height():
                    return self._page_edge_text_position(page_index, at_end=True)
            if char_index is None:
                return None
            return TextPosition(page_index=page_index, pdfium_index=char_index)
        if allow_page_edge_fallback:
            return self._gap_fallback_text_position(global_point.y())
        return None

    def _page_edge_text_position(self, page_index: int, *, at_end: bool) -> TextPosition | None:
        text_index = self._text_indexes.get(page_index)
        if text_index is None or not text_index.characters:
            return None
        characters = sorted(text_index.characters, key=lambda character: character.pdfium_index)
        character = characters[-1] if at_end else characters[0]
        return TextPosition(page_index=page_index, pdfium_index=character.pdfium_index)

    def _gap_fallback_text_position(self, global_y: int) -> TextPosition | None:
        if self._selection_anchor is None:
            return None
        anchor_page_index = self._selection_anchor.page_index
        for page_index in range(len(self._canvas.pages) - 1):
            upper = self._canvas.pages[page_index]
            lower = self._canvas.pages[page_index + 1]
            upper_top_left = upper.mapToGlobal(QPoint(0, 0))
            lower_top_left = lower.mapToGlobal(QPoint(0, 0))
            upper_bottom = upper_top_left.y() + upper.height()
            lower_top = lower_top_left.y()
            if not upper_bottom < global_y < lower_top:
                continue
            if anchor_page_index <= page_index:
                return self._page_edge_text_position(page_index, at_end=True)
            return self._page_edge_text_position(page_index + 1, at_end=False)
        return None

    def _character_index_for_point(
        self,
        page_index: int,
        point: PdfPoint,
        view_point: QPointF,
    ) -> int | None:
        text_index = self._text_indexes.get(page_index)
        if text_index is None or not text_index.characters:
            return None
        best_index = None
        best_distance = float("inf")
        mapper = self._canvas.pages[page_index]._coordinate_mapper()
        for character in text_index.characters:
            box = character.box
            if box is None:
                continue
            view_rect = mapper.pdf_to_view_rect(box)
            if view_rect.contains(view_point):
                return character.pdfium_index
            distance = self._distance_to_rect(view_rect, view_point)
            if distance <= 6.0 and distance < best_distance:
                best_distance = distance
                best_index = character.pdfium_index
        return best_index

    def _selection_boxes_for_page(
        self,
        text_index: PageTextIndex,
        page_index: int,
        start: TextPosition,
        end: TextPosition,
    ) -> list[PdfRect]:
        characters: list[TextCharacterBox] = []
        for character in text_index.characters:
            if page_index == start.page_index and page_index == end.page_index:
                if start.pdfium_index <= character.pdfium_index <= end.pdfium_index:
                    characters.append(character)
            elif page_index == start.page_index:
                if character.pdfium_index >= start.pdfium_index:
                    characters.append(character)
            elif page_index == end.page_index:
                if character.pdfium_index <= end.pdfium_index:
                    characters.append(character)
            elif start.page_index < page_index < end.page_index:
                characters.append(character)
        return self._merge_characters_into_runs(characters)

    @staticmethod
    def _distance_to_rect(rect: QRectF, point: QPointF) -> float:
        dx = max(rect.left() - point.x(), 0.0, point.x() - rect.right())
        dy = max(rect.top() - point.y(), 0.0, point.y() - rect.bottom())
        return math.hypot(dx, dy)

    def _schedule_visible_page_update(self) -> None:
        if self._metadata is None or self._closed or self._mutation_suspended:
            return
        self._visible_timer.start()

    def _clear_document_render_state_for_reload(self, *, clear_query: bool) -> None:
        self._release_mouse_grab()
        self._set_current_page_index(0, emit=False)
        self._desired_pages.clear()
        self._expected_main_render_keys.clear()
        self._page_organizer.clear()
        self._canvas.set_pages([])
        self._metadata = None
        if clear_query:
            self._clear_text_state(clear_query=True)
            self._pending_restore_query = None

    def _apply_pending_reload_restore(self) -> None:
        if self.page_count <= 0:
            self._pending_restore_page_index = None
            self._pending_restore_selected_page_indexes = None
            self._pending_restore_query = None
            return
        if self._pending_restore_page_index is None:
            return
        clamped_page_index = _clamp(self._pending_restore_page_index, 0, self.page_count - 1)
        selected_page_indexes = self._pending_restore_selected_page_indexes or ()
        self._pending_restore_page_index = None
        self._pending_restore_selected_page_indexes = None
        if self._pending_restore_query is not None:
            self._search_query = self._pending_restore_query
        self._pending_restore_query = None
        self.set_page(clamped_page_index)
        valid_selection = tuple(
            sorted(
                {
                    page_index
                    for page_index in selected_page_indexes
                    if 0 <= page_index < self.page_count
                }
            )
        )
        if not valid_selection:
            valid_selection = (clamped_page_index,)
        current_selection_index = (
            clamped_page_index if clamped_page_index in valid_selection else valid_selection[-1]
        )
        self._page_organizer.set_selected_page_indexes(
            valid_selection,
            current_index=current_selection_index,
        )

    def _request_visible_pages(self) -> None:
        if self._metadata is None or self._path is None or self._closed:
            return
        try:
            revision = DocumentRevision.from_path(self._path)
        except OSError as exc:
            self._show_error(str(exc))
            return
        if revision != self._metadata.revision:
            self._clear_document_render_state_for_reload(clear_query=True)
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
            self._page_organizer.set_desired_thumbnail_pages(())
            return
        self._desired_pages = {request.page_index for request in requests}
        self._expected_main_render_keys = {
            page_index: key
            for page_index, key in self._expected_main_render_keys.items()
            if page_index in self._desired_pages
        }
        for request in requests:
            page = self._canvas.pages[request.page_index]
            if page.state is PlaceholderState.DISPLAYED:
                continue
            if page.state is PlaceholderState.NOT_REQUESTED:
                page.set_state(PlaceholderState.QUEUED)
            else:
                page.set_state(PlaceholderState.RENDERING)
            cache_key = RenderCacheKey(
                revision=revision,
                page_index=request.page_index,
                logical_zoom=self._logical_zoom,
                rotation=self._rotation,
                device_pixel_ratio=self.devicePixelRatioF(),
            )
            self._expected_main_render_keys[request.page_index] = cache_key
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
        self._request_visible_thumbnails(revision)
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
            self._set_current_page_index(0)
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
        self._set_current_page_index(best_page)
        self._page_organizer.set_current_page(best_page)

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

    def _set_current_page_index(self, page_index: int, *, emit: bool = True) -> bool:
        if self._metadata is None:
            if page_index != 0:
                raise ValueError("page index must be zero before document metadata is available")
        elif not 0 <= page_index < self.page_count:
            raise ValueError("page index is out of range")
        if page_index == self._current_page_index:
            return False
        self._current_page_index = page_index
        if emit:
            self.current_page_changed.emit(page_index)
        return True

    def _on_organizer_page_requested(self, page_index: int) -> None:
        self.set_page(page_index)

    def _on_organizer_selection_changed(self, _page_indexes: object) -> None:
        self.state_changed.emit()

    def _on_visible_thumbnail_pages_changed(self, page_indexes: object) -> None:
        if self._mutation_suspended:
            return
        if not isinstance(page_indexes, tuple):
            return
        desired_page_indexes = self._page_organizer.set_desired_thumbnail_pages(page_indexes)
        if self._metadata is None or self._path is None:
            return
        if desired_page_indexes:
            self._request_visible_thumbnails(self._metadata.revision)

    def _request_visible_thumbnails(self, revision: DocumentRevision) -> None:
        if self._metadata is None:
            return
        desired_page_indexes = self._page_organizer.desired_thumbnail_pages
        if not desired_page_indexes:
            return
        visible_pages = set(self._page_organizer.visible_page_indexes)
        for page_index in desired_page_indexes:
            logical_zoom = self._thumbnail_logical_zoom(page_index)
            cache_key = RenderCacheKey(
                revision=revision,
                page_index=page_index,
                logical_zoom=logical_zoom,
                rotation=self._rotation,
                device_pixel_ratio=self.devicePixelRatioF(),
            )
            needs_request = self._page_organizer.prepare_thumbnail_request(
                page_index,
                cache_key,
                rendering=page_index in visible_pages,
            )
            if not needs_request:
                continue
            self._render_service.request_render(
                RenderRequest(
                    document_id=self._document_id,
                    generation=self._generation,
                    page_index=page_index,
                    logical_zoom=logical_zoom,
                    rotation=self._rotation,
                    device_pixel_ratio=self.devicePixelRatioF(),
                    priority=(
                        _THUMBNAIL_PRIORITY_VISIBLE
                        if page_index in visible_pages
                        else _THUMBNAIL_PRIORITY_PREFETCH
                    ),
                    revision=revision,
                )
            )

    def _thumbnail_logical_zoom(self, page_index: int) -> float:
        if self._metadata is None:
            return _THUMBNAIL_MAX_LOGICAL_ZOOM
        metadata = self._metadata.pages[page_index]
        mapper = PageCoordinateMapper(
            geometry=metadata.geometry,
            additional_rotation=self._rotation,
            logical_zoom=1.0,
            device_pixel_ratio=1.0,
        )
        base_size = mapper.view_size
        width = max(1.0, base_size.width())
        height = max(1.0, base_size.height())
        logical_zoom = min(
            _THUMBNAIL_MAX_LOGICAL_ZOOM,
            _THUMBNAIL_TARGET_WIDTH / width,
            _THUMBNAIL_TARGET_HEIGHT / height,
        )
        if not math.isfinite(logical_zoom) or logical_zoom <= 0:
            return _THUMBNAIL_MAX_LOGICAL_ZOOM
        return logical_zoom

    def _clear_text_state(self, *, clear_query: bool) -> None:
        self._release_mouse_grab()
        self._text_indexes.clear()
        self._normalized_page_texts.clear()
        self._search_matches.clear()
        self._current_match_index = -1
        self._selection = None
        self._selection_anchor = None
        self._selection_active = False
        self._indexed_page_count = 0
        self._failed_page_count = 0
        self._index_total_page_count = 0
        self._indexing_completed = False
        self._text_page_indexes.clear()
        self._empty_text_page_indexes.clear()
        self._image_only_page_indexes.clear()
        if clear_query:
            self._search_query = ""
        self._refresh_page_overlays()
        self.search_state_changed.emit()
        self.selection_changed.emit()

    def _update_page_text_flags(self, page_text: PageTextIndex) -> None:
        page_index = page_text.page_index
        self._text_page_indexes.discard(page_index)
        self._empty_text_page_indexes.discard(page_index)
        self._image_only_page_indexes.discard(page_index)
        if page_text.text.strip():
            self._text_page_indexes.add(page_index)
            return
        self._empty_text_page_indexes.add(page_index)
        if page_text.has_image_content:
            self._image_only_page_indexes.add(page_index)

    @staticmethod
    def _merge_characters_into_runs(characters: list[TextCharacterBox]) -> list[PdfRect]:
        boxes = [character for character in characters if character.box is not None]
        if not boxes:
            return []
        runs: list[PdfRect] = []
        current_run = boxes[0].box
        if current_run is None:
            return []
        previous_character = boxes[0]
        for character in boxes[1:]:
            box = character.box
            if box is None:
                continue
            if PdfView._should_merge_boxes(previous_character, character):
                current_run = PdfRect(
                    left=min(current_run.left, box.left),
                    bottom=min(current_run.bottom, box.bottom),
                    right=max(current_run.right, box.right),
                    top=max(current_run.top, box.top),
                )
            else:
                runs.append(current_run)
                current_run = box
            previous_character = character
        runs.append(current_run)
        return runs

    @staticmethod
    def _should_merge_boxes(
        previous_character: TextCharacterBox,
        current_character: TextCharacterBox,
    ) -> bool:
        previous_box = previous_character.box
        current_box = current_character.box
        if previous_box is None or current_box is None:
            return False
        vertical_overlap = min(previous_box.top, current_box.top) - max(
            previous_box.bottom,
            current_box.bottom,
        )
        minimum_height = min(previous_box.height, current_box.height)
        same_line = vertical_overlap >= minimum_height * _LINE_RUN_VERTICAL_OVERLAP_RATIO
        if not same_line:
            same_line = abs(previous_box.center.y - current_box.center.y) <= minimum_height * (
                1.0 - _LINE_RUN_VERTICAL_OVERLAP_RATIO
            )
        if not same_line:
            return False
        gap = current_box.left - previous_box.right
        gap_limit = (
            max(
                previous_box.height,
                current_box.height,
                min(previous_box.width, current_box.width),
            )
            * _LINE_RUN_GAP_MULTIPLIER
        )
        return gap <= gap_limit

    @staticmethod
    def _release_mouse_grab() -> None:
        grabber = QWidget.mouseGrabber()
        if grabber is not None:
            grabber.releaseMouse()

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
