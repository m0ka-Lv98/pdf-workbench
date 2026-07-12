from __future__ import annotations

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


def test_pdf_save_service_saves_new_target_and_updates_session(tmp_path: Path) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path, page_count=2)
    session.mark_modified("reorder placeholder")
    target_path = tmp_path / "saved.pdf"

    result = service.save_atomic(session, target_path, expected_page_count=2)

    assert result.target_path == target_path.resolve()
    assert target_path.exists()
    assert len(PdfReader(str(target_path)).pages) == 2
    assert session.source_path == target_path.resolve()
    assert session.is_modified is False
    assert session.source_fingerprint == FileFingerprint.from_path(target_path)


def test_pdf_save_service_keeps_existing_target_on_writer_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    before = target_path.read_bytes()

    monkeypatch.setattr(
        service,
        "_write_temp_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfSaveError("writer failure")),
    )

    with pytest.raises(PdfSaveError, match="writer failure"):
        service.save_atomic(session, target_path, expected_page_count=1)

    assert target_path.read_bytes() == before
    assert session.source_path != target_path.resolve()
    assert session.is_modified is False
    assert not list(target_path.parent.glob(".*.tmp.pdf"))


def test_pdf_save_service_keeps_existing_target_on_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"original-bytes")
    before = target_path.read_bytes()

    monkeypatch.setattr(
        service,
        "_validate_saved_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfValidationError("validation failure")),
    )

    with pytest.raises(PdfValidationError, match="validation failure"):
        service.save_atomic(session, target_path, expected_page_count=1)

    assert target_path.read_bytes() == before
    assert session.source_path != target_path.resolve()
    assert session.is_modified is False
    assert not list(target_path.parent.glob(".*.tmp.pdf"))


def test_pdf_save_service_keeps_session_dirty_on_replace_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    session.mark_modified("page operation")
    target_path = tmp_path / "target.pdf"

    monkeypatch.setattr(
        service,
        "_replace_atomically",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AtomicReplaceError("replace failure")),
    )

    with pytest.raises(AtomicReplaceError, match="replace failure"):
        service.save_atomic(session, target_path, expected_page_count=1)

    assert session.is_modified is True
    assert session.source_path != target_path.resolve()
    assert not list(target_path.parent.glob(".*.tmp.pdf"))


def test_pdf_save_service_allows_save_to_same_source_path(tmp_path: Path) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path, page_count=1)
    session.mark_modified("touch")
    original_source = session.source_path

    result = service.save_atomic(session, original_source, expected_page_count=1)

    assert result.target_path == original_source
    assert original_source.exists()
    assert len(PdfReader(str(original_source)).pages) == 1
    assert session.source_path == original_source
    assert session.is_modified is False


def test_pdf_save_service_cleans_temp_file_after_success(tmp_path: Path) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "saved.pdf"

    service.save_atomic(session, target_path, expected_page_count=1)

    assert not any(path.name.endswith(".tmp.pdf") for path in target_path.parent.iterdir())


def test_pdf_save_service_cleans_temp_file_after_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path)
    target_path = tmp_path / "saved.pdf"

    monkeypatch.setattr(
        service,
        "_replace_atomically",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AtomicReplaceError("replace failure")),
    )

    with pytest.raises(AtomicReplaceError):
        service.save_atomic(session, target_path, expected_page_count=1)

    assert not any(path.name.endswith(".tmp.pdf") for path in target_path.parent.iterdir())


def test_pdf_save_service_detects_page_count_mismatch(tmp_path: Path) -> None:
    service = PdfSaveService()
    session = create_session(tmp_path, page_count=2)

    with pytest.raises(PdfValidationError, match="ページ数"):
        service.save_atomic(session, tmp_path / "saved.pdf", expected_page_count=1)
