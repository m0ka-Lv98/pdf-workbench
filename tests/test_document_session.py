from pathlib import Path

import pytest

from pdf_workbench.domain.document_session import DocumentSession


def test_document_session_normalizes_pdf_path(tmp_path: Path) -> None:
    path = tmp_path / "sample.pdf"
    path.touch()
    session = DocumentSession(path)
    assert session.source_path == path.resolve()
    assert session.is_modified is False


def test_document_session_rejects_non_pdf(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PDF"):
        DocumentSession(tmp_path / "sample.txt")


def test_mark_modified_records_operation(tmp_path: Path) -> None:
    path = tmp_path / "sample.pdf"
    path.touch()
    session = DocumentSession(path)
    session.mark_modified("rotate page 1")
    assert session.is_modified is True
    assert session.operation_history == ["rotate page 1"]
