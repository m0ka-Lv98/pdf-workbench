from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QRectF, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from pdf_workbench.ui.icon_provider import IconName, IconProvider, IconTone


@dataclass(frozen=True, slots=True)
class SearchBarState:
    query: str
    current_index: int
    total_count: int
    progress_text: str


class SearchInputSurface(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("searchInputSurface")
        self.setFixedWidth(360)
        self.setFixedHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setProperty("focused", False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(11, 0, 6, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.search_icon = QLabel(self)
        self.search_icon.setObjectName("searchIcon")
        self.search_icon.setFixedSize(18, 18)
        self.search_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.search_icon.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.search_input = QLineEdit(self)
        self.search_input.setObjectName("searchInput")
        self.search_input.setPlaceholderText("検索")
        self.search_input.setAccessibleName("Search input")
        self.search_input.setToolTip("検索語句を入力します")
        self.search_input.setFrame(False)
        self.search_input.setFixedHeight(28)
        self.search_input.setTextMargins(0, 0, 0, 0)
        self.search_input.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        self.clear_button = QToolButton(self)
        self.clear_button.setObjectName("clearSearchInputButton")
        self.clear_button.setAccessibleName("Clear search input")
        self.clear_button.setToolTip("検索語句をクリア")
        self.clear_button.setAutoRaise(True)
        self.clear_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.clear_button.setFixedSize(26, 26)
        self.clear_button.hide()

        layout.addWidget(self.search_icon)
        layout.addWidget(self.search_input, 1)
        layout.addWidget(self.clear_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.search_input.installEventFilter(self)

    def set_focused(self, focused: bool) -> None:
        if self.property("focused") == focused:
            return
        self.setProperty("focused", focused)
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.repaint()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        app = QApplication.instance()
        scheme = str(app.property("colorScheme") if app is not None else "light")
        is_dark = scheme == "dark"

        focused = bool(self.property("focused"))
        border = QColor(
            "#60a5fa"
            if is_dark and focused
            else "#2563eb"
            if focused
            else "#3a3f47"
            if is_dark
            else "#d9dde3"
        )
        background = QColor("#292c32" if is_dark else "#ffffff")

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(background)
        painter.setPen(QPen(border, 1))
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.drawRoundedRect(rect, 7, 7)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.search_input:
            if event.type() == QEvent.Type.FocusIn:
                self.set_focused(True)
            elif event.type() == QEvent.Type.FocusOut:
                self.set_focused(False)
        return super().eventFilter(watched, event)

    def refresh_theme_assets(self) -> None:
        self.repaint()


class SearchBar(QWidget):
    search_requested = Signal(str)
    next_requested = Signal()
    previous_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("searchBar")
        self.setVisible(False)
        self.setAccessibleName("Search bar")

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._emit_debounced_search)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.search_input_surface = SearchInputSurface(self)
        self.search_icon = self.search_input_surface.search_icon
        self.search_input = self.search_input_surface.search_input
        self.clear_button = self.search_input_surface.clear_button
        self.search_input.textChanged.connect(self._on_text_changed)
        self.clear_button.clicked.connect(self._clear_query)

        self.previous_button = QToolButton(self)
        self.previous_button.setObjectName("previousSearchResultButton")
        self.previous_button.setAccessibleName("Previous search result")
        self.previous_button.setToolTip("前の検索結果")
        self.previous_button.setAutoRaise(True)
        self.previous_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.previous_button.setFixedSize(34, 34)
        self.previous_button.clicked.connect(self.previous_requested.emit)

        self.next_button = QToolButton(self)
        self.next_button.setObjectName("nextSearchResultButton")
        self.next_button.setAccessibleName("Next search result")
        self.next_button.setToolTip("次の検索結果")
        self.next_button.setAutoRaise(True)
        self.next_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.next_button.setFixedSize(34, 34)
        self.next_button.clicked.connect(self.next_requested.emit)

        self.counter_label = QLabel("0 / 0", self)
        self.counter_label.setObjectName("searchResultCounter")
        self.counter_label.setAccessibleName("Search result counter")
        self.counter_label.setToolTip("検索結果の件数")
        self.counter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.counter_label.setMinimumWidth(48)
        self.progress_label = QLabel("", self)
        self.progress_label.setObjectName("searchProgressLabel")
        self.progress_label.setAccessibleName("Search progress")
        self.progress_label.setToolTip("索引作成の進捗")

        self.close_button = QToolButton(self)
        self.close_button.setObjectName("closeSearchButton")
        self.close_button.setAccessibleName("Close search")
        self.close_button.setToolTip("検索バーを閉じる")
        self.close_button.setAutoRaise(True)
        self.close_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.close_button.setFixedSize(34, 34)
        self.close_button.clicked.connect(self.close_requested.emit)

        layout.addWidget(self.search_input_surface)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.counter_label)
        layout.addWidget(self.next_button)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.close_button)

        self.search_input.installEventFilter(self)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(40)
        self.refresh_theme_assets()

    def set_state(self, state: SearchBarState) -> None:
        blocker = QSignalBlocker(self.search_input)
        self.search_input.setText(state.query)
        del blocker
        self._sync_clear_button_visibility()
        self.counter_label.setText(f"{state.current_index} / {state.total_count}")
        self.progress_label.setText(state.progress_text)
        self.progress_label.setVisible(bool(state.progress_text))

    def focus_search(self) -> None:
        self.show()
        self.activateWindow()
        self.search_input_surface.set_focused(True)
        self.search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def refresh_theme_assets(self) -> None:
        self.search_input_surface.refresh_theme_assets()
        self.search_icon.setPixmap(
            IconProvider.icon(IconName.SEARCH, tone=IconTone.MUTED, size=16).pixmap(16, 16)
        )
        self.clear_button.setIcon(IconProvider.icon(IconName.CLOSE, tone=IconTone.MUTED, size=14))
        self.previous_button.setIcon(IconProvider.icon(IconName.CHEVRON_LEFT, size=16))
        self.next_button.setIcon(IconProvider.icon(IconName.CHEVRON_RIGHT, size=16))
        self.close_button.setIcon(IconProvider.icon(IconName.CLOSE, size=16))

    def cancel_pending_search(self) -> None:
        self._search_timer.stop()

    def submit_current_query(self) -> None:
        self._search_timer.stop()
        self.search_requested.emit(self.search_input.text())

    def _on_text_changed(self, _text: str) -> None:
        self._sync_clear_button_visibility()
        self._search_timer.start()

    def _emit_debounced_search(self) -> None:
        self.submit_current_query()

    def _clear_query(self) -> None:
        self.cancel_pending_search()
        self.search_input.clear()
        self._sync_clear_button_visibility()
        self.search_requested.emit("")

    def _sync_clear_button_visibility(self) -> None:
        self.clear_button.setVisible(bool(self.search_input.text()))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.search_input and event.type() == QEvent.Type.KeyPress:
            key_event = event if isinstance(event, QKeyEvent) else None
            if key_event is None:
                return super().eventFilter(watched, event)
            if key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.submit_current_query()
                if key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.previous_requested.emit()
                else:
                    self.next_requested.emit()
                return True
            if key_event.key() == Qt.Key.Key_Escape:
                self.cancel_pending_search()
                self.close_requested.emit()
                return True
        return super().eventFilter(watched, event)
