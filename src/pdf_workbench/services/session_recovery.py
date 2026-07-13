from __future__ import annotations

import json
import logging
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pdf_workbench.domain.document_session import (
    DocumentSession,
    FileFingerprint,
    SourceStatus,
)
from pdf_workbench.services.pdf_document_validator import (
    PdfDocumentValidationError,
    PdfDocumentValidator,
)
from pdf_workbench.services.session_workspace import (
    SessionWorkspaceLease,
    SessionWorkspaceManager,
    WorkspaceLockActiveError,
    WorkspaceLockError,
)

logger = logging.getLogger(__name__)


class RecoveryValidationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"


class RecoveryMetadataError(RuntimeError):
    """Raised when recovery metadata cannot be parsed or written safely."""


@dataclass(frozen=True, slots=True)
class RecoveryMetadata:
    schema_version: int
    session_id: str
    source_path: Path
    working_copy_name: str
    created_at: datetime
    updated_at: datetime
    last_saved_at: datetime | None
    source_fingerprint: FileFingerprint
    current_page_index: int
    zoom_factor: float
    is_modified: bool
    operation_history: list[str]


@dataclass(slots=True)
class RecoveryCandidate:
    workspace_directory: Path
    working_copy_path: Path
    metadata: RecoveryMetadata
    source_status: SourceStatus
    validation_status: RecoveryValidationStatus
    recoverable: bool
    discardable: bool
    error_message: str | None
    lease: SessionWorkspaceLease | None = None
    working_copy_size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class RecoveryScanResult:
    candidates: list[RecoveryCandidate]


class SessionRecoveryService:
    SCHEMA_VERSION = 1
    MAX_JSON_BYTES = 1024 * 1024
    DISCARDING_MARKER_NAME = ".discarding"

    def __init__(
        self,
        workspace_manager: SessionWorkspaceManager,
        *,
        validator: PdfDocumentValidator | None = None,
    ) -> None:
        self._workspace_manager = workspace_manager
        self._validator = validator if validator is not None else PdfDocumentValidator()

    def write_metadata(self, session: DocumentSession) -> None:
        metadata_path = session.workspace_directory / self._workspace_manager.METADATA_NAME
        temp_path: Path | None = None
        payload = self._serialize_session(session)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(encoded) > self.MAX_JSON_BYTES:
            raise RecoveryMetadataError("セッションメタデータが大きすぎます")
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".session.",
                suffix=".json.tmp",
                dir=session.workspace_directory,
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, metadata_path)
            self._fsync_parent_directory(session.workspace_directory)
        except OSError as exc:
            raise RecoveryMetadataError("セッションメタデータの保存に失敗しました") from exc
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    logger.warning("Failed to remove temp session metadata: %s", temp_path)

    def scan_candidates(self) -> RecoveryScanResult:
        candidates: list[RecoveryCandidate] = []
        for child in sorted(self._workspace_manager.sessions_root.iterdir()):
            if child.name.startswith("."):
                continue
            candidate = self._scan_workspace(child)
            if candidate is not None:
                candidates.append(candidate)
        return RecoveryScanResult(candidates=candidates)

    def restore_candidate(self, candidate: RecoveryCandidate) -> DocumentSession:
        if not candidate.recoverable:
            raise RecoveryMetadataError("この候補は復元できません")
        if candidate.lease is None:
            raise RecoveryMetadataError("復元候補のロックが保持されていません")
        session = DocumentSession(
            source_path=candidate.metadata.source_path,
            working_copy_path=candidate.working_copy_path,
            workspace_directory=candidate.workspace_directory,
            source_fingerprint=candidate.metadata.source_fingerprint,
            current_page_index=candidate.metadata.current_page_index,
            zoom_factor=candidate.metadata.zoom_factor,
            is_modified=candidate.metadata.is_modified,
            operation_history=list(candidate.metadata.operation_history),
            last_saved_at=candidate.metadata.last_saved_at,
            session_id=candidate.metadata.session_id,
            created_at=candidate.metadata.created_at,
            updated_at=datetime.now(UTC),
        )
        session.mark_recovered(candidate.source_status)
        self.write_metadata(session)
        # adopt後はworkspace managerが唯一のlease ownerになる。
        self._workspace_manager.adopt_session_lock(session.session_id, candidate.lease)
        candidate.lease = None
        return session

    def discard_candidate(self, candidate: RecoveryCandidate) -> None:
        if not candidate.discardable:
            raise RecoveryMetadataError("この候補は安全に破棄できません")
        resolved_workspace = self._validate_workspace_identity(candidate.workspace_directory)
        lease = candidate.lease
        if lease is None:
            session_id = resolved_workspace.name
            try:
                lease = self._workspace_manager.acquire_workspace_lock(
                    session_id,
                    resolved_workspace,
                )
            except WorkspaceLockActiveError as exc:
                raise RecoveryMetadataError(
                    "別のプロセスがこの候補を使用中のため破棄できません",
                ) from exc
            except WorkspaceLockError as exc:
                raise RecoveryMetadataError("復旧候補のロックを取得できませんでした") from exc
            candidate.lease = lease
        marker_path = resolved_workspace / self.DISCARDING_MARKER_NAME
        try:
            self._create_discarding_marker(marker_path)
            self._remove_workspace_contents_for_discard(
                resolved_workspace,
                keep_names={self._workspace_manager.LOCK_NAME, self.DISCARDING_MARKER_NAME},
            )
        except OSError as exc:
            raise RecoveryMetadataError("復旧候補の破棄に失敗しました") from exc
        finally:
            if candidate.lease is not None and not candidate.lease.released:
                candidate.lease.release()
            candidate.lease = None
        try:
            self._remove_discarding_lock_and_directory(resolved_workspace, marker_path)
        except OSError as exc:
            raise RecoveryMetadataError("復旧候補の破棄に失敗しました") from exc

    def release_candidate(self, candidate: RecoveryCandidate) -> None:
        if candidate.lease is None:
            return
        candidate.lease.release()
        candidate.lease = None

    def _scan_workspace(self, workspace_directory: Path) -> RecoveryCandidate | None:
        marker_path = workspace_directory / self.DISCARDING_MARKER_NAME
        if marker_path.exists():
            self._resume_discarding_workspace(workspace_directory)
            return None
        try:
            resolved_workspace = self._validate_workspace_identity(workspace_directory)
        except RecoveryMetadataError as exc:
            logger.warning("Found invalid workspace candidate: %s (%s)", workspace_directory, exc)
            return self._invalid_candidate(
                workspace_directory,
                str(exc),
                discardable=self._workspace_is_discardable(workspace_directory),
            )
        session_id = resolved_workspace.name
        try:
            lease = self._workspace_manager.acquire_workspace_lock(session_id, resolved_workspace)
        except WorkspaceLockActiveError:
            return None
        except WorkspaceLockError as exc:
            logger.warning("Failed to inspect recovery candidate: %s (%s)", resolved_workspace, exc)
            return None

        try:
            metadata, working_copy_path, working_copy_size = self._validate_workspace_contents(
                resolved_workspace,
                session_id,
            )
            source_status = self._detect_source_status(metadata)
            return RecoveryCandidate(
                workspace_directory=resolved_workspace,
                working_copy_path=working_copy_path,
                metadata=metadata,
                source_status=source_status,
                validation_status=RecoveryValidationStatus.VALID,
                recoverable=True,
                discardable=True,
                error_message=None,
                lease=lease,
                working_copy_size_bytes=working_copy_size,
            )
        except (RecoveryMetadataError, PdfDocumentValidationError) as exc:
            metadata = self._best_effort_metadata(resolved_workspace, session_id)
            return RecoveryCandidate(
                workspace_directory=resolved_workspace,
                working_copy_path=resolved_workspace / self._workspace_manager.WORKING_COPY_NAME,
                metadata=metadata,
                source_status=SourceStatus.UNREADABLE,
                validation_status=RecoveryValidationStatus.INVALID,
                recoverable=False,
                discardable=True,
                error_message=str(exc),
                lease=lease,
                working_copy_size_bytes=self._safe_size(
                    resolved_workspace / self._workspace_manager.WORKING_COPY_NAME
                ),
            )

    def _validate_workspace_identity(self, workspace_directory: Path) -> Path:
        if workspace_directory.is_symlink():
            raise RecoveryMetadataError("workspaceがsymlinkです")
        resolved_workspace = workspace_directory.expanduser().resolve()
        if resolved_workspace.parent != self._workspace_manager.sessions_root:
            raise RecoveryMetadataError("sessions root直下ではありません")
        if not resolved_workspace.is_dir():
            raise RecoveryMetadataError("workspaceがdirectoryではありません")
        if len(resolved_workspace.name) != 32 or not all(
            character in "0123456789abcdef" for character in resolved_workspace.name
        ):
            raise RecoveryMetadataError("session ID形式が不正です")
        lock_path = resolved_workspace / self._workspace_manager.LOCK_NAME
        if not lock_path.exists() or not lock_path.is_file():
            raise RecoveryMetadataError("session lockが見つかりません")
        if lock_path.is_symlink():
            raise RecoveryMetadataError("session lockがsymlinkです")
        return resolved_workspace

    def _validate_workspace_contents(
        self,
        workspace_directory: Path,
        session_id: str,
    ) -> tuple[RecoveryMetadata, Path, int]:
        working_copy_path = workspace_directory / self._workspace_manager.WORKING_COPY_NAME
        metadata_path = workspace_directory / self._workspace_manager.METADATA_NAME
        if not working_copy_path.exists() or not working_copy_path.is_file():
            raise RecoveryMetadataError("working PDFが見つかりません")
        if working_copy_path.is_symlink():
            raise RecoveryMetadataError("working PDFがsymlinkです")
        if not metadata_path.exists() or not metadata_path.is_file():
            raise RecoveryMetadataError("metadataが見つかりません")
        if metadata_path.is_symlink():
            raise RecoveryMetadataError("metadataがsymlinkです")
        metadata = self._read_metadata(workspace_directory, session_id)
        working_copy_size = working_copy_path.stat().st_size
        if working_copy_size <= 0:
            raise RecoveryMetadataError("working PDFが空です")
        self._validator.validate(str(working_copy_path))
        return metadata, working_copy_path, working_copy_size

    def _invalid_candidate(
        self,
        workspace_directory: Path,
        error_message: str,
        *,
        discardable: bool,
    ) -> RecoveryCandidate:
        session_id = workspace_directory.name
        resolved_workspace = workspace_directory.expanduser().resolve(strict=False)
        return RecoveryCandidate(
            workspace_directory=resolved_workspace,
            working_copy_path=resolved_workspace / self._workspace_manager.WORKING_COPY_NAME,
            metadata=self._best_effort_metadata(resolved_workspace, session_id),
            source_status=SourceStatus.UNREADABLE,
            validation_status=RecoveryValidationStatus.INVALID,
            recoverable=False,
            discardable=discardable,
            error_message=error_message,
            lease=None,
            working_copy_size_bytes=self._safe_size(
                resolved_workspace / self._workspace_manager.WORKING_COPY_NAME
            ),
        )

    def _read_metadata(self, workspace_directory: Path, session_id: str) -> RecoveryMetadata:
        metadata_path = workspace_directory / self._workspace_manager.METADATA_NAME
        encoded = metadata_path.read_bytes()
        if len(encoded) > self.MAX_JSON_BYTES:
            raise RecoveryMetadataError("metadataが大きすぎます")
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryMetadataError("metadataが破損しています") from exc
        if not isinstance(payload, dict):
            raise RecoveryMetadataError("metadataの形式が不正です")
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int or schema_version != self.SCHEMA_VERSION:
            raise RecoveryMetadataError("schema versionに対応していません")
        metadata_session_id = payload.get("session_id")
        if metadata_session_id != session_id:
            raise RecoveryMetadataError("session IDが一致しません")
        working_copy_name = payload.get("working_copy_name")
        if working_copy_name != self._workspace_manager.WORKING_COPY_NAME:
            raise RecoveryMetadataError("working copy名が不正です")
        source_path_raw = payload.get("source_path")
        if not isinstance(source_path_raw, str):
            raise RecoveryMetadataError("source pathが不正です")
        source_path = Path(source_path_raw).expanduser()
        if not source_path.is_absolute() or source_path.suffix.lower() != ".pdf":
            raise RecoveryMetadataError("source pathが不正です")
        source_fingerprint_payload = payload.get("source_fingerprint")
        if not isinstance(source_fingerprint_payload, dict):
            raise RecoveryMetadataError("fingerprintが不正です")
        size_bytes = source_fingerprint_payload.get("size_bytes")
        modified_time_ns = source_fingerprint_payload.get("modified_time_ns")
        if type(size_bytes) is not int or size_bytes < 0:
            raise RecoveryMetadataError("fingerprint sizeが不正です")
        if type(modified_time_ns) is not int or modified_time_ns < 0:
            raise RecoveryMetadataError("fingerprint mtimeが不正です")
        current_page_index = payload.get("current_page_index")
        if type(current_page_index) is not int or current_page_index < 0:
            raise RecoveryMetadataError("page indexが不正です")
        zoom_factor = payload.get("zoom_factor")
        if not isinstance(zoom_factor, (float, int)) or not math.isfinite(float(zoom_factor)):
            raise RecoveryMetadataError("zoomが不正です")
        zoom = float(zoom_factor)
        if not DocumentSession.MIN_ZOOM_FACTOR <= zoom <= DocumentSession.MAX_ZOOM_FACTOR:
            raise RecoveryMetadataError("zoomが範囲外です")
        is_modified = payload.get("is_modified")
        if not isinstance(is_modified, bool):
            raise RecoveryMetadataError("is_modifiedが不正です")
        operation_history_payload = payload.get("operation_history")
        if not isinstance(operation_history_payload, list) or any(
            not isinstance(item, str) for item in operation_history_payload
        ):
            raise RecoveryMetadataError("operation historyが不正です")
        operation_history = operation_history_payload[-DocumentSession.MAX_OPERATION_HISTORY :]
        created_at = self._parse_datetime(payload.get("created_at"))
        updated_at = self._parse_datetime(payload.get("updated_at"))
        last_saved_at_raw = payload.get("last_saved_at")
        last_saved_at = (
            None if last_saved_at_raw is None else self._parse_datetime(last_saved_at_raw)
        )
        return RecoveryMetadata(
            schema_version=self.SCHEMA_VERSION,
            session_id=session_id,
            source_path=source_path.resolve(strict=False),
            working_copy_name=working_copy_name,
            created_at=created_at,
            updated_at=updated_at,
            last_saved_at=last_saved_at,
            source_fingerprint=FileFingerprint(
                size_bytes=size_bytes,
                modified_time_ns=modified_time_ns,
            ),
            current_page_index=current_page_index,
            zoom_factor=zoom,
            is_modified=is_modified,
            operation_history=operation_history,
        )

    def _best_effort_metadata(self, workspace_directory: Path, session_id: str) -> RecoveryMetadata:
        now = datetime.now(UTC)
        return RecoveryMetadata(
            schema_version=self.SCHEMA_VERSION,
            session_id=session_id,
            source_path=(workspace_directory / "unknown.pdf").resolve(strict=False),
            working_copy_name=self._workspace_manager.WORKING_COPY_NAME,
            created_at=now,
            updated_at=now,
            last_saved_at=None,
            source_fingerprint=FileFingerprint(size_bytes=0, modified_time_ns=0),
            current_page_index=0,
            zoom_factor=1.0,
            is_modified=True,
            operation_history=[],
        )

    def _serialize_session(self, session: DocumentSession) -> dict[str, object]:
        updated_at = datetime.now(UTC)
        session.updated_at = updated_at
        return {
            "schema_version": self.SCHEMA_VERSION,
            "session_id": session.session_id,
            "source_path": str(session.source_path),
            "working_copy_name": self._workspace_manager.WORKING_COPY_NAME,
            "created_at": session.created_at.isoformat(),
            "updated_at": updated_at.isoformat(),
            "last_saved_at": (
                None if session.last_saved_at is None else session.last_saved_at.isoformat()
            ),
            "source_fingerprint": {
                "size_bytes": session.source_fingerprint.size_bytes,
                "modified_time_ns": session.source_fingerprint.modified_time_ns,
            },
            "current_page_index": session.current_page_index,
            "zoom_factor": session.zoom_factor,
            "is_modified": session.is_modified,
            "operation_history": session.operation_history[
                -DocumentSession.MAX_OPERATION_HISTORY :
            ],
        }

    @staticmethod
    def _parse_datetime(value: object) -> datetime:
        if not isinstance(value, str):
            raise RecoveryMetadataError("datetimeが不正です")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise RecoveryMetadataError("datetimeが不正です") from exc
        if parsed.tzinfo is None:
            raise RecoveryMetadataError("datetimeにtimezoneがありません")
        return parsed.astimezone(UTC)

    @staticmethod
    def _safe_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _workspace_is_discardable(self, workspace_directory: Path) -> bool:
        try:
            self._validate_workspace_identity(workspace_directory)
        except RecoveryMetadataError:
            return False
        return True

    @staticmethod
    def _create_discarding_marker(marker_path: Path) -> None:
        with open(marker_path, "xb") as handle:
            handle.write(b"discarding\n")

    def _remove_workspace_contents_for_discard(
        self,
        workspace_directory: Path,
        *,
        keep_names: set[str],
    ) -> None:
        for child in workspace_directory.iterdir():
            if child.name in keep_names:
                continue
            if child.is_symlink():
                child.unlink()
                continue
            if child.is_dir():
                shutil.rmtree(child)
                continue
            child.unlink()

    def _remove_discarding_lock_and_directory(
        self,
        workspace_directory: Path,
        marker_path: Path,
    ) -> None:
        lock_path = workspace_directory / self._workspace_manager.LOCK_NAME
        try:
            if marker_path.exists():
                marker_path.unlink()
        except FileNotFoundError:
            pass
        try:
            if lock_path.exists():
                lock_path.unlink()
        except FileNotFoundError:
            pass
        try:
            workspace_directory.rmdir()
        except FileNotFoundError:
            return

    def _resume_discarding_workspace(self, workspace_directory: Path) -> None:
        if not self._workspace_is_discardable(workspace_directory):
            logger.warning("Ignoring unsafe discarding workspace: %s", workspace_directory)
            return
        resolved_workspace = workspace_directory.expanduser().resolve()
        session_id = resolved_workspace.name
        try:
            lease = self._workspace_manager.acquire_workspace_lock(session_id, resolved_workspace)
        except WorkspaceLockActiveError:
            return
        except WorkspaceLockError as exc:
            logger.warning(
                "Failed to resume workspace discard: %s (%s)",
                resolved_workspace,
                exc,
            )
            return
        marker_path = resolved_workspace / self.DISCARDING_MARKER_NAME
        try:
            self._remove_workspace_contents_for_discard(
                resolved_workspace,
                keep_names={self._workspace_manager.LOCK_NAME, self.DISCARDING_MARKER_NAME},
            )
        except OSError:
            logger.exception("Failed to remove stale discarding workspace: %s", resolved_workspace)
            return
        finally:
            if not lease.released:
                lease.release()
        try:
            self._remove_discarding_lock_and_directory(resolved_workspace, marker_path)
        except FileNotFoundError:
            return
        except OSError:
            logger.exception("Failed to finalize stale workspace discard: %s", resolved_workspace)

    @staticmethod
    def _fsync_parent_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            directory_handle = os.open(directory, os.O_RDONLY)
        except OSError as exc:
            logger.warning("Failed to open session directory for fsync: %s (%s)", directory, exc)
            return
        try:
            os.fsync(directory_handle)
        except OSError as exc:
            logger.warning(
                "Failed to fsync session directory after metadata replace: %s (%s)",
                directory,
                exc,
            )
        finally:
            os.close(directory_handle)

    @staticmethod
    def _detect_source_status(metadata: RecoveryMetadata) -> SourceStatus:
        try:
            stat_result = metadata.source_path.stat()
        except FileNotFoundError:
            return SourceStatus.MISSING
        except OSError:
            return SourceStatus.UNREADABLE
        if (
            stat_result.st_size == metadata.source_fingerprint.size_bytes
            and stat_result.st_mtime_ns == metadata.source_fingerprint.modified_time_ns
        ):
            return SourceStatus.UNCHANGED
        return SourceStatus.MODIFIED
