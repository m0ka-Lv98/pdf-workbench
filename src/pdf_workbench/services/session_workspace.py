from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from io import BufferedRandom
from pathlib import Path
from uuid import uuid4

from pdf_workbench.core.app_paths import ensure_app_directories
from pdf_workbench.domain.document_session import DocumentSession, FileFingerprint

logger = logging.getLogger(__name__)


class WorkspaceCreationError(RuntimeError):
    """Raised when a working-copy session cannot be created."""


class WorkspaceLockError(RuntimeError):
    """Raised when a session lock cannot be acquired or released safely."""


class WorkspaceLockActiveError(WorkspaceLockError):
    """Raised when another process is already holding the workspace lock."""


@dataclass(slots=True)
class SessionWorkspaceLease:
    session_id: str
    lock_path: Path
    handle: BufferedRandom
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.released = True


class SessionWorkspaceManager:
    WORKING_COPY_NAME = "working.pdf"
    METADATA_NAME = "session.json"
    LOCK_NAME = "session.lock"

    def __init__(
        self,
        sessions_root: Path | None = None,
        *,
        cleanup_attempts: int = 10,
        cleanup_delay_seconds: float = 0.05,
    ) -> None:
        if sessions_root is None:
            sessions_root = ensure_app_directories().cache_dir / "sessions"
        self._sessions_root = sessions_root.expanduser().resolve()
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        self._cleanup_attempts = cleanup_attempts
        self._cleanup_delay_seconds = cleanup_delay_seconds
        self._active_leases: dict[str, SessionWorkspaceLease] = {}

    @property
    def sessions_root(self) -> Path:
        return self._sessions_root

    def contains_managed_path(self, path: Path) -> bool:
        resolved_path = path.expanduser().resolve()
        try:
            return resolved_path.is_relative_to(self._sessions_root)
        except ValueError:
            return False

    def create_session(self, source_path: Path) -> DocumentSession:
        resolved_source = source_path.expanduser().resolve()
        if not resolved_source.exists():
            raise WorkspaceCreationError("PDFファイルが見つかりません")
        if not resolved_source.is_file():
            raise WorkspaceCreationError("PDFファイルを指定してください")
        if resolved_source.suffix.lower() != ".pdf":
            raise WorkspaceCreationError("PDFファイルのみ開けます")

        fingerprint = FileFingerprint.from_path(resolved_source)
        session_id = uuid4().hex
        workspace_directory = self._sessions_root / session_id
        working_copy_path = workspace_directory / self.WORKING_COPY_NAME
        logger.info(
            "Creating session workspace: session_id=%s source=%s working_copy=%s",
            session_id,
            resolved_source,
            working_copy_path,
        )

        try:
            workspace_directory.mkdir(parents=True, exist_ok=False)
            shutil.copy2(resolved_source, working_copy_path)
            copied_fingerprint = FileFingerprint.from_path(working_copy_path)
            if copied_fingerprint.size_bytes != fingerprint.size_bytes:
                raise WorkspaceCreationError("作業コピーのサイズが一致しません")
            lease = self.acquire_workspace_lock(session_id, workspace_directory)
            session = DocumentSession(
                source_path=resolved_source,
                working_copy_path=working_copy_path,
                workspace_directory=workspace_directory,
                source_fingerprint=fingerprint,
                session_id=session_id,
            )
            self.adopt_session_lock(session_id, lease)
            return session
        except Exception as exc:
            self._cleanup_workspace_directory(workspace_directory)
            if isinstance(exc, WorkspaceCreationError):
                raise
            raise WorkspaceCreationError("作業コピーの作成に失敗しました") from exc

    def cleanup_session(self, session: DocumentSession) -> None:
        logger.info(
            "Cleaning session workspace: session_id=%s workspace=%s",
            session.session_id,
            session.workspace_directory,
        )
        self.release_session_lock(session.session_id)
        self._cleanup_workspace_directory(session.workspace_directory)

    def cleanup_sessions(self, sessions: list[DocumentSession]) -> None:
        for session in sessions:
            self.cleanup_session(session)

    def cleanup_workspace_directory(self, workspace_directory: Path) -> None:
        self._cleanup_workspace_directory(workspace_directory)

    def acquire_workspace_lock(
        self,
        session_id: str,
        workspace_directory: Path,
    ) -> SessionWorkspaceLease:
        resolved_workspace = workspace_directory.expanduser().resolve()
        lock_path = resolved_workspace / self.LOCK_NAME
        try:
            lock_path.touch(exist_ok=True)
            handle = lock_path.open("a+b")
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if lock_path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if "handle" in locals():
                handle.close()
            if os.name == "nt" and getattr(exc, "winerror", None) in {33, 36}:
                raise WorkspaceLockActiveError("別のプロセスがセッションを使用中です") from exc
            if os.name != "nt" and exc.errno in {11, 35}:
                raise WorkspaceLockActiveError("別のプロセスがセッションを使用中です") from exc
            raise WorkspaceLockError("セッションロックを取得できませんでした") from exc
        return SessionWorkspaceLease(
            session_id=session_id,
            lock_path=lock_path,
            handle=handle,
        )

    def adopt_session_lock(self, session_id: str, lease: SessionWorkspaceLease) -> None:
        previous = self._active_leases.pop(session_id, None)
        if previous is not None:
            previous.release()
        self._active_leases[session_id] = lease

    def release_session_lock(self, session_id: str) -> None:
        lease = self._active_leases.pop(session_id, None)
        if lease is None:
            return
        try:
            lease.release()
        except OSError as exc:
            logger.warning("Failed to release workspace lock: session_id=%s (%s)", session_id, exc)

    def release_all_locks(self) -> None:
        for session_id in list(self._active_leases):
            self.release_session_lock(session_id)

    def _cleanup_workspace_directory(self, workspace_directory: Path) -> None:
        resolved_directory = workspace_directory.expanduser().resolve()
        if not resolved_directory.exists():
            return
        if not self.contains_managed_path(resolved_directory) or (
            resolved_directory.parent != self._sessions_root
        ):
            logger.warning(
                "Refusing to clean unexpected workspace directory: %s",
                resolved_directory,
            )
            return
        for attempt in range(1, self._cleanup_attempts + 1):
            try:
                shutil.rmtree(resolved_directory)
                logger.info("Removed session workspace: %s", resolved_directory)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt == self._cleanup_attempts:
                    logger.exception("Failed to remove session workspace: %s", resolved_directory)
                    return
                time.sleep(self._cleanup_delay_seconds)
