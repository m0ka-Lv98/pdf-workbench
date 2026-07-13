from __future__ import annotations

import logging
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from pdf_workbench.domain.document_session import DocumentSession, FileFingerprint, SourceStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceCheckResult:
    path: Path
    status: SourceStatus
    expected_fingerprint: FileFingerprint
    current_fingerprint: FileFingerprint | None
    checked_at: datetime
    error_message: str | None


class SourceFileInspector:
    def inspect(
        self,
        path: Path,
        expected_fingerprint: FileFingerprint,
    ) -> SourceCheckResult:
        checked_at = datetime.now(UTC)
        expanded_path = path.expanduser()
        fallback_path = expanded_path if expanded_path.is_absolute() else expanded_path.absolute()
        try:
            resolved_path = expanded_path.resolve()
            stat_result = resolved_path.stat()
        except FileNotFoundError:
            return SourceCheckResult(
                path=fallback_path,
                status=SourceStatus.MISSING,
                expected_fingerprint=expected_fingerprint,
                current_fingerprint=None,
                checked_at=checked_at,
                error_message="元のPDFが見つかりません",
            )
        except RuntimeError as exc:
            return SourceCheckResult(
                path=fallback_path,
                status=SourceStatus.UNREADABLE,
                expected_fingerprint=expected_fingerprint,
                current_fingerprint=None,
                checked_at=checked_at,
                error_message=f"元のPDFの状態を確認できません: {exc}",
            )
        except OSError as exc:
            return SourceCheckResult(
                path=fallback_path,
                status=SourceStatus.UNREADABLE,
                expected_fingerprint=expected_fingerprint,
                current_fingerprint=None,
                checked_at=checked_at,
                error_message=f"元のPDFの状態を確認できません: {exc}",
            )

        if not stat.S_ISREG(stat_result.st_mode):
            return SourceCheckResult(
                path=resolved_path,
                status=SourceStatus.UNREADABLE,
                expected_fingerprint=expected_fingerprint,
                current_fingerprint=None,
                checked_at=checked_at,
                error_message="元のPDFがファイルとして読み取れません",
            )

        current_fingerprint = FileFingerprint(
            size_bytes=stat_result.st_size,
            modified_time_ns=stat_result.st_mtime_ns,
        )
        status = (
            SourceStatus.UNCHANGED
            if current_fingerprint == expected_fingerprint
            else SourceStatus.MODIFIED
        )
        error_message = None
        if status is SourceStatus.MODIFIED:
            error_message = "元のPDFが外部で変更されています"
        return SourceCheckResult(
            path=resolved_path,
            status=status,
            expected_fingerprint=expected_fingerprint,
            current_fingerprint=current_fingerprint,
            checked_at=checked_at,
            error_message=error_message,
        )


class SourceChangeMonitor(QObject):
    source_status_changed = Signal(str, object)

    def __init__(
        self,
        *,
        inspector: SourceFileInspector | None = None,
        poll_interval_ms: int = 2000,
        debounce_interval_ms: int = 180,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._inspector = inspector if inspector is not None else SourceFileInspector()
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_directory_changed)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(poll_interval_ms)
        self._poll_timer.timeout.connect(self.check_all_now)
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(debounce_interval_ms)
        self._debounce_timer.timeout.connect(self._process_pending_checks)
        self._sessions: dict[str, DocumentSession] = {}
        self._session_paths: dict[str, Path] = {}
        self._last_results: dict[str, SourceCheckResult] = {}
        self._file_to_sessions: dict[Path, set[str]] = {}
        self._directory_to_sessions: dict[Path, set[str]] = {}
        self._pending_session_ids: set[str] = set()
        self._application_state_connected = False
        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            app.applicationStateChanged.connect(self._on_application_state_changed)
            self._application_state_connected = True
        self._poll_timer.start()

    @property
    def poll_interval_ms(self) -> int:
        return self._poll_timer.interval()

    def register_session(self, session: DocumentSession) -> None:
        self.unregister_session(session.session_id)
        self._sessions[session.session_id] = session
        self._session_paths[session.session_id] = session.source_path.expanduser().resolve()
        self._register_paths(session)
        self._sync_watcher_paths()
        self._emit_if_changed(session, self.inspect_session(session))

    def unregister_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        session_path = self._session_paths.pop(session_id, None)
        self._last_results.pop(session_id, None)
        self._pending_session_ids.discard(session_id)
        if session is None or session_path is None:
            return
        self._remove_session_from_watch_maps(session_id, session_path)
        self._sync_watcher_paths()

    def refresh_baseline(self, session: DocumentSession) -> None:
        previous_path = self._session_paths.get(session.session_id)
        if previous_path is not None:
            self._remove_session_from_watch_maps(session.session_id, previous_path)
        self._sessions[session.session_id] = session
        self._session_paths[session.session_id] = session.source_path.expanduser().resolve()
        self._register_paths(session)
        self._rearm_file_watch(session.source_path.expanduser().resolve())
        self._sync_watcher_paths()
        self._last_results[session.session_id] = self.inspect_session(session)

    def check_session_now(self, session: DocumentSession) -> SourceCheckResult:
        if session.session_id not in self._sessions:
            return self.inspect_session(session)
        self._sessions[session.session_id] = session
        result = self.inspect_session(session)
        self._rearm_file_watch(session.source_path.expanduser().resolve())
        self._sync_watcher_paths()
        self._emit_if_changed(session, result)
        return result

    def check_all_now(self) -> None:
        for session in list(self._sessions.values()):
            self.check_session_now(session)

    def shutdown(self) -> None:
        self._poll_timer.stop()
        self._debounce_timer.stop()
        for path in list(self._watcher.files()):
            self._watcher.removePath(path)
        for path in list(self._watcher.directories()):
            self._watcher.removePath(path)
        app = QGuiApplication.instance()
        if self._application_state_connected and isinstance(app, QGuiApplication):
            with suppress(RuntimeError, TypeError):
                app.applicationStateChanged.disconnect(self._on_application_state_changed)
            self._application_state_connected = False
        self._sessions.clear()
        self._session_paths.clear()
        self._last_results.clear()
        self._file_to_sessions.clear()
        self._directory_to_sessions.clear()
        self._pending_session_ids.clear()

    def inspect_session(self, session: DocumentSession) -> SourceCheckResult:
        return self._inspector.inspect(session.source_path, session.source_fingerprint)

    def _register_paths(self, session: DocumentSession) -> None:
        resolved_path = session.source_path.expanduser().resolve()
        directory = resolved_path.parent
        self._directory_to_sessions.setdefault(directory, set()).add(session.session_id)
        self._file_to_sessions.setdefault(resolved_path, set()).add(session.session_id)
        self._ensure_directory_watch(directory)
        if resolved_path.exists():
            self._ensure_file_watch(resolved_path)

    def _remove_session_from_watch_maps(self, session_id: str, source_path: Path) -> None:
        resolved_path = source_path.expanduser().resolve()
        directory = resolved_path.parent
        file_sessions = self._file_to_sessions.get(resolved_path)
        if file_sessions is not None:
            file_sessions.discard(session_id)
            if not file_sessions:
                self._file_to_sessions.pop(resolved_path, None)
                self._remove_file_watch(resolved_path)
        directory_sessions = self._directory_to_sessions.get(directory)
        if directory_sessions is not None:
            directory_sessions.discard(session_id)
            if not directory_sessions:
                self._directory_to_sessions.pop(directory, None)
                self._remove_directory_watch(directory)

    def _ensure_file_watch(self, path: Path) -> None:
        path_text = str(path)
        if path_text not in self._watcher.files():
            self._watcher.addPath(path_text)

    def _ensure_directory_watch(self, path: Path) -> None:
        path_text = str(path)
        if path_text not in self._watcher.directories():
            self._watcher.addPath(path_text)

    def _remove_file_watch(self, path: Path) -> None:
        path_text = str(path)
        if path_text in self._watcher.files():
            self._watcher.removePath(path_text)

    def _remove_directory_watch(self, path: Path) -> None:
        path_text = str(path)
        if path_text in self._watcher.directories():
            self._watcher.removePath(path_text)

    def _on_file_changed(self, path_text: str) -> None:
        path = Path(path_text)
        self._pending_session_ids.update(self._file_to_sessions.get(path, set()))
        for session_id in list(self._file_to_sessions.get(path, set())):
            session = self._sessions.get(session_id)
            if session is not None:
                self._rearm_file_watch(session.source_path.expanduser().resolve())
        self._debounce_timer.start()

    def _on_directory_changed(self, path_text: str) -> None:
        path = Path(path_text)
        self._pending_session_ids.update(self._directory_to_sessions.get(path, set()))
        for session_id in list(self._directory_to_sessions.get(path, set())):
            session = self._sessions.get(session_id)
            if session is not None:
                self._rearm_file_watch(session.source_path.expanduser().resolve())
        self._debounce_timer.start()

    def _rearm_file_watch(self, path: Path) -> None:
        if path.exists():
            self._ensure_file_watch(path)
        else:
            self._remove_file_watch(path)

    def _sync_watcher_paths(self) -> None:
        desired_files = {
            str(path)
            for path, session_ids in self._file_to_sessions.items()
            if session_ids and path.exists()
        }
        desired_directories = {
            str(path) for path, session_ids in self._directory_to_sessions.items() if session_ids
        }
        current_files = set(self._watcher.files())
        current_directories = set(self._watcher.directories())

        stale_files = sorted(current_files - desired_files)
        stale_directories = sorted(current_directories - desired_directories)
        for path_text in stale_files:
            self._watcher.removePath(path_text)
        for path_text in stale_directories:
            self._watcher.removePath(path_text)

        stale_files_remaining = set(self._watcher.files()) - desired_files
        stale_directories_remaining = set(self._watcher.directories()) - desired_directories
        if stale_files_remaining or stale_directories_remaining:
            self._rebuild_watcher(desired_files, desired_directories)
            return

        for path_text in sorted(desired_directories - set(self._watcher.directories())):
            self._watcher.addPath(path_text)
        for path_text in sorted(desired_files - set(self._watcher.files())):
            self._watcher.addPath(path_text)

    def _rebuild_watcher(
        self,
        desired_files: set[str],
        desired_directories: set[str],
    ) -> None:
        self._watcher.fileChanged.disconnect(self._on_file_changed)
        self._watcher.directoryChanged.disconnect(self._on_directory_changed)
        self._watcher.deleteLater()
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_directory_changed)
        for path_text in sorted(desired_directories):
            self._watcher.addPath(path_text)
        for path_text in sorted(desired_files):
            self._watcher.addPath(path_text)

    def _process_pending_checks(self) -> None:
        session_ids = list(self._pending_session_ids)
        self._pending_session_ids.clear()
        for session_id in session_ids:
            session = self._sessions.get(session_id)
            if session is None:
                continue
            self.check_session_now(session)

    def _emit_if_changed(self, session: DocumentSession, result: SourceCheckResult) -> None:
        last_result = self._last_results.get(session.session_id)
        self._last_results[session.session_id] = result
        if (
            last_result is not None
            and last_result.status is result.status
            and last_result.current_fingerprint == result.current_fingerprint
            and last_result.expected_fingerprint == result.expected_fingerprint
            and last_result.error_message == result.error_message
            and last_result.path == result.path
        ):
            return
        self.source_status_changed.emit(session.session_id, result)

    def _on_application_state_changed(self, state: Qt.ApplicationState) -> None:
        if state is Qt.ApplicationState.ApplicationActive:
            self.check_all_now()
