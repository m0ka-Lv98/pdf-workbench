from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from pdf_test_utils import create_blank_pdf
from pdf_workbench.domain.document_session import DocumentSession, FileFingerprint, SourceStatus
from pdf_workbench.services.source_change_monitor import (
    SourceChangeMonitor,
    SourceCheckResult,
    SourceFileInspector,
)


def create_session(tmp_path: Path) -> DocumentSession:
    source_path = create_blank_pdf(tmp_path / "source.pdf", 1)
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


def test_source_file_inspector_reports_unchanged(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    inspector = SourceFileInspector()

    result = inspector.inspect(session.source_path, session.source_fingerprint)

    assert result.status is SourceStatus.UNCHANGED
    assert result.current_fingerprint == session.source_fingerprint


def test_source_file_inspector_reports_modified_after_atomic_replace(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    inspector = SourceFileInspector()
    replacement = create_blank_pdf(tmp_path / "replacement.pdf", 2)

    replacement_bytes = replacement.read_bytes()
    session.source_path.write_bytes(replacement_bytes)

    result = inspector.inspect(session.source_path, session.source_fingerprint)

    assert result.status is SourceStatus.MODIFIED
    assert result.current_fingerprint != session.source_fingerprint


def test_source_file_inspector_reports_missing(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    inspector = SourceFileInspector()
    session.source_path.unlink()

    result = inspector.inspect(session.source_path, session.source_fingerprint)

    assert result.status is SourceStatus.MISSING
    assert result.current_fingerprint is None


def test_source_file_inspector_reports_directory_as_unreadable(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    inspector = SourceFileInspector()

    result = inspector.inspect(tmp_path, session.source_fingerprint)

    assert result.status is SourceStatus.UNREADABLE
    assert result.current_fingerprint is None


def test_source_change_monitor_emits_when_directory_event_detects_recreated_file(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    session = create_session(tmp_path)
    monitor = SourceChangeMonitor(poll_interval_ms=10, debounce_interval_ms=10)
    captured: list[SourceCheckResult] = []
    monitor.source_status_changed.connect(lambda _sid, result: captured.append(result))

    monitor.register_session(session)
    session.source_path.unlink()
    replacement = create_blank_pdf(tmp_path / "replacement.pdf", 2)
    session.source_path.write_bytes(replacement.read_bytes())
    monitor._on_directory_changed(str(session.source_path.parent))

    qtbot.waitUntil(lambda: any(item.status is SourceStatus.MODIFIED for item in captured))

    assert captured[-1].status is SourceStatus.MODIFIED
    monitor.shutdown()


def test_source_change_monitor_rechecks_on_application_activation(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    session = create_session(tmp_path)
    monitor = SourceChangeMonitor(poll_interval_ms=50, debounce_interval_ms=10)
    captured: list[SourceCheckResult] = []
    monitor.source_status_changed.connect(lambda _sid, result: captured.append(result))
    monitor.register_session(session)

    session.source_path.unlink()
    monitor._on_application_state_changed(Qt.ApplicationState.ApplicationActive)

    qtbot.waitUntil(lambda: any(item.status is SourceStatus.MISSING for item in captured))
    assert captured[-1].status is SourceStatus.MISSING
    monitor.shutdown()


def test_source_change_monitor_shutdown_stops_future_emits(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    session = create_session(tmp_path)
    monitor = SourceChangeMonitor(poll_interval_ms=10, debounce_interval_ms=10)
    captured: list[SourceCheckResult] = []
    monitor.source_status_changed.connect(lambda _sid, result: captured.append(result))
    monitor.register_session(session)
    monitor.shutdown()
    session.source_path.unlink()
    monitor._on_application_state_changed(Qt.ApplicationState.ApplicationActive)
    qtbot.wait(30)

    assert len(captured) == 1


def test_source_change_monitor_keeps_parent_watch_for_missing_file(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    monitor = SourceChangeMonitor(poll_interval_ms=10, debounce_interval_ms=10)
    monitor.register_session(session)
    session.source_path.unlink()
    monitor.check_session_now(session)

    assert str(session.source_path.parent) in monitor._watcher.directories()
    assert str(session.source_path) not in monitor._watcher.files()
    monitor.shutdown()


def test_source_change_monitor_refresh_baseline_switches_watched_file(tmp_path: Path) -> None:
    session = create_session(tmp_path)
    monitor = SourceChangeMonitor(poll_interval_ms=10, debounce_interval_ms=10)
    monitor.register_session(session)
    old_path = session.source_path
    new_path = tmp_path / "moved.pdf"
    new_path.write_bytes(b"%PDF-1.7\n% moved\n")
    session.mark_saved(new_path, FileFingerprint.from_path(new_path), session.created_at)
    monitor.refresh_baseline(session)

    assert str(old_path) not in monitor._watcher.files()
    assert str(new_path) in monitor._watcher.files()
    monitor.shutdown()


def test_source_change_monitor_registers_missing_source_and_rearms_file_watch_after_recreate(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    session = create_session(tmp_path)
    monitor = SourceChangeMonitor(poll_interval_ms=10, debounce_interval_ms=10)
    session.source_path.unlink()
    captured: list[SourceCheckResult] = []
    monitor.source_status_changed.connect(lambda _sid, result: captured.append(result))

    monitor.register_session(session)

    assert session.session_id in monitor._file_to_sessions[session.source_path]
    assert str(session.source_path.parent) in monitor._watcher.directories()
    assert str(session.source_path) not in monitor._watcher.files()

    replacement = create_blank_pdf(tmp_path / "replacement.pdf", 2)
    session.source_path.write_bytes(replacement.read_bytes())
    monitor._on_directory_changed(str(session.source_path.parent))
    qtbot.waitUntil(lambda: str(session.source_path) in monitor._watcher.files())

    assert session.session_id in monitor._file_to_sessions[session.source_path]

    second_replacement = create_blank_pdf(tmp_path / "replacement-2.pdf", 3)
    session.source_path.write_bytes(second_replacement.read_bytes())
    monitor._on_file_changed(str(session.source_path))
    qtbot.waitUntil(lambda: any(item.status is SourceStatus.MODIFIED for item in captured))
    assert captured[-1].status is SourceStatus.MODIFIED
    monitor.shutdown()


def test_source_change_monitor_ignores_unregistered_session_for_direct_check(
    tmp_path: Path,
) -> None:
    session = create_session(tmp_path)
    monitor = SourceChangeMonitor(poll_interval_ms=10, debounce_interval_ms=10)

    result = monitor.check_session_now(session)

    assert result.status is SourceStatus.UNCHANGED
    assert session.session_id not in monitor._sessions
    monitor.shutdown()


def test_source_change_monitor_directory_watch_ref_counts_sessions(tmp_path: Path) -> None:
    first = create_session(tmp_path)
    second_source_path = create_blank_pdf(tmp_path / "second-source.pdf", 1)
    second_workspace = tmp_path / "workspace-second"
    second_workspace.mkdir()
    second_working_copy = second_workspace / "working.pdf"
    second_working_copy.write_bytes(second_source_path.read_bytes())
    second = DocumentSession(
        source_path=second_source_path,
        working_copy_path=second_working_copy,
        workspace_directory=second_workspace,
        source_fingerprint=FileFingerprint.from_path(second_source_path),
    )
    monitor = SourceChangeMonitor(poll_interval_ms=10, debounce_interval_ms=10)

    monitor.register_session(first)
    monitor.register_session(second)

    directory_text = str(first.source_path.parent)
    assert directory_text in monitor._watcher.directories()

    monitor.unregister_session(first.session_id)
    assert directory_text in monitor._watcher.directories()

    monitor.unregister_session(second.session_id)
    assert directory_text not in monitor._watcher.directories()
    monitor.shutdown()


def test_source_file_inspector_converts_resolve_errors_to_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = create_session(tmp_path)
    inspector = SourceFileInspector()

    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )

    result = inspector.inspect(session.source_path, session.source_fingerprint)

    assert result.status is SourceStatus.UNREADABLE


def test_source_file_inspector_converts_symlink_loop_to_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = create_session(tmp_path)
    inspector = SourceFileInspector()

    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(RuntimeError("symlink loop")),
    )

    result = inspector.inspect(session.source_path, session.source_fingerprint)

    assert result.status is SourceStatus.UNREADABLE
