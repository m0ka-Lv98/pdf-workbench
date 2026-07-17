from __future__ import annotations

import logging
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractListModel,
    QEvent,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QDrag,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from pdf_workbench.domain.page_reorder import PageReorderNoOpError, build_page_reorder_plan
from pdf_workbench.services.page_coordinates import PageMetadata
from pdf_workbench.services.pdf_renderer import RenderCacheKey

_INVALID_MODEL_INDEX = QModelIndex()
_REORDER_MIME = "application/x-pdf-workbench-page-reorder"
logger = logging.getLogger(__name__)


class _OrganizerRoles:
    PAGE_NUMBER = Qt.ItemDataRole.UserRole + 1
    THUMBNAIL = Qt.ItemDataRole.UserRole + 2
    THUMBNAIL_STATE = Qt.ItemDataRole.UserRole + 3
    MESSAGE = Qt.ItemDataRole.UserRole + 4
    CURRENT = Qt.ItemDataRole.UserRole + 5


@dataclass(slots=True)
class ThumbnailEntry:
    page_index: int
    metadata: PageMetadata
    state: str = "not_requested"
    image: QImage | None = None
    message: str = "待機中"
    current: bool = False


class PageOrganizerModel(QAbstractListModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries: list[ThumbnailEntry] = []

    def rowCount(
        self,
        parent: QModelIndex | QPersistentModelIndex = _INVALID_MODEL_INDEX,
    ) -> int:
        if parent.isValid():
            return 0
        return len(self._entries)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid() or not 0 <= index.row() < len(self._entries):
            return None
        entry = self._entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return f"Page {entry.page_index + 1}"
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"Page {entry.page_index + 1}"
        if role == _OrganizerRoles.PAGE_NUMBER:
            return entry.page_index + 1
        if role == _OrganizerRoles.THUMBNAIL:
            return entry.image
        if role == _OrganizerRoles.THUMBNAIL_STATE:
            return entry.state
        if role == _OrganizerRoles.MESSAGE:
            return entry.message
        if role == _OrganizerRoles.CURRENT:
            return entry.current
        return None

    def flags(
        self,
        index: QModelIndex | QPersistentModelIndex,
    ) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid():
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        return flags

    def set_entries(self, metadata: tuple[PageMetadata, ...]) -> None:
        self.beginResetModel()
        self._entries = [
            ThumbnailEntry(page_index=index, metadata=item) for index, item in enumerate(metadata)
        ]
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._entries = []
        self.endResetModel()

    def set_current_page(self, page_index: int) -> None:
        changed_rows: list[int] = []
        for index, entry in enumerate(self._entries):
            new_value = index == page_index
            if entry.current != new_value:
                entry.current = new_value
                changed_rows.append(index)
        for row in changed_rows:
            model_index = self.index(row)
            self.dataChanged.emit(
                model_index,
                model_index,
                [_OrganizerRoles.CURRENT],
            )

    def set_thumbnail_state(self, page_index: int, state: str, message: str = "待機中") -> None:
        if not 0 <= page_index < len(self._entries):
            return
        entry = self._entries[page_index]
        if entry.state == state and entry.message == message and entry.image is None:
            return
        entry.state = state
        entry.message = message
        entry.image = None
        self.dataChanged.emit(
            self.index(page_index),
            self.index(page_index),
            [_OrganizerRoles.THUMBNAIL, _OrganizerRoles.THUMBNAIL_STATE, _OrganizerRoles.MESSAGE],
        )

    def set_thumbnail_image(self, page_index: int, image: QImage) -> None:
        if not 0 <= page_index < len(self._entries):
            return
        entry = self._entries[page_index]
        if entry.state == "displayed" and entry.message == "" and entry.image is image:
            return
        entry.image = image
        entry.state = "displayed"
        entry.message = ""
        self.dataChanged.emit(
            self.index(page_index),
            self.index(page_index),
            [_OrganizerRoles.THUMBNAIL, _OrganizerRoles.THUMBNAIL_STATE, _OrganizerRoles.MESSAGE],
        )

    def clear_thumbnail(self, page_index: int) -> None:
        if not 0 <= page_index < len(self._entries):
            return
        entry = self._entries[page_index]
        if entry.image is None and entry.state == "not_requested" and entry.message == "待機中":
            return
        entry.image = None
        entry.state = "not_requested"
        entry.message = "待機中"
        self.dataChanged.emit(
            self.index(page_index),
            self.index(page_index),
            [_OrganizerRoles.THUMBNAIL, _OrganizerRoles.THUMBNAIL_STATE, _OrganizerRoles.MESSAGE],
        )


class PageOrganizerListView(QListView):
    visible_thumbnail_pages_changed = Signal(object)
    page_requested = Signal(int)
    page_reorder_requested = Signal(object, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageOrganizerList")
        self.setAccessibleName("Document pages")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setSpacing(10)
        self.setMouseTracking(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._visible_timer = QTimer(self)
        self._visible_timer.setSingleShot(True)
        self._visible_timer.setInterval(0)
        self._visible_timer.timeout.connect(self._emit_visible_pages)
        self.verticalScrollBar().valueChanged.connect(self._schedule_visible_pages)
        self._visible_rows: tuple[int, ...] = ()
        self._last_emitted_pages: tuple[int, ...] | None = None
        self._reordering_enabled = True
        self._drag_source_rows: tuple[int, ...] = ()

    def setModel(self, model: QAbstractItemModel | None) -> None:
        previous = self.selectionModel()
        super().setModel(model)
        if previous is not None:
            with suppress(Exception):
                previous.currentChanged.disconnect(self._on_current_changed)
        current = self.selectionModel()
        if current is not None:
            current.currentChanged.connect(self._on_current_changed)
        self.schedule_visible_thumbnail_update(force=True)

    def selectionCommand(
        self,
        index: QModelIndex | QPersistentModelIndex,
        event: QEvent | None = None,
    ) -> QItemSelectionModel.SelectionFlag:
        if event is not None and hasattr(event, "modifiers"):
            modifiers = event.modifiers()
            if modifiers & Qt.KeyboardModifier.MetaModifier:
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    return (
                        QItemSelectionModel.SelectionFlag.Clear
                        | QItemSelectionModel.SelectionFlag.SelectCurrent
                        | QItemSelectionModel.SelectionFlag.Rows
                    )
                return (
                    QItemSelectionModel.SelectionFlag.Toggle
                    | QItemSelectionModel.SelectionFlag.Rows
                )
        return super().selectionCommand(index, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        super().mousePressEvent(event)
        self._schedule_visible_pages()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._accept_reorder_event(event):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._accept_reorder_event(event):
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        if not self._accept_reorder_event(event):
            event.ignore()
            return
        model = self.model()
        if model is None:
            event.ignore()
            return
        source_rows = self._decode_reorder_rows(
            event.mimeData(),
            row_count=model.rowCount(),
        )
        if not source_rows:
            event.ignore()
            return
        insertion_slot = self.insertion_slot_for_position(event.position().toPoint())
        try:
            build_page_reorder_plan(model.rowCount(), source_rows, insertion_slot)
        except PageReorderNoOpError:
            event.setDropAction(Qt.DropAction.MoveAction)
            event.accept()
            return
        except (TypeError, ValueError):
            event.ignore()
            return
        except Exception:
            logger.exception("Unexpected page reorder drop failure")
            event.ignore()
            return
        self.page_reorder_requested.emit(source_rows, insertion_slot)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        _ = supported_actions
        if not self._reordering_enabled:
            return
        model = self.model()
        if model is None or model.rowCount() <= 1:
            return
        source_rows = self.selected_rows_in_order()
        if not source_rows or len(source_rows) >= model.rowCount():
            return
        mime_data = QMimeData()
        mime_data.setData(
            _REORDER_MIME,
            ",".join(str(row) for row in source_rows).encode("ascii"),
        )
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        self._drag_source_rows = source_rows
        try:
            drag.exec(Qt.DropAction.MoveAction)
        finally:
            self._drag_source_rows = ()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_visible_pages()

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if current.isValid():
            self.page_requested.emit(current.row())

    def _schedule_visible_pages(self) -> None:
        self._visible_timer.start()

    def schedule_visible_thumbnail_update(self, *, force: bool = False) -> None:
        if force:
            self._last_emitted_pages = None
        self._schedule_visible_pages()

    def stop_pending_visible_thumbnail_update(self) -> None:
        self._visible_timer.stop()

    def _emit_visible_pages(self) -> None:
        model = self.model()
        if model is None:
            self._emit_if_changed(())
            return
        visible: list[int] = []
        for row in range(model.rowCount()):
            rect = self.visualRect(model.index(row, 0))
            if rect.isValid() and rect.bottom() >= 0 and rect.top() <= self.viewport().height():
                visible.append(row)
        if not visible:
            self._visible_rows = ()
            self._emit_if_changed(())
            return
        self._visible_rows = tuple(visible)
        prefetch: set[int] = set(visible)
        for offset in (1, 2):
            if visible[0] - offset >= 0:
                prefetch.add(visible[0] - offset)
            if visible[-1] + offset < model.rowCount():
                prefetch.add(visible[-1] + offset)
        self._emit_if_changed(tuple(sorted(prefetch)))

    def _emit_if_changed(self, page_indexes: tuple[int, ...]) -> None:
        if self._last_emitted_pages == page_indexes:
            return
        self._last_emitted_pages = page_indexes
        self.visible_thumbnail_pages_changed.emit(page_indexes)

    @property
    def visible_rows(self) -> tuple[int, ...]:
        return self._visible_rows

    def set_reordering_enabled(self, enabled: bool) -> None:
        self._reordering_enabled = bool(enabled)

    def selected_rows_in_order(self) -> tuple[int, ...]:
        selection_model = self.selectionModel()
        if selection_model is None:
            return ()
        return tuple(sorted({index.row() for index in selection_model.selectedRows()}))

    def insertion_slot_for_position(self, point: QPoint) -> int:
        model = self.model()
        if model is None or model.rowCount() == 0:
            return 0
        index = self.indexAt(point)
        if index.isValid():
            rect = self.visualRect(index)
            if point.y() < rect.center().y():
                return index.row()
            return index.row() + 1
        first_rect = self.visualRect(model.index(0, 0))
        if first_rect.isValid() and point.y() < first_rect.top():
            return 0
        return model.rowCount()

    def _accept_reorder_event(self, event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> bool:
        model = self.model()
        if (
            not self._reordering_enabled
            or model is None
            or model.rowCount() <= 1
            or event.source() is not self
            or not self._has_only_reorder_mime(event.mimeData())
        ):
            return False
        source_rows = self._decode_reorder_rows(
            event.mimeData(),
            row_count=model.rowCount(),
        )
        return bool(source_rows) and len(source_rows) < model.rowCount()

    def _has_only_reorder_mime(self, mime_data: QMimeData | None) -> bool:
        if mime_data is None:
            return False
        formats = tuple(mime_data.formats())
        return formats == (_REORDER_MIME,)

    def _decode_reorder_rows(
        self,
        mime_data: QMimeData | None,
        *,
        row_count: int | None = None,
    ) -> tuple[int, ...]:
        if not self._has_only_reorder_mime(mime_data) or mime_data is None:
            return ()
        raw_payload = bytes(mime_data.data(_REORDER_MIME).data())
        try:
            payload = raw_payload.decode("ascii").strip()
        except UnicodeDecodeError:
            return ()
        if not payload:
            return ()
        chunks = payload.split(",")
        if not chunks or any(not chunk for chunk in chunks):
            return ()
        rows: list[int] = []
        seen: set[int] = set()
        try:
            for chunk in chunks:
                if not chunk.isascii() or not chunk.isdigit():
                    return ()
                row = int(chunk)
                if row < 0 or row in seen:
                    return ()
                seen.add(row)
                rows.append(row)
        except ValueError:
            return ()
        if rows != sorted(rows):
            return ()
        if row_count is not None and any(row >= row_count for row in rows):
            return ()
        decoded_rows = tuple(rows)
        if self._drag_source_rows and decoded_rows != self._drag_source_rows:
            return ()
        return decoded_rows


class PageThumbnailDelegate(QStyledItemDelegate):
    _ROW_SIZE = QSize(168, 228)
    _THUMBNAIL_RECT = QRect(14, 10, 140, 182)

    def sizeHint(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> QSize:
        _ = (option, index)
        return self._ROW_SIZE

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        rect = option.rect.adjusted(6, 2, -6, -2)
        palette = option.palette
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        current = bool(index.data(_OrganizerRoles.CURRENT))
        surface = palette.base().color()
        border = palette.mid().color()
        selection_fill = palette.highlight().color().lighter(175)
        current_marker = palette.highlight().color()

        painter.setBrush(selection_fill if selected else surface)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(rect, 10, 10)

        thumb_rect = QRect(
            rect.left() + self._THUMBNAIL_RECT.left(),
            rect.top() + self._THUMBNAIL_RECT.top(),
            self._THUMBNAIL_RECT.width(),
            self._THUMBNAIL_RECT.height(),
        )
        painter.setBrush(palette.window().color())
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(thumb_rect, 6, 6)

        image = index.data(_OrganizerRoles.THUMBNAIL)
        state = str(index.data(_OrganizerRoles.THUMBNAIL_STATE) or "not_requested")
        message = str(index.data(_OrganizerRoles.MESSAGE) or "")
        if isinstance(image, QImage) and not image.isNull():
            pixmap = image.scaled(
                thumb_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            target = QRect(0, 0, pixmap.width(), pixmap.height())
            target.moveCenter(thumb_rect.center())
            painter.drawImage(target, pixmap)
        else:
            painter.setPen(palette.text().color())
            painter.drawText(
                thumb_rect.adjusted(8, 8, -8, -8),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                {
                    "queued": "サムネイル待機中",
                    "rendering": "サムネイル生成中",
                    "error": message or "サムネイル失敗",
                }.get(state, "サムネイル未読込"),
            )

        text_rect = QRect(rect.left() + 12, thumb_rect.bottom() + 8, rect.width() - 24, 20)
        painter.setPen(palette.text().color())
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignCenter,
            f"Page {int(index.data(_OrganizerRoles.PAGE_NUMBER) or 0)}",
        )
        if current:
            marker_rect = QRect(rect.left() + 8, rect.top() + 8, 4, rect.height() - 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(current_marker)
            painter.drawRoundedRect(marker_rect, 2, 2)
        if option.state & QStyle.StateFlag.State_HasFocus:
            focus_pen = QPen(palette.highlight().color(), 1, Qt.PenStyle.DotLine)
            painter.setPen(focus_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 8, 8)
        painter.restore()


class ThumbnailImageCache:
    def __init__(self, *, max_items: int = 96, max_bytes: int = 32 * 1024 * 1024) -> None:
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._items: OrderedDict[RenderCacheKey, QImage] = OrderedDict()
        self._total_bytes = 0

    def get(self, key: RenderCacheKey) -> QImage | None:
        image = self._items.get(key)
        if image is None:
            return None
        self._items.move_to_end(key)
        return image

    def put(self, key: RenderCacheKey, image: QImage) -> tuple[RenderCacheKey, ...]:
        evicted: list[RenderCacheKey] = []
        if key in self._items:
            existing = self._items.pop(key)
            self._total_bytes -= existing.sizeInBytes()
        self._items[key] = image
        self._total_bytes += image.sizeInBytes()
        while len(self._items) > self._max_items or (
            len(self._items) > 1 and self._total_bytes > self._max_bytes
        ):
            evicted_key, evicted_image = self._items.popitem(last=False)
            self._total_bytes -= evicted_image.sizeInBytes()
            evicted.append(evicted_key)
        return tuple(evicted)

    def clear(self) -> None:
        self._items.clear()
        self._total_bytes = 0

    @property
    def item_count(self) -> int:
        return len(self._items)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes


class PageOrganizer(QWidget):
    page_requested = Signal(int)
    page_selection_changed = Signal(object)
    pages_reorder_requested = Signal(object, int)
    visible_thumbnail_pages_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pageOrganizer")
        self.setAccessibleName("Page organizer")
        self.setMinimumWidth(150)
        self.setMaximumWidth(320)
        self.resize(208, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget(self)
        header.setObjectName("pageOrganizerHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 14, 16, 10)
        header_layout.setSpacing(8)
        self._title = QLabel("Pages", header)
        self._title.setObjectName("pageOrganizerTitle")
        self._count = QLabel("0", header)
        self._count.setObjectName("pageOrganizerCount")
        header_layout.addWidget(self._title)
        header_layout.addStretch(1)
        header_layout.addWidget(self._count)
        layout.addWidget(header)

        self._model = PageOrganizerModel(self)
        self._list = PageOrganizerListView(self)
        self._list.setItemDelegate(PageThumbnailDelegate(self._list))
        self._list.setModel(self._model)
        layout.addWidget(self._list, 1)

        self._selection_guard = False
        self._current_page_index = 0
        self._selected_page_indexes: tuple[int, ...] = ()
        self._desired_page_indexes: frozenset[int] = frozenset()
        self._expected_keys: dict[int, RenderCacheKey] = {}
        self._thumbnail_cache = ThumbnailImageCache()

        self._list.page_requested.connect(self._on_page_requested)
        self._list.page_reorder_requested.connect(self.pages_reorder_requested.emit)
        self._list.visible_thumbnail_pages_changed.connect(
            self.visible_thumbnail_pages_changed.emit
        )
        self._list.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._list.set_reordering_enabled(False)

    @property
    def list_view(self) -> QListView:
        return self._list

    @property
    def visible_page_indexes(self) -> tuple[int, ...]:
        return self._list.visible_rows

    @property
    def selected_page_indexes(self) -> tuple[int, ...]:
        return self._selected_page_indexes

    @property
    def current_page_index(self) -> int:
        return self._current_page_index

    @property
    def row_count(self) -> int:
        return self._model.rowCount()

    @property
    def desired_thumbnail_pages(self) -> tuple[int, ...]:
        return tuple(sorted(self._desired_page_indexes))

    @property
    def expected_key_count(self) -> int:
        return len(self._expected_keys)

    @property
    def thumbnail_cache_item_count(self) -> int:
        return self._thumbnail_cache.item_count

    @property
    def thumbnail_cache_total_bytes(self) -> int:
        return self._thumbnail_cache.total_bytes

    def page_display_text(self, page_index: int) -> str:
        model_index = self._model.index(page_index)
        return str(model_index.data(Qt.ItemDataRole.DisplayRole) or "")

    def thumbnail_state(self, page_index: int) -> str:
        model_index = self._model.index(page_index)
        return str(model_index.data(_OrganizerRoles.THUMBNAIL_STATE) or "")

    def thumbnail_message(self, page_index: int) -> str:
        model_index = self._model.index(page_index)
        return str(model_index.data(_OrganizerRoles.MESSAGE) or "")

    def has_thumbnail_image(self, page_index: int) -> bool:
        model_index = self._model.index(page_index)
        return isinstance(model_index.data(_OrganizerRoles.THUMBNAIL), QImage)

    def set_document(self, pages: tuple[PageMetadata, ...]) -> None:
        previous_selection = self._selected_page_indexes
        self._desired_page_indexes = frozenset()
        self._expected_keys.clear()
        self._thumbnail_cache.clear()
        self._model.set_entries(pages)
        self._count.setText(str(len(pages)))
        self._current_page_index = 0
        if pages:
            self.set_current_page(0)
            self.set_selected_page_indexes((0,), current_index=0)
        else:
            self._selected_page_indexes = ()
            if previous_selection:
                self.page_selection_changed.emit(())
        self._list.schedule_visible_thumbnail_update(force=True)

    def clear(self) -> None:
        previous_selection = self._selected_page_indexes
        self._desired_page_indexes = frozenset()
        self._expected_keys.clear()
        self._thumbnail_cache.clear()
        self._current_page_index = 0
        self._selected_page_indexes = ()
        self._count.setText("0")
        self._model.clear()
        if previous_selection:
            self.page_selection_changed.emit(())
        self._list.schedule_visible_thumbnail_update(force=True)

    def set_current_page(self, page_index: int) -> None:
        if self._model.rowCount() == 0 or not 0 <= page_index < self._model.rowCount():
            return
        self._current_page_index = page_index
        self._model.set_current_page(page_index)
        current = self._model.index(page_index)
        if current.isValid():
            self._selection_guard = True
            try:
                self._list.selectionModel().setCurrentIndex(
                    current,
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
            finally:
                self._selection_guard = False
            self._list.scrollTo(current, QAbstractItemView.ScrollHint.PositionAtCenter)

    def set_selected_page_indexes(
        self,
        page_indexes: tuple[int, ...],
        *,
        current_index: int | None = None,
    ) -> None:
        if self._list.selectionModel() is None:
            return
        valid_indexes = tuple(
            sorted(
                {
                    page_index
                    for page_index in page_indexes
                    if 0 <= page_index < self._model.rowCount()
                }
            )
        )
        if current_index is not None and not 0 <= current_index < self._model.rowCount():
            current_index = None
        if current_index is None:
            if valid_indexes:
                current_index = valid_indexes[-1]
            elif (
                self._model.rowCount() > 0
                and 0 <= self._current_page_index < self._model.rowCount()
            ):
                current_index = self._current_page_index
        self._selection_guard = True
        try:
            self._list.clearSelection()
            flags = (
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
            )
            for page_index in valid_indexes:
                model_index = self._model.index(page_index)
                if model_index.isValid():
                    self._list.selectionModel().select(model_index, flags)
            if current_index is not None:
                current = self._model.index(current_index)
                if current.isValid():
                    self._list.selectionModel().setCurrentIndex(
                        current,
                        QItemSelectionModel.SelectionFlag.NoUpdate,
                    )
        finally:
            self._selection_guard = False
        self._selected_page_indexes = valid_indexes
        self.page_selection_changed.emit(self._selected_page_indexes)

    def schedule_visible_thumbnail_update(self, *, force: bool = False) -> None:
        self._list.schedule_visible_thumbnail_update(force=force)

    def stop_pending_thumbnail_updates(self) -> None:
        self._list.stop_pending_visible_thumbnail_update()

    def set_reordering_enabled(self, enabled: bool) -> None:
        self._list.set_reordering_enabled(enabled and self._model.rowCount() > 1)

    def set_desired_thumbnail_pages(self, page_indexes: tuple[int, ...]) -> tuple[int, ...]:
        valid_indexes = tuple(
            sorted(
                {
                    page_index
                    for page_index in page_indexes
                    if 0 <= page_index < self._model.rowCount()
                }
            )
        )
        next_desired = frozenset(valid_indexes)
        previous_desired = self._desired_page_indexes
        if next_desired == previous_desired:
            return valid_indexes
        removed = previous_desired - next_desired
        self._desired_page_indexes = next_desired
        for page_index in removed:
            self._expected_keys.pop(page_index, None)
            self._model.clear_thumbnail(page_index)
        return valid_indexes

    def prepare_thumbnail_request(
        self,
        page_index: int,
        key: RenderCacheKey,
        *,
        rendering: bool,
    ) -> bool:
        if page_index not in self._desired_page_indexes:
            return False
        if not 0 <= page_index < self._model.rowCount():
            return False
        expected_key = self._expected_keys.get(page_index)
        current_state = self._model.data(
            self._model.index(page_index),
            _OrganizerRoles.THUMBNAIL_STATE,
        )
        if expected_key == key and current_state in {"queued", "rendering", "displayed"}:
            return False
        self._expected_keys[page_index] = key
        cached = self._thumbnail_cache.get(key)
        if cached is not None:
            self._model.set_thumbnail_image(page_index, cached)
            return False
        self._model.set_thumbnail_state(
            page_index,
            "rendering" if rendering else "queued",
            "サムネイル生成中" if rendering else "サムネイル待機中",
        )
        return True

    def expected_key_for_page(self, page_index: int) -> RenderCacheKey | None:
        return self._expected_keys.get(page_index)

    def apply_thumbnail(self, page_index: int, key: RenderCacheKey, image: QImage) -> bool:
        if page_index not in self._desired_page_indexes:
            return False
        if not 0 <= page_index < self._model.rowCount():
            return False
        if self._expected_keys.get(page_index) != key:
            return False
        evicted = self._thumbnail_cache.put(key, image)
        for evicted_key in evicted:
            self._clear_page_for_key(evicted_key)
        self._model.set_thumbnail_image(page_index, image)
        return True

    def apply_thumbnail_failure(self, page_index: int, key: RenderCacheKey, message: str) -> bool:
        if page_index not in self._desired_page_indexes:
            return False
        if not 0 <= page_index < self._model.rowCount():
            return False
        if self._expected_keys.get(page_index) != key:
            return False
        self._model.set_thumbnail_state(page_index, "error", message)
        return True

    def _clear_page_for_key(self, key: RenderCacheKey) -> None:
        for page_index, expected_key in self._expected_keys.items():
            if expected_key == key:
                self._model.clear_thumbnail(page_index)
                return

    def _on_selection_changed(self) -> None:
        if self._selection_guard:
            return
        indexes = sorted({index.row() for index in self._list.selectionModel().selectedRows()})
        self._selected_page_indexes = tuple(indexes)
        self.page_selection_changed.emit(self._selected_page_indexes)

    def _on_page_requested(self, page_index: int) -> None:
        if self._selection_guard:
            return
        self.page_requested.emit(page_index)
