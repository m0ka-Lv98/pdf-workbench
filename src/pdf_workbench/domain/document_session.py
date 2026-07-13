from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    size_bytes: int
    modified_time_ns: int

    @classmethod
    def from_path(cls, path: Path) -> FileFingerprint:
        stat_result = path.stat()
        return cls(
            size_bytes=stat_result.st_size,
            modified_time_ns=stat_result.st_mtime_ns,
        )


class SourceStatus(StrEnum):
    UNCHANGED = "unchanged"
    MISSING = "missing"
    MODIFIED = "modified"
    UNREADABLE = "unreadable"


@dataclass(slots=True)
class DocumentSession:
    """State associated with one open PDF document."""

    MIN_ZOOM_FACTOR = 0.25
    MAX_ZOOM_FACTOR = 5.0
    MAX_OPERATION_HISTORY = 100

    source_path: Path
    working_copy_path: Path
    workspace_directory: Path
    source_fingerprint: FileFingerprint
    current_page_index: int = 0
    zoom_factor: float = 1.0
    is_modified: bool = False
    operation_history: list[str] = field(default_factory=list)
    last_saved_at: datetime | None = None
    session_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    is_saving: bool = False
    recovered_from_interrupted_session: bool = False
    requires_save_as: bool = False
    recovery_source_status: SourceStatus | None = None
    source_status: SourceStatus = SourceStatus.UNCHANGED
    source_status_checked_at: datetime | None = None
    source_change_detected_at: datetime | None = None
    source_status_revision: int = 0

    def __post_init__(self) -> None:
        self.source_path = self.source_path.expanduser().resolve()
        self.working_copy_path = self.working_copy_path.expanduser().resolve()
        self.workspace_directory = self.workspace_directory.expanduser().resolve()
        if self.source_path.suffix.lower() != ".pdf":
            raise ValueError("source_path must refer to a PDF file")
        if self.working_copy_path.suffix.lower() != ".pdf":
            raise ValueError("working_copy_path must refer to a PDF file")
        if not isfinite(self.zoom_factor):
            raise ValueError("zoom_factor must be finite")
        if not self.MIN_ZOOM_FACTOR <= self.zoom_factor <= self.MAX_ZOOM_FACTOR:
            raise ValueError("zoom_factor must be positive")
        if self.current_page_index < 0:
            raise ValueError("current_page_index must be non-negative")
        if self.working_copy_path.parent != self.workspace_directory:
            raise ValueError("working_copy_path must live inside workspace_directory")

    @property
    def display_path(self) -> Path:
        return self.source_path

    @property
    def document_path(self) -> Path:
        return self.working_copy_path

    def mark_modified(self, description: str) -> None:
        self.is_modified = True
        self.updated_at = datetime.now(UTC)
        self.operation_history.append(description)
        self.operation_history = self.operation_history[-self.MAX_OPERATION_HISTORY :]

    def mark_saved(
        self,
        source_path: Path,
        fingerprint: FileFingerprint,
        saved_at: datetime,
    ) -> None:
        self.source_path = source_path
        self.source_fingerprint = fingerprint
        self.last_saved_at = saved_at
        self.updated_at = saved_at
        self.is_modified = False
        self.recovered_from_interrupted_session = False
        self.requires_save_as = False
        self.recovery_source_status = None
        self.source_status = SourceStatus.UNCHANGED
        self.source_status_checked_at = saved_at
        self.source_change_detected_at = None

    def set_navigation_state(self, *, page_index: int, zoom_factor: float) -> None:
        if page_index < 0:
            raise ValueError("page_index must be non-negative")
        if not isfinite(zoom_factor):
            raise ValueError("zoom_factor must be finite")
        if not self.MIN_ZOOM_FACTOR <= zoom_factor <= self.MAX_ZOOM_FACTOR:
            raise ValueError("zoom_factor is out of range")
        self.current_page_index = page_index
        self.zoom_factor = zoom_factor
        self.updated_at = datetime.now(UTC)

    def mark_recovered(self, source_status: SourceStatus) -> None:
        self.recovered_from_interrupted_session = True
        self.recovery_source_status = source_status
        self.source_status = source_status
        now = datetime.now(UTC)
        self.source_status_checked_at = now
        self.source_change_detected_at = None if source_status is SourceStatus.UNCHANGED else now
        self.requires_save_as = source_status is not SourceStatus.UNCHANGED
        if source_status is not SourceStatus.UNCHANGED:
            self.source_status_revision += 1

    def clear_recovery_state(self) -> None:
        self.recovered_from_interrupted_session = False
        self.recovery_source_status = None
        self.requires_save_as = self.source_status is not SourceStatus.UNCHANGED

    def apply_source_check(self, result: object) -> bool:
        from pdf_workbench.services.source_change_monitor import SourceCheckResult

        if not isinstance(result, SourceCheckResult):
            raise TypeError("result must be SourceCheckResult")

        fingerprint_changed = result.current_fingerprint != self.source_fingerprint
        status_changed = result.status is not self.source_status
        previous_status = self.source_status
        previous_checked_at = self.source_status_checked_at
        previous_requires_save_as = self.requires_save_as

        self.source_status = result.status
        self.source_status_checked_at = result.checked_at
        if result.status is SourceStatus.UNCHANGED:
            self.source_change_detected_at = None
        elif self.source_change_detected_at is None or previous_status is SourceStatus.UNCHANGED:
            self.source_change_detected_at = result.checked_at

        self.requires_save_as = bool(
            result.status is not SourceStatus.UNCHANGED
            or (
                self.recovered_from_interrupted_session
                and self.recovery_source_status is not None
                and self.recovery_source_status is not SourceStatus.UNCHANGED
            )
        )

        if status_changed or fingerprint_changed:
            self.source_status_revision += 1

        return bool(
            status_changed
            or fingerprint_changed
            or previous_requires_save_as != self.requires_save_as
            or previous_checked_at is None
        )
