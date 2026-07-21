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
    QFileDialog,
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

from pdf_workbench.domain.pdf_merge import (
    PdfMergeBookmarkPolicy,
    PdfMergeMetadataPolicy,
    PdfMergePlan,
    build_pdf_merge_plan,
)
from pdf_workbench.services.pdf_merge import InspectedPdfMergeInput, SourcePdfChangedError
from pdf_workbench.services.pdf_page_mutation import SourcePdfRevision
from pdf_workbench.services.pdf_save_service import TargetSnapshot


@dataclass(frozen=True, slots=True)
class MergePdfsDialogResult:
    plan: PdfMergePlan
    overwrite: bool
    expected_source_revisions: dict[Path, SourcePdfRevision]
    expected_target_snapshot: TargetSnapshot


class MergeInputListWidget(QListWidget):
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
        if accepted and isinstance(parent, MergePdfsDialog):
            parent.update_preview()
        return accepted


class MergePdfsDialog(QDialog):
    def __init__(
        self,
        *,
        input_reader: Callable[[Path], InspectedPdfMergeInput],
        target_snapshot_reader: Callable[[Path], TargetSnapshot] = TargetSnapshot.capture,
        is_managed_path: Callable[[Path], bool] | None = None,
        default_output_directory: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("PDFを結合")
        self.setModal(True)
        self._input_reader = input_reader
        self._target_snapshot_reader = target_snapshot_reader
        self._is_managed_path = is_managed_path
        self._dialog_result: MergePdfsDialogResult | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self.summary_label = QLabel("2個以上のPDFを指定した順序で1つのPDFへ結合します。", self)
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        list_row = QHBoxLayout()
        self.input_list = MergeInputListWidget(self)
        self.input_list.setObjectName("mergeInputList")
        self.input_list.model().rowsMoved.connect(lambda *_args: self.update_preview())
        list_row.addWidget(self.input_list, 1)

        button_column = QVBoxLayout()
        self.add_button = QPushButton("追加…", self)
        self.add_button.setObjectName("mergeAddButton")
        self.remove_button = QPushButton("削除", self)
        self.remove_button.setObjectName("mergeRemoveButton")
        self.up_button = QPushButton("上へ", self)
        self.up_button.setObjectName("mergeMoveUpButton")
        self.down_button = QPushButton("下へ", self)
        self.down_button.setObjectName("mergeMoveDownButton")
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
            str((default_output_directory / "merged.pdf").expanduser().resolve()),
            self,
        )
        self.output_path_edit.setObjectName("mergeOutputPathEdit")
        self.output_path_edit.textChanged.connect(self.update_preview)
        self.output_button = QPushButton("出力先…", self)
        self.output_button.clicked.connect(self.choose_output_path)
        output_row.addWidget(QLabel("出力先", self))
        output_row.addWidget(self.output_path_edit, 1)
        output_row.addWidget(self.output_button)
        root.addLayout(output_row)

        self.overwrite_checkbox = QCheckBox("既存の出力PDFを上書きする", self)
        self.overwrite_checkbox.setObjectName("mergeOverwriteCheckBox")
        self.overwrite_checkbox.toggled.connect(self.update_preview)
        root.addWidget(self.overwrite_checkbox)

        metadata_row = QHBoxLayout()
        self.metadata_combo = QComboBox(self)
        self.metadata_combo.setObjectName("mergeMetadataCombo")
        self.metadata_combo.currentIndexChanged.connect(self.update_preview)
        metadata_row.addWidget(QLabel("Metadata", self))
        metadata_row.addWidget(self.metadata_combo, 1)
        root.addLayout(metadata_row)

        bookmark_row = QHBoxLayout()
        self.bookmark_combo = QComboBox(self)
        self.bookmark_combo.setObjectName("mergeBookmarkCombo")
        self.bookmark_combo.addItem("含めない", PdfMergeBookmarkPolicy.NONE.value)
        self.bookmark_combo.addItem(
            "入力PDFごとに保持",
            PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE.value,
        )
        self.bookmark_combo.currentIndexChanged.connect(self.update_preview)
        bookmark_row.addWidget(QLabel("Bookmarks", self))
        bookmark_row.addWidget(self.bookmark_combo, 1)
        root.addLayout(bookmark_row)

        self.feedback_label = QLabel("", self)
        self.feedback_label.setObjectName("mergeValidationFeedback")
        self.feedback_label.setWordWrap(True)
        root.addWidget(self.feedback_label)

        self.preview_label = QLabel("", self)
        self.preview_label.setObjectName("mergePreviewLabel")
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
        self.update_preview()

    @property
    def dialog_result(self) -> MergePdfsDialogResult | None:
        return self._dialog_result

    def add_inputs(self, paths: tuple[Path, ...]) -> None:
        rejected: list[str] = []
        existing_paths = {item.merge_input.path for item in self._current_inputs()}
        for path in paths:
            try:
                inspected_input = self._input_reader(path)
            except Exception as exc:
                rejected.append(f"{path.name}: {exc}")
                continue
            merge_input = inspected_input.merge_input
            if merge_input.page_count != inspected_input.source_revision.page_count:
                rejected.append(f"{merge_input.label}: revisionとページ数が一致しません")
                continue
            if merge_input.path in existing_paths:
                rejected.append(f"{merge_input.label}: 既に追加されています")
                continue
            existing_paths.add(merge_input.path)
            item = QListWidgetItem(
                f"{merge_input.label} — {merge_input.page_count}ページ",
                self.input_list,
            )
            item.setData(Qt.ItemDataRole.UserRole, inspected_input)
        self.update_preview()
        if rejected:
            QMessageBox.warning(self, "追加できないPDF", "\n".join(rejected))

    def choose_inputs(self) -> None:
        filenames, _ = QFileDialog.getOpenFileNames(
            self,
            "結合するPDFを追加",
            "",
            "PDF files (*.pdf)",
        )
        if filenames:
            self.add_inputs(tuple(Path(filename) for filename in filenames))

    def choose_output_path(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "結合PDFの出力先",
            self.output_path_edit.text(),
            "PDF files (*.pdf)",
        )
        if filename:
            self.output_path_edit.setText(str(self._normalize_output_path(Path(filename))))

    def remove_selected_inputs(self) -> None:
        selected_metadata_source = self._metadata_policy()[1]
        removed_metadata_source = False
        for item in self.input_list.selectedItems():
            inspected_input = item.data(Qt.ItemDataRole.UserRole)
            if (
                isinstance(inspected_input, InspectedPdfMergeInput)
                and selected_metadata_source == inspected_input.merge_input.path
            ):
                removed_metadata_source = True
            row = self.input_list.row(item)
            self.input_list.takeItem(row)
        self.update_preview()
        if removed_metadata_source:
            self.metadata_combo.setCurrentIndex(0)
            self.feedback_label.setText(
                "Metadata取得元が削除されたため、引き継がない設定へ戻しました"
            )

    def move_selected_input_up(self) -> None:
        self._move_selected_input(-1)

    def move_selected_input_down(self) -> None:
        self._move_selected_input(1)

    def update_preview(self) -> None:
        self._refresh_metadata_options()
        try:
            plan = self._build_plan()
        except (OSError, TypeError, ValueError) as exc:
            self.feedback_label.setText(str(exc))
            self.preview_label.setText("")
            self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return
        self.feedback_label.setText(
            f"{len(plan.inputs)}個のPDF、合計{plan.total_page_count}ページを結合します。"
        )
        lines = [
            f"{index}. {item.label} — {item.page_count}ページ — 出力 {output_range.display_range}"
            for index, (item, output_range) in enumerate(
                zip(plan.inputs, plan.output_ranges, strict=True),
                start=1,
            )
        ]
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
        except (OSError, TypeError, ValueError, SourcePdfChangedError) as exc:
            self.feedback_label.setText(str(exc))
            QMessageBox.warning(self, "入力エラー", str(exc))
            return
        self._dialog_result = MergePdfsDialogResult(
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

    def _current_inputs(self) -> tuple[InspectedPdfMergeInput, ...]:
        inputs: list[InspectedPdfMergeInput] = []
        for row in range(self.input_list.count()):
            item = self.input_list.item(row)
            inspected_input = item.data(Qt.ItemDataRole.UserRole)
            if not isinstance(inspected_input, InspectedPdfMergeInput):
                raise ValueError("結合入力の状態が不正です")
            inputs.append(inspected_input)
        return tuple(inputs)

    def _build_plan(self) -> PdfMergePlan:
        inspected_inputs = self._current_inputs()
        inputs = tuple(item.merge_input for item in inspected_inputs)
        output_path = self._normalize_output_path(Path(self.output_path_edit.text()))
        metadata_policy, metadata_source_path = self._metadata_policy()
        bookmark_policy = PdfMergeBookmarkPolicy(str(self.bookmark_combo.currentData()))
        return build_pdf_merge_plan(
            inputs,
            output_path,
            metadata_policy=metadata_policy,
            metadata_source_path=metadata_source_path,
            bookmark_policy=bookmark_policy,
        )

    def _metadata_policy(self) -> tuple[PdfMergeMetadataPolicy, Path | None]:
        value = self.metadata_combo.currentData()
        if value is None or value == PdfMergeMetadataPolicy.NONE.value:
            return PdfMergeMetadataPolicy.NONE, None
        return PdfMergeMetadataPolicy.SELECTED_SOURCE, Path(str(value)).expanduser().resolve()

    def _refresh_metadata_options(self) -> None:
        current_data = self.metadata_combo.currentData()
        self.metadata_combo.blockSignals(True)
        self.metadata_combo.clear()
        self.metadata_combo.addItem("引き継がない", PdfMergeMetadataPolicy.NONE.value)
        for item in self._current_inputs():
            self.metadata_combo.addItem(
                f"{item.merge_input.label} から引き継ぐ",
                str(item.merge_input.path),
            )
        if current_data is not None:
            index = self.metadata_combo.findData(current_data)
            if index >= 0:
                self.metadata_combo.setCurrentIndex(index)
        self.metadata_combo.blockSignals(False)

    def _validated_current_revisions(
        self,
        inspected_inputs: tuple[InspectedPdfMergeInput, ...],
        plan: PdfMergePlan,
    ) -> dict[Path, SourcePdfRevision]:
        refreshed: dict[Path, SourcePdfRevision] = {}
        inspected_by_path = {item.merge_input.path: item for item in inspected_inputs}
        if set(inspected_by_path) != {item.path for item in plan.inputs}:
            raise ValueError("結合入力の状態が不正です")
        for input_item in plan.inputs:
            original = inspected_by_path[input_item.path]
            refreshed_input = self._input_reader(input_item.path)
            if refreshed_input.merge_input.path != input_item.path:
                raise SourcePdfChangedError(f"{input_item.label} の場所が変更されました")
            if refreshed_input.merge_input.page_count != input_item.page_count:
                raise SourcePdfChangedError(f"{input_item.label} のページ数が変更されました")
            if refreshed_input.source_revision != original.source_revision:
                raise SourcePdfChangedError(
                    f"{input_item.label} が変更されたため結合を開始できません"
                )
            refreshed[input_item.path] = original.source_revision
        metadata_policy, metadata_source = self._metadata_policy()
        if (
            metadata_policy is PdfMergeMetadataPolicy.SELECTED_SOURCE
            and metadata_source not in refreshed
        ):
            raise ValueError("Metadata取得元が結合入力に含まれていません")
        return refreshed

    @staticmethod
    def _normalize_output_path(path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if resolved.suffix:
            return resolved
        return resolved.with_suffix(".pdf")
