from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QStyle,
    QToolButton,
    QWidget,
)


@dataclass(frozen=True, slots=True)
class ToolbarState:
    has_document: bool
    page_index: int
    page_count: int
    zoom_factor: float


class DocumentToolbar(QWidget):
    open_requested = Signal()
    previous_requested = Signal()
    next_requested = Signal()
    rotate_requested = Signal()
    page_requested = Signal(int)
    zoom_requested = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("documentToolbar")
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(8)

        self.open_button = self._button(
            "PDFを開く",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            primary=True,
        )
        self.previous_button = self._button("前へ", QStyle.StandardPixmap.SP_ArrowUp)
        self.next_button = self._button("次へ", QStyle.StandardPixmap.SP_ArrowDown)
        self.rotate_button = self._button("回転", QStyle.StandardPixmap.SP_BrowserReload)
        self.zoom_out_button = self._button("縮小", QStyle.StandardPixmap.SP_ArrowBack)
        self.zoom_in_button = self._button("拡大", QStyle.StandardPixmap.SP_ArrowForward)

        self.page_field = QSpinBox(self)
        self.page_field.setMinimum(1)
        self.page_field.setMaximum(9999)
        self.page_field.setPrefix("P ")
        self.page_field.setSuffix(" / 0")
        self.page_field.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.page_field.valueChanged.connect(self._emit_page_requested)

        self.zoom_field = QComboBox(self)
        self.zoom_field.setEditable(True)
        self.zoom_field.addItems(["50%", "75%", "100%", "125%", "150%", "200%"])
        self.zoom_field.setCurrentText("150%")
        self.zoom_field.currentTextChanged.connect(self._emit_zoom_requested)
        self._last_zoom_text = "150%"

        self._page_label = QLabel(" / 0", self)
        self._page_label.setProperty("muted", True)

        self._group(root, self.open_button)
        self._group(root, self.previous_button, self.page_field, self._page_label, self.next_button)
        self._group(
            root,
            self.zoom_out_button,
            self.zoom_field,
            self.zoom_in_button,
            self.rotate_button,
        )

        self.open_button.clicked.connect(self.open_requested.emit)
        self.previous_button.clicked.connect(self.previous_requested.emit)
        self.next_button.clicked.connect(self.next_requested.emit)
        self.rotate_button.clicked.connect(self.rotate_requested.emit)
        self.zoom_out_button.clicked.connect(lambda: self._nudge_zoom(-0.2))
        self.zoom_in_button.clicked.connect(lambda: self._nudge_zoom(0.2))

        for button in (
            self.open_button,
            self.previous_button,
            self.next_button,
            self.rotate_button,
            self.zoom_out_button,
            self.zoom_in_button,
        ):
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        self.setFixedHeight(48)
        self.setState(ToolbarState(False, 0, 0, 1.5))

    def setState(self, state: ToolbarState) -> None:
        self.open_button.setEnabled(True)
        self.previous_button.setEnabled(state.has_document and state.page_index > 0)
        self.next_button.setEnabled(state.has_document and state.page_index + 1 < state.page_count)
        self.rotate_button.setEnabled(state.has_document)
        self.zoom_out_button.setEnabled(state.has_document)
        self.zoom_in_button.setEnabled(state.has_document)
        self.page_field.setEnabled(state.has_document)
        self.zoom_field.setEnabled(state.has_document)
        self.page_field.blockSignals(True)
        self.page_field.setMaximum(max(1, state.page_count))
        self.page_field.setValue(max(1, state.page_index + 1))
        self.page_field.setSuffix(f" / {state.page_count}")
        self.page_field.blockSignals(False)
        self.zoom_field.blockSignals(True)
        self.zoom_field.setCurrentText(f"{round(state.zoom_factor * 100)}%")
        self._last_zoom_text = self.zoom_field.currentText()
        self.zoom_field.blockSignals(False)

    def _button(
        self,
        text: str,
        icon: QStyle.StandardPixmap,
        *,
        primary: bool = False,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setIcon(self.style().standardIcon(icon))
        button.setProperty("variant", "primary" if primary else "secondary")
        return button

    def _group(self, layout: QHBoxLayout, *widgets: QWidget) -> None:
        frame = QFrame(self)
        frame.setObjectName("toolbarGroup")
        group_layout = QHBoxLayout(frame)
        group_layout.setContentsMargins(4, 0, 4, 0)
        group_layout.setSpacing(6)
        for widget in widgets:
            group_layout.addWidget(widget)
        layout.addWidget(frame)

    def _emit_page_requested(self, value: int) -> None:
        self.page_requested.emit(value - 1)

    def _emit_zoom_requested(self, text: str) -> None:
        value = self._parse_zoom(text)
        if value is not None:
            self._last_zoom_text = text
            self.zoom_requested.emit(value)
            return
        self.zoom_field.blockSignals(True)
        self.zoom_field.setCurrentText(self._last_zoom_text)
        self.zoom_field.blockSignals(False)

    def _nudge_zoom(self, delta: float) -> None:
        value = self._parse_zoom(self.zoom_field.currentText())
        if value is None:
            return
        value = max(0.25, min(value + delta, 5.0))
        self.zoom_requested.emit(value)

    @staticmethod
    def _parse_zoom(text: str) -> float | None:
        cleaned = text.strip().rstrip("%")
        if not cleaned:
            return None
        try:
            return float(cleaned) / 100.0
        except ValueError:
            return None
