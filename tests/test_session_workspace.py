from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pdf_test_utils import create_blank_pdf
from pdf_workbench.services.session_workspace import SessionWorkspaceManager, WorkspaceCreationError


def test_workspace_manager_creates_working_copy_without_modifying_source(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    source_bytes = source_path.read_bytes()
    manager = SessionWorkspaceManager(tmp_path / "sessions")

    session = manager.create_session(source_path)

    assert session.source_path == source_path.resolve()
    assert session.document_path.read_bytes() == source_bytes
    assert session.source_path.read_bytes() == source_bytes
    assert session.workspace_directory.parent == manager.sessions_root
    assert session.workspace_directory.name == session.session_id
    assert (session.workspace_directory / manager.LOCK_NAME).exists()


def test_workspace_manager_creates_unique_session_directories(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager = SessionWorkspaceManager(tmp_path / "sessions")

    first = manager.create_session(source_path)
    second = manager.create_session(source_path)

    assert first.workspace_directory != second.workspace_directory
    assert first.document_path != second.document_path


def test_workspace_manager_cleans_partial_directory_on_copy_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager = SessionWorkspaceManager(tmp_path / "sessions")

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr(shutil, "copy2", fail_copy)

    with pytest.raises(WorkspaceCreationError, match="作業コピー"):
        manager.create_session(source_path)

    assert list(manager.sessions_root.iterdir()) == []


def test_workspace_manager_cleanup_removes_only_target_session_directory(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager = SessionWorkspaceManager(tmp_path / "sessions")
    first = manager.create_session(source_path)
    second = manager.create_session(source_path)

    manager.cleanup_session(first)

    assert not first.workspace_directory.exists()
    assert second.workspace_directory.exists()


def test_workspace_manager_cleanup_is_idempotent(tmp_path: Path) -> None:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    manager = SessionWorkspaceManager(tmp_path / "sessions")
    session = manager.create_session(source_path)

    manager.cleanup_session(session)
    manager.cleanup_session(session)

    assert not session.workspace_directory.exists()


def test_workspace_manager_detects_managed_paths_safely(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    manager = SessionWorkspaceManager(sessions_root)
    managed_file = sessions_root / "abc" / "working.pdf"
    external_file = tmp_path / "sessions-copy" / "abc.pdf"

    assert manager.contains_managed_path(managed_file) is True
    assert manager.contains_managed_path(external_file) is False
