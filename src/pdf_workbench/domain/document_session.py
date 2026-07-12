from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
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


@dataclass(slots=True)
class DocumentSession:
    """State associated with one open PDF document."""

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
    is_saving: bool = False

    def __post_init__(self) -> None:
        self.source_path = self.source_path.expanduser().resolve()
        self.working_copy_path = self.working_copy_path.expanduser().resolve()
        self.workspace_directory = self.workspace_directory.expanduser().resolve()
        if self.source_path.suffix.lower() != ".pdf":
            raise ValueError("source_path must refer to a PDF file")
        if self.working_copy_path.suffix.lower() != ".pdf":
            raise ValueError("working_copy_path must refer to a PDF file")
        if self.zoom_factor <= 0:
            raise ValueError("zoom_factor must be positive")
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
        self.operation_history.append(description)

    def mark_saved(
        self,
        source_path: Path,
        fingerprint: FileFingerprint,
        saved_at: datetime,
    ) -> None:
        self.source_path = source_path.expanduser().resolve()
        self.source_fingerprint = fingerprint
        self.last_saved_at = saved_at
        self.is_modified = False
