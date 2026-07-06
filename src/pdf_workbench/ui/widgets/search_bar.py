from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QToolButton, QWidget


@dataclass(frozen=True, slots=True)
class SearchBarState:
    query: str
    current_index: int
    total_count: int
    progress_text: str


class SearchBar(QWidget):
    search_changed = Signal(str)
    next_requested = Signal()
    previous_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("searchBar")
        self.setVisible(False)
        self.setAccessibleName("Search bar")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        self.search_input = QLineEdit(self)
        self.search_input.setObjectName("searchInput")
        self.search_input.setPlaceholderText("検索")
        self.search_input.setAccessibleName("Search input")
        self.search_input.textChanged.connect(self.search_changed.emit)

        self.previous_button = QToolButton(self)
        self.previous_button.setObjectName("previousSearchResultButton")
        self.previous_button.setText("↑")
        self.previous_button.clicked.connect(self.previous_requested.emit)

        self.next_button = QToolButton(self)
        self.next_button.setObjectName("nextSearchResultButton")
        self.next_button.setText("↓")
        self.next_button.clicked.connect(self.next_requested.emit)

        self.counter_label = QLabel("0 / 0", self)
        self.counter_label.setObjectName("searchResultCounter")
        self.progress_label = QLabel("", self)
        self.progress_label.setObjectName("searchProgressLabel")

        self.close_button = QToolButton(self)
        self.close_button.setObjectName("closeSearchButton")
        self.close_button.setText("x")
        self.close_button.clicked.connect(self.close_requested.emit)

        layout.addWidget(self.search_input)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.counter_label)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.close_button)

        self.search_input.returnPressed.connect(self.next_requested.emit)
        self.search_input.installEventFilter(self)

    def set_state(self, state: SearchBarState) -> None:
        self.search_input.setText(state.query)
        self.counter_label.setText(f"{state.current_index} / {state.total_count}")
        self.progress_label.setText(state.progress_text)

    def focus_search(self) -> None:
        self.show()
        self.search_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
