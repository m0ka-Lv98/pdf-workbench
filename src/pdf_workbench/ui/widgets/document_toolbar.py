from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QSignalBlocker, Qt, Signal
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
        self.setAccessibleName("Document toolbar")
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(8)

        self.open_button = self._button(
            "PDFを開く",
            QStyle.StandardPixmap.SP_DialogOpenButton,
            role="primary",
            object_name="openPdfButton",
            tooltip="PDFを開く",
        )
        self.previous_button = self._button(
            "前へ",
            QStyle.StandardPixmap.SP_ArrowUp,
            object_name="previousPageButton",
            tooltip="前のページ",
        )
        self.next_button = self._button(
            "次へ",
            QStyle.StandardPixmap.SP_ArrowDown,
            object_name="nextPageButton",
            tooltip="次のページ",
        )
        self.rotate_button = self._button(
            "回転",
            QStyle.StandardPixmap.SP_BrowserReload,
            object_name="rotateClockwiseButton",
            tooltip="時計回りに回転",
        )
        self.zoom_out_button = self._button(
            "縮小",
            QStyle.StandardPixmap.SP_ArrowBack,
            object_name="zoomOutButton",
            tooltip="ズームを縮小",
        )
        self.zoom_in_button = self._button(
            "拡大",
            QStyle.StandardPixmap.SP_ArrowForward,
            object_name="zoomInButton",
            tooltip="ズームを拡大",
        )

        self.page_field = QSpinBox(self)
        self.page_field.setObjectName("pageNumberInput")
        self.page_field.setAccessibleName("Page number")
        self.page_field.setToolTip("ページ番号")
        self.page_field.setMinimum(0)
        self.page_field.setMaximum(0)
        self.page_field.setSpecialValueText("—")
        self.page_field.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.page_field.valueChanged.connect(self._emit_page_requested)

        self._page_label = QLabel("/ 0", self)
        self._page_label.setObjectName("pageTotalLabel")
        self._page_label.setAccessibleName("Page total")
        self._page_label.setProperty("muted", True)

        self.zoom_field = QComboBox(self)
        self.zoom_field.setObjectName("zoomControl")
        self.zoom_field.setAccessibleName("Zoom control")
        self.zoom_field.setToolTip("ズーム倍率")
        self.zoom_field.setEditable(True)
        self.zoom_field.addItems(["50%", "75%", "100%", "125%", "150%", "200%", "300%", "400%"])
        self.zoom_field.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.zoom_field.setCurrentText("100%")
        self._last_zoom_text = "100%"
        self.zoom_field.activated.connect(self._commit_zoom_from_index)
        line_edit = self.zoom_field.lineEdit()
        if line_edit is None:
            raise RuntimeError("editable combo box is missing its line edit")
        line_edit.editingFinished.connect(self._commit_zoom_from_editor)
        line_edit.returnPressed.connect(self._commit_zoom_from_editor)

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
        self.zoom_out_button.clicked.connect(lambda: self._nudge_zoom(-1 / 6))
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
        self.setState(ToolbarState(False, 0, 0, 1.0))

    def setState(self, state: ToolbarState) -> None:
        self.open_button.setEnabled(True)
        self.previous_button.setEnabled(state.has_document and state.page_index > 0)
        self.next_button.setEnabled(state.has_document and state.page_index + 1 < state.page_count)
        self.rotate_button.setEnabled(state.has_document)
        self.zoom_out_button.setEnabled(state.has_document)
        self.zoom_in_button.setEnabled(state.has_document)
        self.page_field.setEnabled(state.has_document)
        self.zoom_field.setEnabled(state.has_document)

        with QSignalBlocker(self.page_field):
            if state.has_document:
                self.page_field.setMaximum(max(1, state.page_count))
                self.page_field.setValue(max(1, state.page_index + 1))
            else:
                self.page_field.setMaximum(0)
                self.page_field.setValue(0)
        self._page_label.setText(f"/ {state.page_count}")

        display_text = f"{round(state.zoom_factor * 100)}%"
        self._set_zoom_text(display_text, remember=True)

    def _button(
        self,
        text: str,
        icon: QStyle.StandardPixmap,
        *,
        object_name: str,
        tooltip: str,
        role: str = "outline",
    ) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(object_name)
        button.setAccessibleName(text)
        button.setToolTip(tooltip)
        button.setText(text)
        button.setIcon(self.style().standardIcon(icon))
        button.setProperty("buttonRole", role)
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
        if self.page_field.isEnabled():
            self.page_requested.emit(max(0, value - 1))

    def _commit_zoom_from_index(self, index: int) -> None:
        self._commit_zoom_text(self.zoom_field.itemText(index))

    def _commit_zoom_from_editor(self) -> None:
        self._commit_zoom_text(self.zoom_field.currentText())

    def _commit_zoom_text(self, text: str) -> None:
        value = self._parse_zoom(text)
        if value is None:
            self._set_zoom_text(self._last_zoom_text, remember=False)
            return
        normalized = self._format_zoom(value)
        if normalized == self._last_zoom_text:
            self._set_zoom_text(normalized, remember=False)
            return
        self._last_zoom_text = normalized
        self._set_zoom_text(normalized, remember=False)
        self.zoom_requested.emit(value)

    def _set_zoom_text(self, text: str, *, remember: bool) -> None:
        with QSignalBlocker(self.zoom_field):
            self.zoom_field.setCurrentText(text)
        if remember:
            self._last_zoom_text = text

    def _nudge_zoom(self, delta: float) -> None:
        value = self._parse_zoom(self._last_zoom_text)
        if value is None:
            return
        target = max(0.25, min(value * (1.0 + delta), 5.0))
        self._commit_zoom_text(self._format_zoom(target))

    @staticmethod
    def _parse_zoom(text: str) -> float | None:
        cleaned = text.strip().rstrip("%")
        if not cleaned:
            return None
        try:
            value = float(cleaned)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        if value < 25 or value > 500:
            return None
        return value / 100.0

    @staticmethod
    def _format_zoom(value: float) -> str:
        return f"{round(value * 100)}%"
