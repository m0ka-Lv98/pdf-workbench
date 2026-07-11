from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QToolButton, QWidget


@dataclass(frozen=True, slots=True)
class SearchBarState:
    query: str
    current_index: int
    total_count: int
    progress_text: str


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
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        self.search_input = QLineEdit(self)
        self.search_input.setObjectName("searchInput")
        self.search_input.setPlaceholderText("検索")
        self.search_input.setAccessibleName("Search input")
        self.search_input.setToolTip("検索語句を入力します")
        self.search_input.setMinimumWidth(320)
        self.search_input.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.search_input.textChanged.connect(self._on_text_changed)

        self.previous_button = QToolButton(self)
        self.previous_button.setObjectName("previousSearchResultButton")
        self.previous_button.setText("↑")
        self.previous_button.setAccessibleName("Previous search result")
        self.previous_button.setToolTip("前の検索結果")
        self.previous_button.clicked.connect(self.previous_requested.emit)

        self.next_button = QToolButton(self)
        self.next_button.setObjectName("nextSearchResultButton")
        self.next_button.setText("↓")
        self.next_button.setAccessibleName("Next search result")
        self.next_button.setToolTip("次の検索結果")
        self.next_button.clicked.connect(self.next_requested.emit)

        self.counter_label = QLabel("0 / 0", self)
        self.counter_label.setObjectName("searchResultCounter")
        self.counter_label.setAccessibleName("Search result counter")
        self.counter_label.setToolTip("検索結果の件数")
        self.progress_label = QLabel("", self)
        self.progress_label.setObjectName("searchProgressLabel")
        self.progress_label.setAccessibleName("Search progress")
        self.progress_label.setToolTip("索引作成の進捗")

        self.close_button = QToolButton(self)
        self.close_button.setObjectName("closeSearchButton")
        self.close_button.setText("\u00d7")
        self.close_button.setAccessibleName("Close search")
        self.close_button.setToolTip("検索バーを閉じる")
        self.close_button.clicked.connect(self.close_requested.emit)

        layout.addWidget(self.search_input)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.counter_label)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.close_button)

        self.search_input.installEventFilter(self)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_state(self, state: SearchBarState) -> None:
        blocker = QSignalBlocker(self.search_input)
        self.search_input.setText(state.query)
        del blocker
        self.counter_label.setText(f"{state.current_index} / {state.total_count}")
        self.progress_label.setText(state.progress_text)

    def focus_search(self) -> None:
        self.show()
        self.activateWindow()
        self.search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def cancel_pending_search(self) -> None:
        self._search_timer.stop()

    def submit_current_query(self) -> None:
        self._search_timer.stop()
        self.search_requested.emit(self.search_input.text())

    def _on_text_changed(self, _text: str) -> None:
        self._search_timer.start()

    def _emit_debounced_search(self) -> None:
        self.submit_current_query()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.search_input and event.type() == QEvent.Type.KeyPress:
            key_event = event if isinstance(event, QKeyEvent) else None
            if key_event is None:
                return super().eventFilter(watched, event)
            if key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
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
