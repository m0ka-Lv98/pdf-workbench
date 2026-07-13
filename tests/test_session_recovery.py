from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pdf_test_utils import create_blank_pdf
from pdf_workbench.domain.document_session import SourceStatus
from pdf_workbench.services.pdf_document_validator import PdfDocumentValidator
from pdf_workbench.services.session_recovery import (
    RecoveryMetadataError,
    RecoveryValidationStatus,
    SessionRecoveryService,
)
from pdf_workbench.services.session_workspace import SessionWorkspaceManager


def create_session_environment(
    tmp_path: Path,
) -> tuple[SessionWorkspaceManager, SessionRecoveryService]:
    manager = SessionWorkspaceManager(tmp_path / "sessions")
    recovery = SessionRecoveryService(manager, validator=PdfDocumentValidator())
    return manager, recovery


def test_recovery_metadata_round_trip(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    session.mark_modified("rotate page 1")
    session.set_navigation_state(page_index=0, zoom_factor=1.5)

    recovery.write_metadata(session)
    metadata_path = session.workspace_directory / manager.METADATA_NAME
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["session_id"] == session.session_id
    assert payload["source_path"] == str(source_path.resolve())
    assert payload["working_copy_name"] == manager.WORKING_COPY_NAME
    assert payload["is_modified"] is True
    assert payload["zoom_factor"] == pytest.approx(1.5)
    assert payload["current_page_index"] == 0
    assert payload["created_at"].endswith("+00:00")
    assert payload["updated_at"].endswith("+00:00")
    assert payload["operation_history"] == ["rotate page 1"]


def test_recovery_metadata_write_failure_preserves_existing_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    metadata_path = session.workspace_directory / manager.METADATA_NAME
    original = metadata_path.read_text(encoding="utf-8")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    session.mark_modified("edit")
    with pytest.raises(RecoveryMetadataError):
        recovery.write_metadata(session)

    assert metadata_path.read_text(encoding="utf-8") == original


def test_scan_returns_valid_candidate_for_dirty_workspace(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    session.mark_modified("edit")
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)

    scan_result = recovery.scan_candidates()

    assert len(scan_result.candidates) == 1
    candidate = scan_result.candidates[0]
    assert candidate.recoverable is True
    assert candidate.discardable is True
    assert candidate.validation_status is RecoveryValidationStatus.VALID
    assert candidate.metadata.session_id == session.session_id
    assert candidate.metadata.is_modified is True
    assert candidate.source_status is SourceStatus.UNCHANGED
    recovery.release_candidate(candidate)


def test_scan_marks_missing_source_and_restore_requires_save_as(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    session.mark_modified("edit")
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    source_path.unlink()

    scan_result = recovery.scan_candidates()
    candidate = scan_result.candidates[0]
    restored = recovery.restore_candidate(candidate)

    assert candidate.source_status is SourceStatus.MISSING
    assert restored.recovered_from_interrupted_session is True
    assert restored.requires_save_as is True
    assert restored.recovery_source_status is SourceStatus.MISSING

    manager.release_session_lock(restored.session_id)


def test_scan_keeps_invalid_candidate_without_deleting_workspace(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    metadata_path = session.workspace_directory / manager.METADATA_NAME
    metadata_path.write_text("{invalid", encoding="utf-8")

    scan_result = recovery.scan_candidates()

    assert len(scan_result.candidates) == 1
    candidate = scan_result.candidates[0]
    assert candidate.recoverable is False
    assert candidate.discardable is True
    assert candidate.validation_status is RecoveryValidationStatus.INVALID
    assert session.workspace_directory.exists()


def test_scan_reports_invalid_candidate_when_metadata_is_missing(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    (session.workspace_directory / manager.METADATA_NAME).unlink()

    scan_result = recovery.scan_candidates()

    assert len(scan_result.candidates) == 1
    candidate = scan_result.candidates[0]
    assert candidate.recoverable is False
    assert candidate.discardable is True
    assert candidate.error_message == "metadataが見つかりません"


def test_discard_candidate_removes_workspace(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    candidate = recovery.scan_candidates().candidates[0]

    recovery.discard_candidate(candidate)

    assert not session.workspace_directory.exists()
    assert not (manager.sessions_root / session.session_id).exists()


def test_discard_invalid_metadata_candidate_removes_workspace(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    metadata_path = session.workspace_directory / manager.METADATA_NAME
    metadata_path.write_text("{broken", encoding="utf-8")
    candidate = recovery.scan_candidates().candidates[0]

    recovery.discard_candidate(candidate)

    assert not session.workspace_directory.exists()


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (
            lambda session, manager: (session.workspace_directory / manager.METADATA_NAME).unlink(),
            "metadataが見つかりません",
        ),
        (
            lambda session, manager: (
                session.workspace_directory / manager.WORKING_COPY_NAME
            ).unlink(),
            "working PDFが見つかりません",
        ),
        (
            lambda session, manager: (
                session.workspace_directory / manager.WORKING_COPY_NAME
            ).write_bytes(b"not-a-pdf"),
            None,
        ),
        (
            lambda session, manager: (
                session.workspace_directory / manager.WORKING_COPY_NAME
            ).write_bytes(b""),
            "working PDFが空です",
        ),
    ],
)
def test_discard_invalid_candidate_variants_remove_workspace(
    tmp_path: Path,
    mutator,
    expected_error: str | None,
) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    mutator(session, manager)

    candidate = recovery.scan_candidates().candidates[0]

    assert candidate.recoverable is False
    assert candidate.discardable is True
    if expected_error is not None:
        assert candidate.error_message == expected_error
    recovery.discard_candidate(candidate)

    assert not session.workspace_directory.exists()
    assert recovery.scan_candidates().candidates == []


def test_discard_rejects_unsafe_candidate(tmp_path: Path) -> None:
    manager, recovery = create_session_environment(tmp_path)
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    real_session = manager.create_session(source_path)
    recovery.write_metadata(real_session)
    manager.release_session_lock(real_session.session_id)
    symlink_path = manager.sessions_root / ("e" * 32)
    symlink_path.symlink_to(real_session.workspace_directory, target_is_directory=True)
    candidate = next(
        item
        for item in recovery.scan_candidates().candidates
        if item.error_message == "workspaceがsymlinkです"
    )

    with pytest.raises(RecoveryMetadataError, match="安全に破棄できません"):
        recovery.discard_candidate(candidate)


def test_discard_active_workspace_without_lease_is_rejected(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    candidate = recovery.scan_candidates().candidates[0]
    assert candidate.lease is not None
    candidate.lease.release()
    candidate.lease = None

    competing_lease = manager.acquire_workspace_lock(
        session.session_id,
        session.workspace_directory,
    )
    try:
        with pytest.raises(RecoveryMetadataError, match="使用中"):
            recovery.discard_candidate(candidate)
    finally:
        competing_lease.release()


def test_discard_active_workspace_locked_by_subprocess_preserves_workspace(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)

    candidate = recovery.scan_candidates().candidates[0]
    assert candidate.lease is not None
    candidate.lease.release()
    candidate.lease = None

    workspace_directory = session.workspace_directory
    source_root = Path(__file__).resolve().parents[1] / "src"
    command = [
        sys.executable,
        "-c",
        (
            "import sys, time; "
            "from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); "
            "from pdf_workbench.services.session_workspace import SessionWorkspaceManager; "
            "workspace = Path(sys.argv[2]); "
            "manager = SessionWorkspaceManager(workspace.parent); "
            "lease = manager.acquire_workspace_lock(workspace.name, workspace); "
            "time.sleep(3); "
            "lease.release()"
        ),
        str(source_root),
        str(workspace_directory),
    ]
    process = subprocess.Popen(command)
    try:
        time.sleep(0.5)
        with pytest.raises(RecoveryMetadataError, match="使用中"):
            recovery.discard_candidate(candidate)
        assert not (workspace_directory / recovery.DISCARDING_MARKER_NAME).exists()
        assert (workspace_directory / manager.WORKING_COPY_NAME).exists()
        assert (workspace_directory / manager.METADATA_NAME).exists()
        assert (workspace_directory / manager.LOCK_NAME).exists()
        assert workspace_directory.exists()
    finally:
        process.wait(timeout=10)

    rescanned = recovery.scan_candidates()
    assert len(rescanned.candidates) == 1
    recovery.discard_candidate(rescanned.candidates[0])
    assert not workspace_directory.exists()


def test_scan_marks_modified_source_status(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    source_path.write_bytes(source_path.read_bytes() + b"% changed\n")

    candidate = recovery.scan_candidates().candidates[0]

    assert candidate.source_status is SourceStatus.MODIFIED
    recovery.release_candidate(candidate)


def test_release_candidate_is_idempotent(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    candidate = recovery.scan_candidates().candidates[0]

    recovery.release_candidate(candidate)
    recovery.release_candidate(candidate)

    assert candidate.lease is None


def test_restore_candidate_rejects_nonrecoverable_candidate(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    metadata_path = session.workspace_directory / manager.METADATA_NAME
    metadata_path.write_text("{broken", encoding="utf-8")
    candidate = recovery.scan_candidates().candidates[0]

    with pytest.raises(RecoveryMetadataError, match="復元できません"):
        recovery.restore_candidate(candidate)


def test_scan_marks_symlink_workspace_as_unsafe_invalid_candidate(tmp_path: Path) -> None:
    manager, recovery = create_session_environment(tmp_path)
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    real_session = manager.create_session(source_path)
    recovery.write_metadata(real_session)
    manager.release_session_lock(real_session.session_id)
    symlink_path = manager.sessions_root / ("f" * 32)
    symlink_path.symlink_to(real_session.workspace_directory, target_is_directory=True)

    candidates = recovery.scan_candidates().candidates

    unsafe = next(
        candidate for candidate in candidates if candidate.error_message == "workspaceがsymlinkです"
    )
    assert unsafe.recoverable is False
    assert unsafe.discardable is False


def test_scan_rejects_bool_integer_fields(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    metadata_path = session.workspace_directory / manager.METADATA_NAME
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["schema_version"] = True
    payload["current_page_index"] = False
    payload["source_fingerprint"]["size_bytes"] = True
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    candidate = recovery.scan_candidates().candidates[0]

    assert candidate.recoverable is False
    assert candidate.discardable is True
    assert candidate.error_message == "schema versionに対応していません"


def test_scan_rejects_float_integer_fields(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    metadata_path = session.workspace_directory / manager.METADATA_NAME
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    payload["current_page_index"] = 1.25
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    candidate = recovery.scan_candidates().candidates[0]

    assert candidate.recoverable is False
    assert candidate.error_message == "page indexが不正です"


def test_scan_resumes_discarding_workspace_cleanup(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)
    marker_path = session.workspace_directory / recovery.DISCARDING_MARKER_NAME
    marker_path.write_text("discarding\n", encoding="utf-8")

    result = recovery.scan_candidates()

    assert result.candidates == []
    assert not session.workspace_directory.exists()


def test_write_metadata_rejects_oversized_payload(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    session.operation_history = ["x" * (1024 * 1024)]

    with pytest.raises(RecoveryMetadataError, match="大きすぎます"):
        recovery.write_metadata(session)


def test_scan_excludes_active_workspace_locked_by_subprocess(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)

    workspace_directory = session.workspace_directory
    source_root = Path(__file__).resolve().parents[1] / "src"
    command = [
        sys.executable,
        "-c",
        (
            "import sys, time; "
            "from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); "
            "from pdf_workbench.services.session_workspace import SessionWorkspaceManager; "
            "workspace = Path(sys.argv[2]); "
            "manager = SessionWorkspaceManager(workspace.parent); "
            "lease = manager.acquire_workspace_lock(workspace.name, workspace); "
            "time.sleep(3); "
            "lease.release()"
        ),
        str(source_root),
        str(workspace_directory),
    ]
    process = subprocess.Popen(command)
    try:
        time.sleep(0.5)
        concurrent_recovery = SessionRecoveryService(
            SessionWorkspaceManager(tmp_path / "sessions"),
            validator=PdfDocumentValidator(),
        )
        assert concurrent_recovery.scan_candidates().candidates == []
    finally:
        process.wait(timeout=10)

    rescanned = recovery.scan_candidates()
    assert len(rescanned.candidates) == 1
    recovery.release_candidate(rescanned.candidates[0])


def test_scan_hides_workspace_while_discard_is_in_progress(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager, recovery = create_session_environment(tmp_path)
    session = manager.create_session(source_path)
    recovery.write_metadata(session)
    manager.release_session_lock(session.session_id)

    workspace_directory = session.workspace_directory
    ready_path = tmp_path / "discard-ready.txt"
    source_root = Path(__file__).resolve().parents[1] / "src"
    command = [
        sys.executable,
        "-c",
        (
            "import sys, time; "
            "from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); "
            "from pdf_workbench.services.session_workspace import SessionWorkspaceManager; "
            "from pdf_workbench.services.session_recovery import SessionRecoveryService; "
            "workspace = Path(sys.argv[2]); "
            "ready = Path(sys.argv[3]); "
            "manager = SessionWorkspaceManager(workspace.parent); "
            "lease = manager.acquire_workspace_lock(workspace.name, workspace); "
            "marker = workspace / SessionRecoveryService.DISCARDING_MARKER_NAME; "
            "marker.write_text('discarding\\n', encoding='utf-8'); "
            "ready.write_text('ready\\n', encoding='utf-8'); "
            "time.sleep(3); "
            "lease.release()"
        ),
        str(source_root),
        str(workspace_directory),
        str(ready_path),
    ]
    process = subprocess.Popen(command)
    try:
        for _ in range(50):
            if ready_path.exists():
                break
            time.sleep(0.1)
        assert ready_path.exists()
        concurrent_recovery = SessionRecoveryService(
            SessionWorkspaceManager(tmp_path / "sessions"),
            validator=PdfDocumentValidator(),
        )
        assert concurrent_recovery.scan_candidates().candidates == []
    finally:
        process.wait(timeout=10)

    rescanned = recovery.scan_candidates()
    assert rescanned.candidates == []
    assert not workspace_directory.exists()
