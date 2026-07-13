from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pdf_workbench.domain.document_session import DocumentSession, FileFingerprint, SourceStatus
from pdf_workbench.services.source_change_monitor import SourceCheckResult


def create_session(tmp_path: Path) -> DocumentSession:
    source_path = tmp_path / "sample.pdf"
    source_path.write_bytes(b"%PDF-1.7\n% test\n")
    workspace_directory = tmp_path / "workspace"
    workspace_directory.mkdir()
    working_copy_path = workspace_directory / "working.pdf"
    working_copy_path.write_bytes(source_path.read_bytes())
    return DocumentSession(
        source_path=source_path,
        working_copy_path=working_copy_path,
        workspace_directory=workspace_directory,
        source_fingerprint=FileFingerprint.from_path(source_path),
    )


def test_document_session_normalizes_pdf_path(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    assert session.source_path == (tmp_path / "sample.pdf").resolve()
    assert session.working_copy_path == (tmp_path / "workspace" / "working.pdf").resolve()
    assert session.workspace_directory == (tmp_path / "workspace").resolve()
    assert session.document_path == session.working_copy_path
    assert session.display_path == session.source_path
    assert session.is_modified is False


def test_document_session_rejects_non_pdf(tmp_path: Path) -> None:
    workspace_directory = tmp_path / "workspace"
    workspace_directory.mkdir()
    with pytest.raises(ValueError, match="PDF"):
        DocumentSession(
            source_path=tmp_path / "sample.txt",
            working_copy_path=workspace_directory / "working.pdf",
            workspace_directory=workspace_directory,
            source_fingerprint=FileFingerprint(size_bytes=0, modified_time_ns=0),
        )


def test_mark_modified_records_operation(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    session.mark_modified("rotate page 1")
    assert session.is_modified is True
    assert session.operation_history == ["rotate page 1"]


def test_mark_saved_updates_source_path_and_fingerprint(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    session.mark_modified("delete page 2")
    target_path = tmp_path / "saved-as.pdf"
    target_path.write_bytes(b"%PDF-1.7\n% saved\n")
    saved_at = datetime.now(UTC)
    fingerprint = FileFingerprint.from_path(target_path)

    session.mark_saved(target_path, fingerprint, saved_at)

    assert session.source_path == target_path.resolve()
    assert session.display_path == target_path.resolve()
    assert session.source_fingerprint == fingerprint
    assert session.last_saved_at == saved_at
    assert session.is_modified is False


def test_zoom_and_current_page_changes_do_not_mark_modified(tmp_path: Path) -> None:
    session = create_session(tmp_path)

    session.set_navigation_state(page_index=3, zoom_factor=1.5)

    assert session.current_page_index == 3
    assert session.zoom_factor == pytest.approx(1.5)
    assert session.is_modified is False


def test_recovery_flags_follow_source_status_and_clear_after_save(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    session.mark_recovered(SourceStatus.MODIFIED)

    assert session.recovered_from_interrupted_session is True
    assert session.requires_save_as is True
    assert session.recovery_source_status is SourceStatus.MODIFIED

    saved_path = tmp_path / "saved.pdf"
    saved_path.write_bytes(b"%PDF-1.7\n% saved\n")
    session.mark_saved(
        saved_path,
        FileFingerprint.from_path(saved_path),
        datetime.now(UTC),
    )

    assert session.recovered_from_interrupted_session is False
    assert session.requires_save_as is False
    assert session.recovery_source_status is None


def test_mark_modified_limits_operation_history(tmp_path: Path) -> None:
    session = create_session(tmp_path)

    for index in range(150):
        session.mark_modified(f"op-{index}")

    assert len(session.operation_history) == DocumentSession.MAX_OPERATION_HISTORY
    assert session.operation_history[0] == "op-50"
    assert session.operation_history[-1] == "op-149"


def test_apply_source_check_updates_requires_save_as_without_marking_dirty(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    checked_at = datetime.now(UTC)

    changed = session.apply_source_check(
        SourceCheckResult(
            path=session.source_path,
            status=SourceStatus.MODIFIED,
            expected_fingerprint=session.source_fingerprint,
            current_fingerprint=FileFingerprint(size_bytes=999, modified_time_ns=999),
            checked_at=checked_at,
            error_message="modified",
        )
    )

    assert changed is True
    assert session.source_status is SourceStatus.MODIFIED
    assert session.requires_save_as is True
    assert session.is_modified is False
    assert session.source_change_detected_at == checked_at


def test_apply_source_check_returns_to_unchanged_after_non_recovery_transition(
    tmp_path: Path,
) -> None:
    session = create_session(tmp_path)
    session.apply_source_check(
        SourceCheckResult(
            path=session.source_path,
            status=SourceStatus.MODIFIED,
            expected_fingerprint=session.source_fingerprint,
            current_fingerprint=FileFingerprint(size_bytes=999, modified_time_ns=999),
            checked_at=datetime.now(UTC),
            error_message="modified",
        )
    )

    session.apply_source_check(
        SourceCheckResult(
            path=session.source_path,
            status=SourceStatus.UNCHANGED,
            expected_fingerprint=session.source_fingerprint,
            current_fingerprint=session.source_fingerprint,
            checked_at=datetime.now(UTC),
            error_message=None,
        )
    )

    assert session.source_status is SourceStatus.UNCHANGED
    assert session.requires_save_as is False
    assert session.source_change_detected_at is None
