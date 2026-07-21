from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pdf_workbench.domain.page_split import (
    PageSplitPlan,
    build_max_pages_split_plan,
    build_page_range_split_plan,
    build_split_target_path,
)


@dataclass(frozen=True, slots=True)
class SplitPdfDialogResult:
    plan: PageSplitPlan
    output_directory: Path
    overwrite: bool
    mode: str


class SplitPdfDialog(QDialog):
    def __init__(
        self,
        *,
        source_path: Path,
        page_count: int,
        default_output_directory: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("PDFを分割")
        self.setModal(True)
        self._source_path = source_path
        self._page_count = page_count
        self._dialog_result: SplitPdfDialogResult | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.summary_label = QLabel(
            f"{source_path.name} ({page_count}ページ) を複数PDFへ分割します。",
            self,
        )
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        self.range_mode_radio = QRadioButton("ページ範囲で分割", self)
        self.range_mode_radio.setObjectName("splitRangeModeRadio")
        self.max_pages_mode_radio = QRadioButton("1ファイルあたりの最大ページ数", self)
        self.max_pages_mode_radio.setObjectName("splitMaxPagesModeRadio")
        self.range_mode_radio.toggled.connect(self._update_mode_state)
        root.addWidget(self.range_mode_radio)
        root.addWidget(self.max_pages_mode_radio)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)
        self.range_edit = QPlainTextEdit(self)
        self.range_edit.setObjectName("splitPageRangesEdit")
        self.range_edit.setPlaceholderText("例:\n1-3\n4-7\n8-10")
        self.range_edit.setFixedHeight(92)
        self.range_edit.textChanged.connect(self._update_preview)
        form.addRow("分割範囲", self.range_edit)

        self.max_pages_spin = QSpinBox(self)
        self.max_pages_spin.setObjectName("splitMaxPagesSpin")
        self.max_pages_spin.setMinimum(1)
        self.max_pages_spin.setMaximum(max(1, page_count))
        self.max_pages_spin.setValue(max(1, min(5, page_count - 1)))
        self.max_pages_spin.valueChanged.connect(self._update_preview)
        form.addRow("最大ページ数", self.max_pages_spin)

        directory_row = QHBoxLayout()
        self.output_directory_edit = QLineEdit(str(default_output_directory), self)
        self.output_directory_edit.setObjectName("splitOutputDirectoryEdit")
        self.output_directory_edit.textChanged.connect(self._update_preview)
        browse_button = QPushButton("選択…", self)
        browse_button.clicked.connect(self._choose_output_directory)
        directory_row.addWidget(self.output_directory_edit, 1)
        directory_row.addWidget(browse_button)
        directory_host = QWidget(self)
        directory_host.setLayout(directory_row)
        form.addRow("出力フォルダ", directory_host)
        root.addLayout(form)

        self.overwrite_checkbox = QCheckBox("既存の同名ファイルを上書きする", self)
        self.overwrite_checkbox.setObjectName("splitOverwriteCheckBox")
        self.overwrite_checkbox.toggled.connect(self._update_preview)
        root.addWidget(self.overwrite_checkbox)

        self.feedback_label = QLabel("", self)
        self.feedback_label.setObjectName("splitValidationFeedback")
        self.feedback_label.setWordWrap(True)
        root.addWidget(self.feedback_label)

        self.preview_label = QLabel("", self)
        self.preview_label.setObjectName("splitPreviewLabel")
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.preview_label)

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

        self.range_mode_radio.setChecked(True)
        self.range_edit.setPlainText(f"1-{page_count // 2}\n{page_count // 2 + 1}-{page_count}")
        self._update_mode_state()
        self._update_preview()

    @property
    def dialog_result(self) -> SplitPdfDialogResult | None:
        return self._dialog_result

    def _current_mode(self) -> str:
        return "range" if self.range_mode_radio.isChecked() else "max_pages"

    def _build_plan(self) -> PageSplitPlan:
        source_stem = self._source_path.stem
        if self._current_mode() == "range":
            return build_page_range_split_plan(
                self._page_count,
                self.range_edit.toPlainText(),
                source_stem=source_stem,
            )
        return build_max_pages_split_plan(
            self._page_count,
            self.max_pages_spin.value(),
            source_stem=source_stem,
        )

    def _output_directory(self) -> Path:
        return Path(self.output_directory_edit.text()).expanduser().resolve()

    def _update_mode_state(self) -> None:
        range_mode = self._current_mode() == "range"
        self.range_edit.setEnabled(range_mode)
        self.max_pages_spin.setEnabled(not range_mode)
        self._update_preview()

    def _update_preview(self) -> None:
        try:
            plan = self._build_plan()
            output_directory = self._output_directory()
        except (OSError, TypeError, ValueError) as exc:
            self.feedback_label.setText(str(exc))
            self.preview_label.setText("")
            self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return
        lines = [
            f"{chunk.display_range}: {build_split_target_path(output_directory, chunk).name}"
            for chunk in plan.chunks
        ]
        self.feedback_label.setText(f"{plan.output_count}個のPDFを作成します。")
        self.preview_label.setText("\n".join(lines))
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def _choose_output_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "分割PDFの出力フォルダ",
            self.output_directory_edit.text(),
        )
        if directory:
            self.output_directory_edit.setText(directory)

    def _accept_with_validation(self) -> None:
        try:
            plan = self._build_plan()
            output_directory = self._output_directory()
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self._dialog_result = SplitPdfDialogResult(
            plan=plan,
            output_directory=output_directory,
            overwrite=self.overwrite_checkbox.isChecked(),
            mode=self._current_mode(),
        )
        self.accept()
