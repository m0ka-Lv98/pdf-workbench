from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdf_workbench.domain.image_to_pdf import (
    ImageScalingMode,
    ImageSourceRevision,
    ImageToPdfPlan,
    PdfOrientation,
    PdfPageSizeMode,
    TransparencyPolicy,
    build_image_to_pdf_plan,
    margins_from_mm,
    mm_to_points,
)
from pdf_workbench.services.image_to_pdf import ImageSourceChangedError, InspectedImageInput
from pdf_workbench.services.pdf_save_service import TargetSnapshot


@dataclass(frozen=True, slots=True)
class ImageToPdfDialogResult:
    plan: ImageToPdfPlan
    overwrite: bool
    expected_source_revisions: dict[Path, ImageSourceRevision]
    expected_target_snapshot: TargetSnapshot


class ImageInputListWidget(QListWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def dropMimeData(
        self,
        index: int,
        data: QMimeData,
        action: Qt.DropAction,
    ) -> bool:
        accepted = super().dropMimeData(index, data, action)
        parent = self.parent()
        if accepted and isinstance(parent, ImageToPdfDialog):
            parent.update_preview()
        return accepted


class ImageToPdfDialog(QDialog):
    def __init__(
        self,
        *,
        input_reader: Callable[[Path], InspectedImageInput],
        target_snapshot_reader: Callable[[Path], TargetSnapshot] = TargetSnapshot.capture,
        is_managed_path: Callable[[Path], bool] | None = None,
        default_output_directory: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("画像からPDFを作成")
        self.setModal(True)
        self._input_reader = input_reader
        self._target_snapshot_reader = target_snapshot_reader
        self._is_managed_path = is_managed_path
        self._dialog_result: ImageToPdfDialogResult | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.summary_label = QLabel(
            "JPEG / PNG / TIFF / BMP / WebPを指定した順序で1つのPDFへ変換します。",
            self,
        )
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        list_row = QHBoxLayout()
        self.input_list = ImageInputListWidget(self)
        self.input_list.setObjectName("imageToPdfInputList")
        self.input_list.model().rowsMoved.connect(lambda *_args: self.update_preview())
        list_row.addWidget(self.input_list, 1)

        button_column = QVBoxLayout()
        self.add_button = QPushButton("画像を追加…", self)
        self.add_button.setObjectName("imageToPdfAddButton")
        self.remove_button = QPushButton("削除", self)
        self.remove_button.setObjectName("imageToPdfRemoveButton")
        self.up_button = QPushButton("上へ", self)
        self.up_button.setObjectName("imageToPdfMoveUpButton")
        self.down_button = QPushButton("下へ", self)
        self.down_button.setObjectName("imageToPdfMoveDownButton")
        self.add_button.clicked.connect(self.choose_inputs)
        self.remove_button.clicked.connect(self.remove_selected_inputs)
        self.up_button.clicked.connect(self.move_selected_input_up)
        self.down_button.clicked.connect(self.move_selected_input_down)
        for button in (self.add_button, self.remove_button, self.up_button, self.down_button):
            button_column.addWidget(button)
        button_column.addStretch(1)
        list_row.addLayout(button_column)
        root.addLayout(list_row)

        output_row = QHBoxLayout()
        self.output_path_edit = QLineEdit(
            str((default_output_directory / "images.pdf").expanduser().resolve()),
            self,
        )
        self.output_path_edit.setObjectName("imageToPdfOutputPathEdit")
        self.output_path_edit.textChanged.connect(self.update_preview)
        output_button = QPushButton("出力先…", self)
        output_button.clicked.connect(self.choose_output_path)
        output_row.addWidget(QLabel("出力PDF", self))
        output_row.addWidget(self.output_path_edit, 1)
        output_row.addWidget(output_button)
        root.addLayout(output_row)

        self.overwrite_checkbox = QCheckBox("既存の出力PDFを上書きする", self)
        self.overwrite_checkbox.setObjectName("imageToPdfOverwriteCheckBox")
        self.overwrite_checkbox.toggled.connect(self.update_preview)
        root.addWidget(self.overwrite_checkbox)

        form = QFormLayout()
        self.page_size_combo = QComboBox(self)
        self.page_size_combo.setObjectName("imageToPdfPageSizeCombo")
        self.page_size_combo.addItem("画像サイズに合わせる", PdfPageSizeMode.FIT_IMAGE.value)
        self.page_size_combo.addItem("A4", PdfPageSizeMode.A4.value)
        self.page_size_combo.addItem("Letter", PdfPageSizeMode.LETTER.value)
        self.page_size_combo.addItem("カスタム", PdfPageSizeMode.CUSTOM.value)
        self.page_size_combo.currentIndexChanged.connect(self.update_preview)
        form.addRow("ページサイズ", self.page_size_combo)

        custom_size_row = QHBoxLayout()
        self.custom_width_mm = self._spin(10.0, 2000.0, 210.0)
        self.custom_height_mm = self._spin(10.0, 2000.0, 297.0)
        custom_size_row.addWidget(QLabel("幅", self))
        custom_size_row.addWidget(self.custom_width_mm)
        custom_size_row.addWidget(QLabel("mm  高さ", self))
        custom_size_row.addWidget(self.custom_height_mm)
        custom_size_row.addWidget(QLabel("mm", self))
        custom_host = QWidget(self)
        custom_host.setLayout(custom_size_row)
        form.addRow("カスタムサイズ", custom_host)

        self.orientation_combo = QComboBox(self)
        self.orientation_combo.setObjectName("imageToPdfOrientationCombo")
        self.orientation_combo.addItem("自動", PdfOrientation.AUTO.value)
        self.orientation_combo.addItem("縦", PdfOrientation.PORTRAIT.value)
        self.orientation_combo.addItem("横", PdfOrientation.LANDSCAPE.value)
        self.orientation_combo.currentIndexChanged.connect(self.update_preview)
        form.addRow("向き", self.orientation_combo)

        margin_row = QHBoxLayout()
        self.margin_top_mm = self._spin(0.0, 200.0, 10.0)
        self.margin_right_mm = self._spin(0.0, 200.0, 10.0)
        self.margin_bottom_mm = self._spin(0.0, 200.0, 10.0)
        self.margin_left_mm = self._spin(0.0, 200.0, 10.0)
        for label, spin in (
            ("上", self.margin_top_mm),
            ("右", self.margin_right_mm),
            ("下", self.margin_bottom_mm),
            ("左", self.margin_left_mm),
        ):
            margin_row.addWidget(QLabel(label, self))
            margin_row.addWidget(spin)
        margin_row.addWidget(QLabel("mm", self))
        margin_host = QWidget(self)
        margin_host.setLayout(margin_row)
        form.addRow("余白", margin_host)

        self.scaling_combo = QComboBox(self)
        self.scaling_combo.setObjectName("imageToPdfScalingCombo")
        self.scaling_combo.addItem("全体を収める", ImageScalingMode.FIT.value)
        self.scaling_combo.addItem("塗りつぶし", ImageScalingMode.FILL.value)
        self.scaling_combo.addItem("実寸", ImageScalingMode.ACTUAL_SIZE.value)
        self.scaling_combo.currentIndexChanged.connect(self.update_preview)
        form.addRow("拡大縮小", self.scaling_combo)

        self.transparency_combo = QComboBox(self)
        self.transparency_combo.setObjectName("imageToPdfTransparencyCombo")
        self.transparency_combo.addItem("白背景に合成", TransparencyPolicy.WHITE_BACKGROUND.value)
        self.transparency_combo.addItem("黒背景に合成", TransparencyPolicy.BLACK_BACKGROUND.value)
        self.transparency_combo.addItem("透明を保持", TransparencyPolicy.PRESERVE_ALPHA.value)
        self.transparency_combo.currentIndexChanged.connect(self.update_preview)
        form.addRow("透明", self.transparency_combo)
        root.addLayout(form)

        self.feedback_label = QLabel("", self)
        self.feedback_label.setObjectName("imageToPdfValidationFeedback")
        self.feedback_label.setWordWrap(True)
        root.addWidget(self.feedback_label)

        self.preview_label = QLabel("", self)
        self.preview_label.setObjectName("imageToPdfPreviewLabel")
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.preview_label)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        self.button_box.accepted.connect(self.accept_with_validation)
        self.button_box.rejected.connect(self.reject)
        root.addWidget(self.button_box)

        self._connect_spins()
        self.update_preview()

    @property
    def dialog_result(self) -> ImageToPdfDialogResult | None:
        return self._dialog_result

    def add_inputs(self, paths: tuple[Path, ...]) -> None:
        rejected: list[str] = []
        existing_paths = {item.image_input.path for item in self._current_inputs()}
        for path in paths:
            try:
                inspected_input = self._input_reader(path)
            except Exception as exc:
                rejected.append(f"{path.name}: {exc}")
                continue
            image_input = inspected_input.image_input
            if image_input.path in existing_paths:
                rejected.append(f"{image_input.label}: 既に追加されています")
                continue
            existing_paths.add(image_input.path)
            item = QListWidgetItem(self._display_text(inspected_input), self.input_list)
            item.setData(Qt.ItemDataRole.UserRole, inspected_input)
        self.update_preview()
        if rejected:
            QMessageBox.warning(self, "追加できない画像", "\n".join(rejected))

    def choose_inputs(self) -> None:
        filenames, _ = QFileDialog.getOpenFileNames(
            self,
            "PDFに変換する画像を追加",
            "",
            "Images (*.jpg *.jpeg *.png *.tif *.tiff *.bmp *.webp)",
        )
        if filenames:
            self.add_inputs(tuple(Path(filename) for filename in filenames))

    def choose_output_path(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "画像PDFの出力先",
            self.output_path_edit.text(),
            "PDF files (*.pdf)",
        )
        if filename:
            self.output_path_edit.setText(str(self._normalize_output_path(Path(filename))))

    def remove_selected_inputs(self) -> None:
        for item in self.input_list.selectedItems():
            self.input_list.takeItem(self.input_list.row(item))
        self.update_preview()

    def move_selected_input_up(self) -> None:
        self._move_selected_input(-1)

    def move_selected_input_down(self) -> None:
        self._move_selected_input(1)

    def update_preview(self) -> None:
        try:
            plan = self._build_plan()
        except (OSError, TypeError, ValueError) as exc:
            self.feedback_label.setText(str(exc))
            self.preview_label.setText("")
            self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return
        self.feedback_label.setText(
            f"{len(plan.inputs)}個の画像から{plan.total_page_count}ページのPDFを作成します。"
        )
        lines: list[str] = []
        cursor = 1
        for index, item in enumerate(plan.inputs, start=1):
            start = cursor
            end = cursor + item.frame_count - 1
            display_range = f"{start}" if start == end else f"{start}-{end}"
            lines.append(f"{index}. {item.label} — {item.frame_count}ページ — 出力 {display_range}")
            cursor = end + 1
        self.preview_label.setText("\n".join(lines))
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

    def accept_with_validation(self) -> None:
        try:
            plan = self._build_plan()
            inspected_inputs = self._current_inputs()
            revisions = self._validated_current_revisions(inspected_inputs, plan)
            if self._is_managed_path is not None and self._is_managed_path(plan.output_path):
                raise ValueError("アプリの一時作業フォルダ内には出力できません")
            target_snapshot = self._target_snapshot_reader(plan.output_path)
            if target_snapshot.exists and not self.overwrite_checkbox.isChecked():
                raise ValueError("出力先PDFが既に存在します")
        except (OSError, TypeError, ValueError, ImageSourceChangedError) as exc:
            self.feedback_label.setText(str(exc))
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self._dialog_result = ImageToPdfDialogResult(
            plan=plan,
            overwrite=self.overwrite_checkbox.isChecked(),
            expected_source_revisions=revisions,
            expected_target_snapshot=target_snapshot,
        )
        self.accept()

    def _move_selected_input(self, offset: int) -> None:
        row = self.input_list.currentRow()
        target_row = row + offset
        if row < 0 or not 0 <= target_row < self.input_list.count():
            return
        item = self.input_list.takeItem(row)
        self.input_list.insertItem(target_row, item)
        self.input_list.setCurrentRow(target_row)
        self.update_preview()

    def _current_inputs(self) -> tuple[InspectedImageInput, ...]:
        inputs: list[InspectedImageInput] = []
        for row in range(self.input_list.count()):
            item = self.input_list.item(row)
            inspected_input = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(inspected_input, InspectedImageInput):
                raise ValueError("画像入力の状態が不正です")
            inputs.append(inspected_input)
        return tuple(inputs)

    def _build_plan(self) -> ImageToPdfPlan:
        inspected_inputs = self._current_inputs()
        inputs = tuple(item.image_input for item in inspected_inputs)
        output_path = self._normalize_output_path(Path(self.output_path_edit.text()))
        page_size_mode = PdfPageSizeMode(str(self.page_size_combo.currentData()))
        custom_width = custom_height = None
        if page_size_mode is PdfPageSizeMode.CUSTOM:
            custom_width = mm_to_points(self.custom_width_mm.value())
            custom_height = mm_to_points(self.custom_height_mm.value())
        return build_image_to_pdf_plan(
            inputs,
            output_path,
            page_size_mode=page_size_mode,
            orientation=PdfOrientation(str(self.orientation_combo.currentData())),
            scaling_mode=ImageScalingMode(str(self.scaling_combo.currentData())),
            transparency_policy=TransparencyPolicy(str(self.transparency_combo.currentData())),
            margins=margins_from_mm(
                self.margin_top_mm.value(),
                self.margin_right_mm.value(),
                self.margin_bottom_mm.value(),
                self.margin_left_mm.value(),
            ),
            custom_page_width_points=custom_width,
            custom_page_height_points=custom_height,
        )

    def _validated_current_revisions(
        self,
        inspected_inputs: tuple[InspectedImageInput, ...],
        plan: ImageToPdfPlan,
    ) -> dict[Path, ImageSourceRevision]:
        refreshed: dict[Path, ImageSourceRevision] = {}
        inspected_by_path = {item.image_input.path: item for item in inspected_inputs}
        if set(inspected_by_path) != {item.path for item in plan.inputs}:
            raise ValueError("画像入力の状態が不正です")
        for input_item in plan.inputs:
            original = inspected_by_path[input_item.path]
            refreshed_input = self._input_reader(input_item.path)
            if refreshed_input.image_input != original.image_input:
                raise ImageSourceChangedError(
                    f"{input_item.label} の画像情報が変更されたため作成を開始できません"
                )
            if refreshed_input.source_revision != original.source_revision:
                raise ImageSourceChangedError(
                    f"{input_item.label} が変更されたため作成を開始できません"
                )
            refreshed[input_item.path] = original.source_revision
        return refreshed

    @staticmethod
    def _display_text(inspected_input: InspectedImageInput) -> str:
        item = inspected_input.image_input
        return (
            f"{item.label} - {item.frame_count}ページ - {item.detected_format} - "
            f"{item.pixel_width} x {item.pixel_height} - {item.color_mode} - {item.path}"
        )

    @staticmethod
    def _normalize_output_path(path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if resolved.suffix:
            return resolved
        return resolved.with_suffix(".pdf")

    @staticmethod
    def _spin(minimum: float, maximum: float, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        spin.setSingleStep(1.0)
        return spin

    def _connect_spins(self) -> None:
        for spin in (
            self.custom_width_mm,
            self.custom_height_mm,
            self.margin_top_mm,
            self.margin_right_mm,
            self.margin_bottom_mm,
            self.margin_left_mm,
        ):
            spin.valueChanged.connect(self.update_preview)
