from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from pdf_workbench.domain.document_session import FileFingerprint, SourceStatus
from pdf_workbench.services.session_recovery import (
    RecoveryCandidate,
    RecoveryMetadata,
    RecoveryValidationStatus,
)
from pdf_workbench.ui.dialogs.recovery_dialog import RecoveryDialog, RecoveryDialogAction


def make_candidate(
    tmp_path: Path,
    *,
    name: str,
    recoverable: bool,
    discardable: bool | None = None,
    source_status: SourceStatus = SourceStatus.UNCHANGED,
) -> RecoveryCandidate:
    workspace_directory = tmp_path / name
    workspace_directory.mkdir()
    metadata = RecoveryMetadata(
        schema_version=1,
        session_id=name,
        source_path=(tmp_path / f"{name}.pdf").resolve(),
        working_copy_name="working.pdf",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        last_saved_at=None,
        source_fingerprint=FileFingerprint(size_bytes=1, modified_time_ns=1),
        current_page_index=0,
        zoom_factor=1.0,
        is_modified=True,
        operation_history=["edit"],
    )
    return RecoveryCandidate(
        workspace_directory=workspace_directory,
        working_copy_path=workspace_directory / "working.pdf",
        metadata=metadata,
        source_status=source_status,
        validation_status=(
            RecoveryValidationStatus.VALID if recoverable else RecoveryValidationStatus.INVALID
        ),
        recoverable=recoverable,
        discardable=recoverable if discardable is None else discardable,
        error_message=None if recoverable else "metadataが破損しています",
        working_copy_size_bytes=1024,
    )


def test_recovery_dialog_reject_maps_to_later(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = RecoveryDialog([make_candidate(tmp_path, name="a" * 32, recoverable=True)])
    qtbot.addWidget(dialog)
    dialog.show()

    dialog.reject()

    assert dialog.result_value.action is RecoveryDialogAction.LATER


def test_recovery_dialog_recovers_only_checked_recoverable_candidates(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    valid_candidate = make_candidate(tmp_path, name="a" * 32, recoverable=True)
    invalid_candidate = make_candidate(
        tmp_path,
        name="b" * 32,
        recoverable=False,
        discardable=True,
        source_status=SourceStatus.UNREADABLE,
    )
    dialog = RecoveryDialog([valid_candidate, invalid_candidate])
    qtbot.addWidget(dialog)
    dialog.show()

    first_item = dialog._tree.topLevelItem(0)
    second_item = dialog._tree.topLevelItem(1)
    first_item.setCheckState(0, Qt.CheckState.Checked)
    assert second_item.flags() & Qt.ItemFlag.ItemIsUserCheckable

    QTest.mouseClick(dialog._recover_button, Qt.MouseButton.LeftButton)

    assert dialog.result_value.action is RecoveryDialogAction.RECOVER
    assert dialog.result_value.candidates == [valid_candidate]


def test_recovery_dialog_allows_discard_for_invalid_discardable_candidate(
    qtbot: QtBot,
    monkeypatch,
    tmp_path: Path,
) -> None:
    candidate = make_candidate(
        tmp_path,
        name="c" * 32,
        recoverable=False,
        discardable=True,
    )
    dialog = RecoveryDialog([candidate])
    qtbot.addWidget(dialog)
    dialog.show()

    item = dialog._tree.topLevelItem(0)
    assert item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    item.setCheckState(0, Qt.CheckState.Checked)
    assert dialog._recover_button.isEnabled() is False
    assert dialog._discard_button.isEnabled() is True

    monkeypatch.setattr(
        "pdf_workbench.ui.dialogs.recovery_dialog.QMessageBox.question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    QTest.mouseClick(dialog._discard_button, Qt.MouseButton.LeftButton)

    assert dialog.result_value.action is RecoveryDialogAction.DISCARD
    assert dialog.result_value.candidates == [candidate]


def test_recovery_dialog_disables_unsafe_invalid_candidate(qtbot: QtBot, tmp_path: Path) -> None:
    candidate = make_candidate(
        tmp_path,
        name="d" * 32,
        recoverable=False,
        discardable=False,
    )
    dialog = RecoveryDialog([candidate])
    qtbot.addWidget(dialog)
    dialog.show()

    item = dialog._tree.topLevelItem(0)
    assert not item.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert dialog._recover_button.isEnabled() is False
    assert dialog._discard_button.isEnabled() is False


def test_recovery_dialog_fits_within_800x600(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = RecoveryDialog([make_candidate(tmp_path, name="e" * 32, recoverable=True)])
    qtbot.addWidget(dialog)
    dialog.resize(720, 520)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)

    assert dialog.width() <= 720
    assert dialog.height() <= 520
    assert dialog._tree.geometry().width() > 0
    assert dialog._tree.geometry().height() > 0
    assert dialog._button_row_widget is not None
    assert dialog._button_row_widget.geometry().width() > 0
    assert dialog._button_row_widget.geometry().height() > 0
    assert dialog._tree.isColumnHidden(RecoveryDialog.UPDATED_COLUMN) is True
    assert dialog._tree.isColumnHidden(RecoveryDialog.SIZE_COLUMN) is True
    assert dialog._tree.isColumnHidden(RecoveryDialog.STATE_COLUMN) is False
    assert dialog._tree.header().sectionSize(RecoveryDialog.STATE_COLUMN) >= 150


def test_recovery_dialog_shows_all_columns_in_regular_layout(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = RecoveryDialog([make_candidate(tmp_path, name="f" * 32, recoverable=True)])
    qtbot.addWidget(dialog)
    dialog.resize(920, 520)
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)

    assert dialog._tree.isColumnHidden(RecoveryDialog.UPDATED_COLUMN) is False
    assert dialog._tree.isColumnHidden(RecoveryDialog.SIZE_COLUMN) is False
    assert dialog._tree.isColumnHidden(RecoveryDialog.STATE_COLUMN) is False


def test_recovery_dialog_compute_dialog_size_is_screen_safe() -> None:
    assert RecoveryDialog.compute_dialog_size(1100, 800) == (920, 520)
    assert RecoveryDialog.compute_dialog_size(800, 600) == (720, 520)
    assert RecoveryDialog.compute_dialog_size(640, 480) == (640, 420)
