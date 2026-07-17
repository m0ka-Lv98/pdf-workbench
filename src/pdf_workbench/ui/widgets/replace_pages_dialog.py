from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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
class ReplacePagesDialogResult:
    page_selection: SourcePageSelection


class ReplacePagesDialog(QDialog):
    def __init__(
        self,
        source_path: Path,
        source_page_count: int,
        target_page_numbers: tuple[int, ...],
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("別のPDFで選択ページを置換")
        self.setModal(True)
        self._source_page_count = source_page_count
        self._target_page_numbers = target_page_numbers
        self._dialog_result: ReplacePagesDialogResult | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        summary = QLabel(
            "\n".join(
                (
                    f"置換元PDF: {source_path.name}",
                    f"全ページ数: {source_page_count}",
                    "置換対象ページ: "
                    + ", ".join(str(page_number) for page_number in target_page_numbers),
                )
            ),
            self,
        )
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(summary)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)

        self.page_range_edit = QLineEdit(self)
        self.page_range_edit.setObjectName("replaceSourcePageRangeEdit")
        self.page_range_edit.setPlaceholderText("all または 1,3-5")
        self.page_range_edit.setText("all")
        form.addRow("置換元ページ", self.page_range_edit)

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
    def dialog_result(self) -> ReplacePagesDialogResult | None:
        return self._dialog_result

    def _accept_with_validation(self) -> None:
        try:
            selection = parse_source_page_selection(
                self._source_page_count,
                self.page_range_edit.text(),
            )
            if len(selection.page_indexes) != len(self._target_page_numbers):
                raise ValueError("置換元ページ数は置換対象ページ数と一致する必要があります")
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self._dialog_result = ReplacePagesDialogResult(page_selection=selection)
        self.accept()
