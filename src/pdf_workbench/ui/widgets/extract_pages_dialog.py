from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from pdf_workbench.domain.page_extraction import (
    PageExtractionPlan,
    build_selected_page_extraction_plan,
    parse_page_range_extraction_plan,
)


@dataclass(frozen=True, slots=True)
class ExtractPagesDialogResult:
    plan: PageExtractionPlan
    mode: str


class ExtractPagesDialog(QDialog):
    def __init__(
        self,
        *,
        page_count: int,
        selected_page_indexes: tuple[int, ...],
        default_mode: str = "selection",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("PDFへページを抽出")
        self.setModal(True)
        self._page_count = page_count
        self._selected_page_indexes = selected_page_indexes
        self._dialog_result: ExtractPagesDialogResult | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.summary_label = QLabel(self)
        self.summary_label.setObjectName("extractSummaryLabel")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.summary_label)

        self.selection_mode_radio = QRadioButton("現在の選択ページ", self)
        self.selection_mode_radio.setObjectName("extractSelectionModeRadio")
        self.range_mode_radio = QRadioButton("ページ範囲", self)
        self.range_mode_radio.setObjectName("extractRangeModeRadio")
        self.selection_mode_radio.toggled.connect(self._update_mode_state)
        root.addWidget(self.selection_mode_radio)
        root.addWidget(self.range_mode_radio)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        self.range_edit = QLineEdit(self)
        self.range_edit.setObjectName("extractPageRangeEdit")
        self.range_edit.setPlaceholderText("例: 1-3, 5, 8-10")
        self.range_edit.textChanged.connect(self._update_summary)
        form.addRow("ページ範囲", self.range_edit)
        form_host = QWidget(self)
        form_host.setLayout(form)
        root.addWidget(form_host)

        self.feedback_label = QLabel("", self)
        self.feedback_label.setObjectName("extractValidationFeedback")
        self.feedback_label.setWordWrap(True)
        root.addWidget(self.feedback_label)

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

        if default_mode == "range" or not selected_page_indexes:
            self.range_mode_radio.setChecked(True)
        else:
            self.selection_mode_radio.setChecked(True)
        self._update_mode_state()
        self._update_summary()

    @property
    def dialog_result(self) -> ExtractPagesDialogResult | None:
        return self._dialog_result

    def _current_mode(self) -> str:
        return "selection" if self.selection_mode_radio.isChecked() else "range"

    def _build_plan(self) -> PageExtractionPlan:
        if self._current_mode() == "selection":
            return build_selected_page_extraction_plan(
                self._page_count,
                self._selected_page_indexes,
            )
        return parse_page_range_extraction_plan(self._page_count, self.range_edit.text())

    def _update_mode_state(self) -> None:
        selection_available = bool(self._selected_page_indexes)
        self.selection_mode_radio.setEnabled(selection_available)
        self.range_edit.setEnabled(self._current_mode() == "range")
        self._update_summary()

    def _update_summary(self) -> None:
        selected_numbers = ", ".join(str(index + 1) for index in self._selected_page_indexes)
        if not selected_numbers:
            selected_numbers = "なし"
        self.summary_label.setText(
            "\n".join(
                (
                    f"文書ページ数: {self._page_count}",
                    f"現在の選択: {selected_numbers}",
                )
            )
        )
        try:
            plan = self._build_plan()
        except (TypeError, ValueError) as exc:
            self.feedback_label.setText(str(exc))
            self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return
        self.feedback_label.setText(f"{plan.output_page_count}ページを抽出")
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def _accept_with_validation(self) -> None:
        try:
            plan = self._build_plan()
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self._dialog_result = ExtractPagesDialogResult(plan=plan, mode=self._current_mode())
        self.accept()
