from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from pdf_workbench.domain.page_insertion import SourcePageSelection, parse_source_page_selection


@dataclass(frozen=True, slots=True)
class InsertPagesDialogResult:
    page_selection: SourcePageSelection
    insertion_slot: int


class InsertPagesDialog(QDialog):
    def __init__(
        self,
        source_path: Path,
        source_page_count: int,
        insertion_targets: tuple[tuple[str, int], ...],
        *,
        default_index: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("別のPDFからページを挿入")
        self.setModal(True)
        self._source_page_count = source_page_count
        self._insertion_targets = insertion_targets
        self._dialog_result: InsertPagesDialogResult | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        summary = QLabel(
            f"挿入元PDF: {source_path.name}\n全ページ数: {source_page_count}",
            self,
        )
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(summary)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)

        self.page_range_edit = QLineEdit(self)
        self.page_range_edit.setObjectName("insertSourcePageRangeEdit")
        self.page_range_edit.setPlaceholderText("all または 1,3-5")
        self.page_range_edit.setText("all")
        form.addRow("挿入するページ", self.page_range_edit)

        self.insertion_combo = QComboBox(self)
        self.insertion_combo.setObjectName("insertPositionCombo")
        for label, slot in insertion_targets:
            self.insertion_combo.addItem(label, slot)
        if 0 <= default_index < len(insertion_targets):
            self.insertion_combo.setCurrentIndex(default_index)
        form.addRow("挿入位置", self.insertion_combo)

        form_host = QWidget(self)
        form_host.setLayout(form)
        root.addWidget(form_host)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        self.button_box.accepted.connect(self._accept_with_validation)
        self.button_box.rejected.connect(self.reject)
        button_row.addWidget(self.button_box)
        root.addLayout(button_row)

    @property
    def dialog_result(self) -> InsertPagesDialogResult | None:
        return self._dialog_result

    def _accept_with_validation(self) -> None:
        try:
            selection = parse_source_page_selection(
                self._source_page_count,
                self.page_range_edit.text(),
            )
            current_index = self.insertion_combo.currentIndex()
            if not 0 <= current_index < len(self._insertion_targets):
                raise ValueError("挿入位置が不正です")
            insertion_slot = int(self.insertion_combo.currentData())
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self._dialog_result = InsertPagesDialogResult(
            page_selection=selection,
            insertion_slot=insertion_slot,
        )
        self.accept()
