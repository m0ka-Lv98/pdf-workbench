from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdf_workbench.services.session_recovery import RecoveryCandidate


class RecoveryDialogAction(StrEnum):
    RECOVER = "recover"
    DISCARD = "discard"
    LATER = "later"


@dataclass(frozen=True, slots=True)
class RecoveryDialogResult:
    action: RecoveryDialogAction
    candidates: list[RecoveryCandidate]


class RecoveryDialog(QDialog):
    @staticmethod
    def compute_dialog_size(available_width: int, available_height: int) -> tuple[int, int]:
        width = min(920, max(640, available_width - 80))
        height = min(520, max(420, available_height - 80))
        return width, height

    def __init__(
        self,
        candidates: list[RecoveryCandidate],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("recoveryDialog")
        self.setWindowTitle("中断されたセッションを復旧")
        self._candidates = candidates
        self._result = RecoveryDialogResult(RecoveryDialogAction.LATER, [])
        self._button_row_widget: QWidget | None = None

        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width, height = self.compute_dialog_size(
                available.width(),
                available.height(),
            )
            self.resize(width, height)
        else:
            width, height = self.compute_dialog_size(800, 600)
            self.resize(width, height)
        self.setMinimumSize(640, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("前回正常終了しなかった作業セッションが見つかりました。", self)
        title.setObjectName("recoveryDialogTitle")
        title.setWordWrap(True)
        description = QLabel(
            "復元する候補を選ぶか、不要な候補を破棄してください。「後で」を選ぶと次回起動時に再表示されます。",
            self,
        )
        description.setWordWrap(True)
        description.setObjectName("recoveryDialogDescription")

        self._tree = QTreeWidget(self)
        self._tree.setObjectName("recoveryCandidateTree")
        self._tree.setRootIsDecorated(False)
        self._tree.setAlternatingRowColors(False)
        self._tree.setUniformRowHeights(False)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        self._tree.setColumnCount(6)
        self._tree.setHeaderLabels(["対象", "ファイル", "元の場所", "最終更新", "状態", "サイズ"])
        self._tree.header().setStretchLastSection(False)
        self._tree.header().resizeSection(0, 72)
        self._tree.header().resizeSection(1, 180)
        self._tree.header().resizeSection(2, 280)
        self._tree.header().resizeSection(3, 150)
        self._tree.header().resizeSection(4, 190)

        for index, candidate in enumerate(candidates):
            item = QTreeWidgetItem(self._tree)
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            selectable = candidate.recoverable or candidate.discardable
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEnabled)
            if selectable:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
            item.setText(1, candidate.metadata.source_path.name)
            source_path_text = str(candidate.metadata.source_path)
            item.setText(2, source_path_text)
            item.setToolTip(2, source_path_text)
            item.setText(3, self._format_datetime(candidate.metadata.updated_at))
            item.setText(4, self._status_text(candidate))
            item.setToolTip(4, candidate.error_message or self._status_text(candidate))
            item.setText(5, self._format_size(candidate.working_copy_size_bytes))
            if not selectable:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                item.setText(0, "不可")
            elif candidate.recoverable:
                item.setText(0, "選択")
            else:
                item.setText(0, "破棄")

        button_row = QHBoxLayout()
        button_row_widget = QWidget(self)
        button_row_widget.setObjectName("recoveryDialogButtons")
        button_row_widget.setLayout(button_row)
        self._button_row_widget = button_row_widget
        button_row.addStretch(1)
        self._recover_button = QPushButton("選択項目を復元", self)
        self._discard_button = QPushButton("選択項目を破棄", self)
        self._later_button = QPushButton("後で", self)
        self._recover_button.clicked.connect(self._recover_selected)
        self._discard_button.clicked.connect(self._discard_selected)
        self._later_button.clicked.connect(self._later)
        button_row.addWidget(self._recover_button)
        button_row.addWidget(self._discard_button)
        button_row.addWidget(self._later_button)
        self._tree.itemChanged.connect(self._update_button_state)

        layout.addWidget(title)
        layout.addWidget(description)
        layout.addWidget(self._tree, 1)
        layout.addWidget(button_row_widget)
        self._update_button_state()

    @property
    def result_value(self) -> RecoveryDialogResult:
        return self._result

    def reject(self) -> None:
        self._later()

    def _selected_candidates(self) -> list[RecoveryCandidate]:
        selected: list[RecoveryCandidate] = []
        for index in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(index)
            if item is None:
                continue
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            candidate_index = item.data(0, Qt.ItemDataRole.UserRole)
            if not isinstance(candidate_index, int):
                continue
            selected.append(self._candidates[candidate_index])
        return selected

    def _update_button_state(self) -> None:
        selected = self._selected_candidates()
        self._discard_button.setEnabled(any(candidate.discardable for candidate in selected))
        self._recover_button.setEnabled(any(candidate.recoverable for candidate in selected))

    def _recover_selected(self) -> None:
        selected = [candidate for candidate in self._selected_candidates() if candidate.recoverable]
        if not selected:
            return
        self._result = RecoveryDialogResult(RecoveryDialogAction.RECOVER, selected)
        self.accept()

    def _discard_selected(self) -> None:
        selected = [candidate for candidate in self._selected_candidates() if candidate.discardable]
        if not selected:
            return
        result = QMessageBox.question(
            self,
            "復旧候補を破棄",
            "選択した復旧候補を完全に削除しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            return
        self._result = RecoveryDialogResult(RecoveryDialogAction.DISCARD, selected)
        self.accept()

    def _later(self) -> None:
        self._result = RecoveryDialogResult(RecoveryDialogAction.LATER, [])
        self.done(QDialog.DialogCode.Rejected)

    @staticmethod
    def _status_text(candidate: RecoveryCandidate) -> str:
        if not candidate.recoverable:
            if candidate.discardable:
                return candidate.error_message or "復元不可・破棄可能"
            return candidate.error_message or "安全のため自動削除できません"
        status_parts: list[str] = []
        if candidate.metadata.is_modified:
            status_parts.append("未保存の変更あり")
        if candidate.source_status == "missing":
            status_parts.append("元ファイルが見つかりません")
        elif candidate.source_status == "modified":
            status_parts.append("元ファイルが変更されています")
        elif candidate.source_status == "unreadable":
            status_parts.append("元ファイルを確認できません")
        return " / ".join(status_parts) or "復元可能"

    @staticmethod
    def _format_datetime(value: object) -> str:
        from datetime import datetime

        if not isinstance(value, datetime):
            return "-"
        return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KiB"
        return f"{size_bytes / (1024 * 1024):.1f} MiB"
