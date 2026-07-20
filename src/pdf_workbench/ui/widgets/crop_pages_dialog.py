from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from pdf_workbench.domain.page_crop import PageCropMargins


@dataclass(frozen=True, slots=True)
class CropPagesDialogResult:
    margins: PageCropMargins
    reset_to_media_box: bool


class CropPagesDialog(QDialog):
    def __init__(
        self,
        *,
        selected_page_count: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("選択ページをトリミング")
        self.setModal(True)
        self._dialog_result: CropPagesDialogResult | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        summary = QLabel(
            "\n".join(
                (
                    f"選択ページ数: {selected_page_count}",
                    "基準は現在のCropBoxです。",
                    "余白は表示上の左・上・右・下として扱います。",
                    "サイズや回転が異なるページにも同じpoint余白を適用します。",
                )
            ),
            self,
        )
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(summary)

        self.explanation_label = QLabel(
            "トリミングは表示範囲だけを変更します。\n範囲外の内容や注釈はPDFから削除されません。",
            self,
        )
        self.explanation_label.setObjectName("cropExplanationLabel")
        self.explanation_label.setWordWrap(True)
        root.addWidget(self.explanation_label)

        self.reset_checkbox = QCheckBox("MediaBox全体に戻す", self)
        self.reset_checkbox.setObjectName("cropResetCheckBox")
        self.reset_checkbox.toggled.connect(self._update_field_enabled_state)
        root.addWidget(self.reset_checkbox)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)

        self.left_margin_spin = self._create_margin_spin_box("cropLeftMarginSpinBox")
        self.top_margin_spin = self._create_margin_spin_box("cropTopMarginSpinBox")
        self.right_margin_spin = self._create_margin_spin_box("cropRightMarginSpinBox")
        self.bottom_margin_spin = self._create_margin_spin_box("cropBottomMarginSpinBox")

        form.addRow("左余白 (pt)", self.left_margin_spin)
        form.addRow("上余白 (pt)", self.top_margin_spin)
        form.addRow("右余白 (pt)", self.right_margin_spin)
        form.addRow("下余白 (pt)", self.bottom_margin_spin)

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
        self.button_box.accepted.connect(self._accept_with_result)
        self.button_box.rejected.connect(self.reject)
        button_row.addWidget(self.button_box)
        root.addLayout(button_row)
        self._update_field_enabled_state(False)

    @property
    def dialog_result(self) -> CropPagesDialogResult | None:
        return self._dialog_result

    def _accept_with_result(self) -> None:
        self._dialog_result = CropPagesDialogResult(
            margins=PageCropMargins(
                left=self.left_margin_spin.value(),
                top=self.top_margin_spin.value(),
                right=self.right_margin_spin.value(),
                bottom=self.bottom_margin_spin.value(),
            ),
            reset_to_media_box=self.reset_checkbox.isChecked(),
        )
        self.accept()

    def _update_field_enabled_state(self, checked: bool) -> None:
        enabled = not checked
        for spin_box in (
            self.left_margin_spin,
            self.top_margin_spin,
            self.right_margin_spin,
            self.bottom_margin_spin,
        ):
            spin_box.setEnabled(enabled)

    @staticmethod
    def _create_margin_spin_box(object_name: str) -> QDoubleSpinBox:
        spin_box = QDoubleSpinBox()
        spin_box.setObjectName(object_name)
        spin_box.setDecimals(2)
        spin_box.setRange(0.0, 1000000.0)
        spin_box.setSingleStep(1.0)
        spin_box.setAccelerated(True)
        spin_box.setSuffix(" pt")
        return spin_box
