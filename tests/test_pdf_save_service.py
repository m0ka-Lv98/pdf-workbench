from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest
from pypdf import PdfReader

from pdf_test_utils import create_blank_pdf
from pdf_workbench.domain.document_session import DocumentSession, FileFingerprint
from pdf_workbench.services.pdf_save_service import (
    AtomicReplaceError,
    PdfSaveError,
    PdfSaveService,
    PdfValidationError,
    TargetChangedError,
    TargetSnapshot,
)


def create_session(tmp_path: Path, *, page_count: int = 1) -> DocumentSession:
    source_path = create_blank_pdf(tmp_path / "source.pdf", page_count)
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


def mark_session_dirty(
    session: DocumentSession,
) -> tuple[Path, FileFingerprint, datetime | None, bytes]:
    session.mark_modified("test operation")
    return (
        session.source_path,
        session.source_fingerprint,
        session.last_saved_at,
        session.document_path.read_bytes(),
    )


def assert_failed_save_state(
    session: DocumentSession,
    *,
    original_source_path: Path,
    original_fingerprint: FileFingerprint,
    original_saved_at: datetime | None,
    original_working_copy_bytes: bytes,
    target_path: Path,
    original_target_bytes: bytes | None,
) -> None:
    assert session.is_modified is True
    assert session.source_path == original_source_path
    assert session.source_fingerprint == original_fingerprint
    assert session.last_saved_at == original_saved_at
    assert session.document_path.read_bytes() == original_working_copy_bytes
    if original_target_bytes is None:
        assert not target_path.exists()
    else:
        assert target_path.read_bytes() == original_target_bytes


def temp_candidates(directory: Path) -> list[Path]:
    return list(directory.glob(".*.tmp.pdf"))


def target_snapshot(path: Path) -> TargetSnapshot:
    return TargetSnapshot.capture(path)


def test_pdf_save_service_saves_new_target_and_updates_session(tmp_path: Path) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path, page_count=2)
    session.mark_modified("reorder placeholder")
    target_path = tmp_path / "saved.pdf"

    result = service.save_atomic(
        session,
        target_path,
        expected_page_count=2,
        target_snapshot=target_snapshot(target_path),
    )

    assert result.target_path == target_path.resolve()
    assert target_path.exists()
    assert len(PdfReader(str(target_path)).pages) == 2
    assert session.source_path == target_path.resolve()
    assert session.is_modified is False
    assert session.source_fingerprint == FileFingerprint.from_path(target_path)


def test_pdf_save_service_normalizes_temp_creation_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    original_target_bytes = target_path.read_bytes()
    original_source_path, original_fingerprint, original_saved_at, working_copy_bytes = (
        mark_session_dirty(session)
    )

    monkeypatch.setattr(
        service,
        "_create_temp_output_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("mkstemp failure")),
    )

    with pytest.raises(PdfSaveError, match="保存準備"):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=target_snapshot(target_path),
        )

    assert_failed_save_state(
        session,
        original_source_path=original_source_path,
        original_fingerprint=original_fingerprint,
        original_saved_at=original_saved_at,
        original_working_copy_bytes=working_copy_bytes,
        target_path=target_path,
        original_target_bytes=original_target_bytes,
    )
    assert temp_candidates(target_path.parent) == []


def test_pdf_save_service_keeps_existing_target_on_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    original_target_bytes = target_path.read_bytes()
    original_source_path, original_fingerprint, original_saved_at, working_copy_bytes = (
        mark_session_dirty(session)
    )

    monkeypatch.setattr(
        service,
        "_write_temp_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfSaveError("writer failure")),
    )

    with pytest.raises(PdfSaveError, match="writer failure"):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=target_snapshot(target_path),
        )

    assert_failed_save_state(
        session,
        original_source_path=original_source_path,
        original_fingerprint=original_fingerprint,
        original_saved_at=original_saved_at,
        original_working_copy_bytes=working_copy_bytes,
        target_path=target_path,
        original_target_bytes=original_target_bytes,
    )
    assert temp_candidates(target_path.parent) == []


def test_pdf_save_service_keeps_existing_target_on_fsync_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    original_target_bytes = target_path.read_bytes()
    original_source_path, original_fingerprint, original_saved_at, working_copy_bytes = (
        mark_session_dirty(session)
    )

    monkeypatch.setattr(
        service,
        "_fsync_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfSaveError("fsync failure")),
    )

    with pytest.raises(PdfSaveError, match="fsync failure"):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=target_snapshot(target_path),
        )

    assert_failed_save_state(
        session,
        original_source_path=original_source_path,
        original_fingerprint=original_fingerprint,
        original_saved_at=original_saved_at,
        original_working_copy_bytes=working_copy_bytes,
        target_path=target_path,
        original_target_bytes=original_target_bytes,
    )
    assert temp_candidates(target_path.parent) == []


def test_pdf_save_service_keeps_existing_target_on_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    original_target_bytes = target_path.read_bytes()
    original_source_path, original_fingerprint, original_saved_at, working_copy_bytes = (
        mark_session_dirty(session)
    )

    monkeypatch.setattr(
        service,
        "_validate_saved_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfValidationError("validation failure")),
    )

    with pytest.raises(PdfValidationError, match="validation failure"):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=target_snapshot(target_path),
        )

    assert_failed_save_state(
        session,
        original_source_path=original_source_path,
        original_fingerprint=original_fingerprint,
        original_saved_at=original_saved_at,
        original_working_copy_bytes=working_copy_bytes,
        target_path=target_path,
        original_target_bytes=original_target_bytes,
    )
    assert temp_candidates(target_path.parent) == []


def test_pdf_save_service_keeps_existing_target_on_fingerprint_failure_before_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    original_target_bytes = target_path.read_bytes()
    original_source_path, original_fingerprint, original_saved_at, working_copy_bytes = (
        mark_session_dirty(session)
    )
    replace_called = False
    original_from_path = FileFingerprint.from_path

    def fail_for_temp(path: Path) -> FileFingerprint:
        nonlocal replace_called
        assert replace_called is False
        if path.name.endswith(".tmp.pdf"):
            raise OSError("stat failure")
        return original_from_path(path)

    def track_replace(*_args: object, **_kwargs: object) -> None:
        nonlocal replace_called
        replace_called = True

    monkeypatch.setattr(FileFingerprint, "from_path", staticmethod(fail_for_temp))
    monkeypatch.setattr(service, "_replace_atomically", track_replace)

    with pytest.raises(PdfSaveError, match="メタデータ取得"):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=target_snapshot(target_path),
        )

    assert replace_called is False
    assert_failed_save_state(
        session,
        original_source_path=original_source_path,
        original_fingerprint=original_fingerprint,
        original_saved_at=original_saved_at,
        original_working_copy_bytes=working_copy_bytes,
        target_path=target_path,
        original_target_bytes=original_target_bytes,
    )
    assert temp_candidates(target_path.parent) == []


def test_pdf_save_service_keeps_existing_target_on_mode_application_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    original_target_bytes = target_path.read_bytes()
    original_source_path, original_fingerprint, original_saved_at, working_copy_bytes = (
        mark_session_dirty(session)
    )

    monkeypatch.setattr(
        service,
        "_apply_existing_target_mode",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfSaveError("mode application failure")),
    )

    with pytest.raises(PdfSaveError, match="mode application failure"):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=target_snapshot(target_path),
        )

    assert_failed_save_state(
        session,
        original_source_path=original_source_path,
        original_fingerprint=original_fingerprint,
        original_saved_at=original_saved_at,
        original_working_copy_bytes=working_copy_bytes,
        target_path=target_path,
        original_target_bytes=original_target_bytes,
    )
    assert temp_candidates(target_path.parent) == []


def test_pdf_save_service_keeps_session_dirty_on_replace_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    original_target_bytes = target_path.read_bytes()
    original_source_path, original_fingerprint, original_saved_at, working_copy_bytes = (
        mark_session_dirty(session)
    )

    monkeypatch.setattr(
        service,
        "_replace_atomically",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AtomicReplaceError("replace failure")),
    )

    with pytest.raises(AtomicReplaceError, match="replace failure"):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=target_snapshot(target_path),
        )

    assert_failed_save_state(
        session,
        original_source_path=original_source_path,
        original_fingerprint=original_fingerprint,
        original_saved_at=original_saved_at,
        original_working_copy_bytes=working_copy_bytes,
        target_path=target_path,
        original_target_bytes=original_target_bytes,
    )
    assert temp_candidates(target_path.parent) == []


def test_pdf_save_service_rejects_save_into_current_workspace(tmp_path: Path) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    original_source_path, original_fingerprint, original_saved_at, working_copy_bytes = (
        mark_session_dirty(session)
    )

    with pytest.raises(PdfSaveError, match="一時作業フォルダ"):
        service.save_atomic(
            session,
            session.workspace_directory / "rejected.pdf",
            expected_page_count=1,
            target_snapshot=TargetSnapshot(exists=False, fingerprint=None),
        )

    assert_failed_save_state(
        session,
        original_source_path=original_source_path,
        original_fingerprint=original_fingerprint,
        original_saved_at=original_saved_at,
        original_working_copy_bytes=working_copy_bytes,
        target_path=session.workspace_directory / "rejected.pdf",
        original_target_bytes=None,
    )


def test_pdf_save_service_allows_save_to_same_source_path(tmp_path: Path) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path, page_count=1)
    session.mark_modified("touch")
    original_source = session.source_path

    result = service.save_atomic(
        session,
        original_source,
        expected_page_count=1,
        target_snapshot=target_snapshot(original_source),
    )

    assert result.target_path == original_source
    assert original_source.exists()
    assert len(PdfReader(str(original_source)).pages) == 1
    assert session.source_path == original_source
    assert session.is_modified is False


def test_pdf_save_service_cleans_temp_file_after_success(tmp_path: Path) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "saved.pdf"

    service.save_atomic(
        session,
        target_path,
        expected_page_count=1,
        target_snapshot=target_snapshot(target_path),
    )

    assert temp_candidates(target_path.parent) == []


def test_pdf_save_service_cleanup_failure_does_not_mask_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "saved.pdf"
    session.mark_modified("test operation")
    created_temp = tmp_path / ".saved.test.tmp.pdf"
    created_temp.write_bytes(b"temp")

    monkeypatch.setattr(service, "_create_temp_output_path", lambda *_args: created_temp)
    monkeypatch.setattr(
        service,
        "_write_temp_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfSaveError("writer failure")),
    )
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(OSError("unlink failure")),
    )

    with pytest.raises(PdfSaveError, match="writer failure"):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=target_snapshot(target_path),
        )

    assert "Failed to remove temp PDF after save error" in caplog.text


def test_pdf_save_service_cleanup_failure_does_not_mask_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "saved.pdf"
    session.mark_modified("test operation")
    created_temp = tmp_path / ".saved.test.tmp.pdf"
    created_temp.write_bytes(b"temp")

    monkeypatch.setattr(service, "_create_temp_output_path", lambda *_args: created_temp)
    monkeypatch.setattr(
        service,
        "_validate_saved_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfValidationError("validation failure")),
    )
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda self: (_ for _ in ()).throw(OSError("unlink failure")),
    )

    with pytest.raises(PdfValidationError, match="validation failure"):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=target_snapshot(target_path),
        )

    assert "Failed to remove temp PDF after save error" in caplog.text


def test_pdf_save_service_detects_page_count_mismatch(tmp_path: Path) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path, page_count=2)

    with pytest.raises(PdfValidationError, match="ページ数"):
        service.save_atomic(
            session,
            tmp_path / "saved.pdf",
            expected_page_count=1,
            target_snapshot=TargetSnapshot(exists=False, fingerprint=None),
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode preservation is not portable on Windows")
def test_pdf_save_service_preserves_existing_target_mode(tmp_path: Path) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    session.mark_modified("test operation")
    target_path = tmp_path / "mode-target.pdf"
    target_path.write_bytes(b"original-bytes")
    os.chmod(target_path, 0o640)

    service.save_atomic(
        session,
        target_path,
        expected_page_count=1,
        target_snapshot=target_snapshot(target_path),
    )

    assert (target_path.stat().st_mode & 0o777) == 0o640


def test_pdf_save_service_detects_existing_target_change_before_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    session.mark_modified("test operation")
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    snapshot = target_snapshot(target_path)
    original_target_bytes = target_path.read_bytes()
    original_ensure = service._ensure_target_snapshot_matches

    def mutate_before_replace(path: Path, current_snapshot: TargetSnapshot) -> None:
        target_path.write_bytes(b"changed-elsewhere")
        original_ensure(path, current_snapshot)

    monkeypatch.setattr(service, "_ensure_target_snapshot_matches", mutate_before_replace)

    with pytest.raises(TargetChangedError):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=snapshot,
        )

    assert session.is_modified is True
    assert target_path.read_bytes() != session.document_path.read_bytes()
    assert target_path.read_bytes() != original_target_bytes
    assert temp_candidates(target_path.parent) == []


def test_pdf_save_service_detects_new_target_created_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    session.mark_modified("test operation")
    target_path = tmp_path / "new-target.pdf"
    snapshot = TargetSnapshot(exists=False, fingerprint=None)
    original_ensure = service._ensure_target_snapshot_matches

    def create_target_before_replace(path: Path, current_snapshot: TargetSnapshot) -> None:
        target_path.write_bytes(b"created-elsewhere")
        original_ensure(path, current_snapshot)

    monkeypatch.setattr(service, "_ensure_target_snapshot_matches", create_target_before_replace)

    with pytest.raises(TargetChangedError):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=snapshot,
        )

    assert target_path.read_bytes() == b"created-elsewhere"
    assert session.is_modified is True
    assert temp_candidates(target_path.parent) == []


def test_pdf_save_service_detects_target_change_during_mode_application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    session.mark_modified("test operation")
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    snapshot = target_snapshot(target_path)
    external_bytes = b"changed-during-mode"
    call_count = 0
    original_ensure = service._ensure_target_snapshot_matches

    def mutate_on_second_check(path: Path, current_snapshot: TargetSnapshot) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            target_path.write_bytes(external_bytes)
        original_ensure(path, current_snapshot)

    monkeypatch.setattr(service, "_ensure_target_snapshot_matches", mutate_on_second_check)

    with pytest.raises(TargetChangedError):
        service.save_atomic(
            session,
            target_path,
            expected_page_count=1,
            target_snapshot=snapshot,
        )

    assert target_path.read_bytes() == external_bytes
    assert session.is_modified is True
    assert temp_candidates(target_path.parent) == []


def test_target_snapshot_capture_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        TargetSnapshot.capture(tmp_path)
