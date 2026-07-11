from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QRect, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QToolButton,
    QWidget,
)

from pdf_workbench.ui.icon_provider import IconName, IconProvider, IconTone


@dataclass(frozen=True, slots=True)
class ToolbarState:
    has_document: bool
    page_index: int
    page_count: int
    zoom_factor: float


class ChevronComboBox(QComboBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chevron_icon = IconProvider.icon(IconName.CHEVRON_DOWN, tone=IconTone.MUTED, size=14)
        self.installEventFilter(self)

    def refresh_theme_assets(self) -> None:
        self._chevron_icon = IconProvider.icon(IconName.CHEVRON_DOWN, tone=IconTone.MUTED, size=14)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        arrow_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxArrow,
            self,
        )
        target = QRect(
            arrow_rect.center().x() - 7,
            arrow_rect.center().y() - 7,
            14,
            14,
        )
        self._chevron_icon.paint(painter, target)
        painter.end()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self and event.type() == QEvent.Type.EnabledChange:
            self.update()
        return super().eventFilter(watched, event)


class DocumentToolbar(QWidget):
    open_requested = Signal()
    search_requested = Signal()
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
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        self.open_button = QPushButton("開く", self)
        self.open_button.setObjectName("openPdfButton")
        self.open_button.setAccessibleName("Open PDF")
        self.open_button.setToolTip("PDFを開く")
        self.open_button.clicked.connect(self.open_requested.emit)
        root.addWidget(self.open_button)

        self.search_button = self._icon_button(
            "Search document",
            "検索 (⌘F / Ctrl+F)",
            object_name="openSearchButton",
        )
        self.search_button.clicked.connect(self.search_requested.emit)
        root.addWidget(self.search_button)

        root.addWidget(self._separator())

        self.previous_button = self._icon_button(
            "Previous page",
            "前のページ",
            object_name="previousPageButton",
        )
        self.previous_button.clicked.connect(self.previous_requested.emit)
        root.addWidget(self.previous_button)

        self.page_field = QSpinBox(self)
        self.page_field.setObjectName("pageNumberInput")
        self.page_field.setAccessibleName("Page number")
        self.page_field.setToolTip("ページ番号")
        self.page_field.setMinimum(0)
        self.page_field.setMaximum(0)
        self.page_field.setSpecialValueText("—")
        self.page_field.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.page_field.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_field.setFixedWidth(58)
        self.page_field.valueChanged.connect(self._emit_page_requested)
        page_editor = self.page_field.findChild(QLineEdit)
        if page_editor is not None:
            page_editor.setObjectName("pageNumberEditor")
            page_editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
            page_editor.setFrame(False)
        root.addWidget(self.page_field)

        self._page_label = QLabel("/ 0", self)
        self._page_label.setObjectName("pageTotalLabel")
        self._page_label.setAccessibleName("Page total")
        self._page_label.setProperty("muted", True)
        root.addWidget(self._page_label)

        self.next_button = self._icon_button(
            "Next page",
            "次のページ",
            object_name="nextPageButton",
        )
        self.next_button.clicked.connect(self.next_requested.emit)
        root.addWidget(self.next_button)

        root.addWidget(self._separator())

        self.zoom_out_button = self._icon_button(
            "Zoom out",
            "ズームを縮小",
            object_name="zoomOutButton",
        )
        self.zoom_out_button.clicked.connect(lambda: self._nudge_zoom(-1 / 6))
        root.addWidget(self.zoom_out_button)

        self.zoom_field = ChevronComboBox(self)
        self.zoom_field.setObjectName("zoomControl")
        self.zoom_field.setAccessibleName("Zoom control")
        self.zoom_field.setToolTip("ズーム倍率")
        self.zoom_field.setEditable(True)
        self.zoom_field.setFixedWidth(94)
        self.zoom_field.addItems(["50%", "75%", "100%", "125%", "150%", "200%", "300%", "400%"])
        self.zoom_field.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.zoom_field.setCurrentText("100%")
        self._last_zoom_text = "100%"
        self.zoom_field.activated.connect(self._commit_zoom_from_index)
        line_edit = self.zoom_field.lineEdit()
        if line_edit is None:
            raise RuntimeError("editable combo box is missing its line edit")
        line_edit.setObjectName("zoomControlEditor")
        line_edit.setFrame(False)
        line_edit.editingFinished.connect(self._commit_zoom_from_editor)
        line_edit.returnPressed.connect(self._commit_zoom_from_editor)
        root.addWidget(self.zoom_field)

        self.zoom_in_button = self._icon_button(
            "Zoom in",
            "ズームを拡大",
            object_name="zoomInButton",
        )
        self.zoom_in_button.clicked.connect(lambda: self._nudge_zoom(0.2))
        root.addWidget(self.zoom_in_button)

        root.addWidget(self._separator())

        self.rotate_button = self._icon_button(
            "Rotate clockwise",
            "時計回りに回転",
            object_name="rotateClockwiseButton",
        )
        self.rotate_button.clicked.connect(self.rotate_requested.emit)
        root.addWidget(self.rotate_button)

        root.addStretch(1)

        self.setFixedHeight(54)
        self.refresh_theme_assets()
        self.setState(ToolbarState(False, 0, 0, 1.0))

    def setState(self, state: ToolbarState) -> None:
        self.open_button.setEnabled(True)
        self.search_button.setEnabled(state.has_document)
        self.previous_button.setEnabled(state.has_document and state.page_index > 0)
        self.next_button.setEnabled(state.has_document and state.page_index + 1 < state.page_count)
        self.rotate_button.setEnabled(state.has_document)
        self.zoom_out_button.setEnabled(state.has_document)
        self.zoom_in_button.setEnabled(state.has_document)
        self.page_field.setEnabled(state.has_document)
        self.zoom_field.setEnabled(state.has_document)

        with QSignalBlocker(self.page_field):
            if state.has_document:
                self.page_field.setSpecialValueText("")
                self.page_field.setMinimum(1)
                self.page_field.setMaximum(max(1, state.page_count))
                self.page_field.setValue(max(1, state.page_index + 1))
            else:
                self.page_field.setSpecialValueText("—")
                self.page_field.setMinimum(0)
                self.page_field.setMaximum(0)
                self.page_field.setValue(0)
        self._page_label.setText(f"/ {state.page_count}")
        display_text = f"{round(state.zoom_factor * 100)}%"
        self._set_zoom_text(display_text, remember=True)

    def refresh_theme_assets(self) -> None:
        self.open_button.setIcon(IconProvider.icon(IconName.OPEN, tone=IconTone.INVERSE, size=18))
        self.search_button.setIcon(IconProvider.icon(IconName.SEARCH, size=18))
        self.previous_button.setIcon(IconProvider.icon(IconName.CHEVRON_LEFT, size=18))
        self.next_button.setIcon(IconProvider.icon(IconName.CHEVRON_RIGHT, size=18))
        self.zoom_out_button.setIcon(IconProvider.icon(IconName.ZOOM_OUT, size=18))
        self.zoom_in_button.setIcon(IconProvider.icon(IconName.ZOOM_IN, size=18))
        self.rotate_button.setIcon(IconProvider.icon(IconName.ROTATE_CLOCKWISE, size=18))
        self.zoom_field.refresh_theme_assets()

    def _icon_button(
        self,
        accessible_name: str,
        tooltip: str,
        *,
        object_name: str,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(object_name)
        button.setAccessibleName(accessible_name)
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        return button

    def _separator(self) -> QFrame:
        separator = QFrame(self)
        separator.setObjectName("toolbarSeparator")
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        return separator

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


def button_has_icon(button: QPushButton | QToolButton) -> bool:
    icon: QIcon = button.icon()
    return not icon.isNull()
