from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
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
        source_status=SourceStatus.UNREADABLE,
    )
    dialog = RecoveryDialog([valid_candidate, invalid_candidate])
    qtbot.addWidget(dialog)
    dialog.show()

    first_item = dialog._tree.topLevelItem(0)
    second_item = dialog._tree.topLevelItem(1)
    first_item.setCheckState(0, Qt.CheckState.Checked)
    assert not second_item.flags() & Qt.ItemFlag.ItemIsUserCheckable

    QTest.mouseClick(dialog._recover_button, Qt.MouseButton.LeftButton)

    assert dialog.result_value.action is RecoveryDialogAction.RECOVER
    assert dialog.result_value.candidates == [valid_candidate]
