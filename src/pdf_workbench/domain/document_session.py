from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class DocumentSession:
    """State associated with one open PDF document."""

    source_path: Path
    current_page_index: int = 0
    zoom_factor: float = 1.0
    is_modified: bool = False
    operation_history: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.source_path = self.source_path.expanduser().resolve()
        if self.source_path.suffix.lower() != ".pdf":
            raise ValueError("source_path must refer to a PDF file")
        if self.zoom_factor <= 0:
            raise ValueError("zoom_factor must be positive")

    def mark_modified(self, description: str) -> None:
        self.is_modified = True
        self.operation_history.append(description)
