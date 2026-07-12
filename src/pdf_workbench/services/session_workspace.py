from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from uuid import uuid4

from pdf_workbench.core.app_paths import ensure_app_directories
from pdf_workbench.domain.document_session import DocumentSession, FileFingerprint

logger = logging.getLogger(__name__)


class WorkspaceCreationError(RuntimeError):
    """Raised when a working-copy session cannot be created."""


class SessionWorkspaceManager:
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

    @property
    def sessions_root(self) -> Path:
        return self._sessions_root

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
        working_copy_path = workspace_directory / "working.pdf"
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
            return DocumentSession(
                source_path=resolved_source,
                working_copy_path=working_copy_path,
                workspace_directory=workspace_directory,
                source_fingerprint=fingerprint,
                session_id=session_id,
            )
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
        self._cleanup_workspace_directory(session.workspace_directory)

    def cleanup_sessions(self, sessions: list[DocumentSession]) -> None:
        for session in sessions:
            self.cleanup_session(session)

    def _cleanup_workspace_directory(self, workspace_directory: Path) -> None:
        resolved_directory = workspace_directory.expanduser().resolve()
        if not resolved_directory.exists():
            return
        if resolved_directory.parent != self._sessions_root:
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
