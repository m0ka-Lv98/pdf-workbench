from __future__ import annotations

import argparse
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from pdf_workbench.domain.document_session import FileFingerprint, SourceStatus
from pdf_workbench.services.session_recovery import (
    RecoveryCandidate,
    RecoveryMetadata,
    RecoveryValidationStatus,
)
from pdf_workbench.ui.dialogs.recovery_dialog import RecoveryDialog
from pdf_workbench.ui.theme import ColorScheme, apply_application_theme


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture recovery dialog review artifacts")
    parser.add_argument("--output-png", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--window-size", type=str, required=True)
    parser.add_argument(
        "--color-scheme",
        choices=[ColorScheme.LIGHT.value, ColorScheme.DARK.value],
        required=True,
    )
    return parser


def _parse_size(value: str) -> tuple[int, int]:
    width_text, separator, height_text = value.partition("x")
    if separator != "x":
        raise ValueError("window size must use WIDTHxHEIGHT")
    return int(width_text), int(height_text)


def _geometry(widget: QWidget | None) -> list[int]:
    if widget is None:
        return [0, 0, 0, 0]
    rect = widget.geometry()
    return [rect.x(), rect.y(), rect.width(), rect.height()]


def _build_candidates(root: Path) -> list[RecoveryCandidate]:
    now = datetime.now(UTC)

    def candidate(
        name: str,
        *,
        recoverable: bool,
        discardable: bool,
        error_message: str | None = None,
    ) -> RecoveryCandidate:
        workspace_directory = root / name
        workspace_directory.mkdir()
        metadata = RecoveryMetadata(
            schema_version=1,
            session_id=name,
            source_path=(root / f"{name}.pdf").resolve(),
            working_copy_name="working.pdf",
            created_at=now,
            updated_at=now,
            last_saved_at=None,
            source_fingerprint=FileFingerprint(size_bytes=1024, modified_time_ns=1),
            current_page_index=1,
            zoom_factor=1.0,
            is_modified=True,
            operation_history=["edit"],
        )
        return RecoveryCandidate(
            workspace_directory=workspace_directory,
            working_copy_path=workspace_directory / "working.pdf",
            metadata=metadata,
            source_status=SourceStatus.MODIFIED,
            validation_status=(
                RecoveryValidationStatus.VALID if recoverable else RecoveryValidationStatus.INVALID
            ),
            recoverable=recoverable,
            discardable=discardable,
            error_message=error_message,
            working_copy_size_bytes=1024 * 64,
        )

    return [
        candidate("a" * 32, recoverable=True, discardable=True),
        candidate(
            "b" * 32,
            recoverable=False,
            discardable=True,
            error_message="復元不可・破棄可能",
        ),
        candidate(
            "c" * 32,
            recoverable=False,
            discardable=False,
            error_message="安全のため自動削除できません",
        ),
    ]


def main() -> int:
    args = build_parser().parse_args()
    width, height = _parse_size(args.window_size)

    app = QApplication.instance() or QApplication([])
    apply_application_theme(app, ColorScheme(args.color_scheme))

    host = QWidget()
    host.setWindowTitle("Recovery Dialog Review")
    host.resize(width, height)
    host.show()

    with tempfile.TemporaryDirectory() as temp_directory:
        candidates = _build_candidates(Path(temp_directory))
        dialog = RecoveryDialog(candidates, host)
        dialog.setModal(False)
        dialog.show()
        dialog.activateWindow()

        for _ in range(4):
            app.processEvents()

        dialog.move(
            max(0, (host.width() - dialog.width()) // 2),
            max(0, (host.height() - dialog.height()) // 2),
        )
        invalid_discardable_item = dialog._tree.topLevelItem(1)
        invalid_discardable_item.setCheckState(0, Qt.CheckState.Checked)

        for _ in range(4):
            app.processEvents()

        args.output_png.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        host.grab().save(str(args.output_png))

        screen = dialog.screen() or QApplication.primaryScreen()
        available = None if screen is None else screen.availableGeometry()
        dialog_geometry = _geometry(dialog)
        available_geometry = (
            [0, 0, 0, 0]
            if available is None
            else [available.x(), available.y(), available.width(), available.height()]
        )
        fits_in_screen = (
            available is not None
            and dialog_geometry[0] >= available.x()
            and dialog_geometry[1] >= available.y()
            and dialog_geometry[0] + dialog_geometry[2] <= available.x() + available.width()
            and dialog_geometry[1] + dialog_geometry[3] <= available.y() + available.height()
        )
        payload = {
            "requested_window_size": [width, height],
            "dialog_geometry": dialog_geometry,
            "available_screen_geometry": available_geometry,
            "tree_geometry": _geometry(dialog._tree),
            "button_row_geometry": _geometry(dialog._button_row_widget),
            "candidate_count": len(candidates),
            "recoverable_count": sum(1 for candidate in candidates if candidate.recoverable),
            "discardable_count": sum(1 for candidate in candidates if candidate.discardable),
            "recover_enabled": dialog._recover_button.isEnabled(),
            "discard_enabled": dialog._discard_button.isEnabled(),
            "later_visible": dialog._later_button.isVisible(),
            "fits_in_screen": fits_in_screen,
        }
        args.output_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    dialog.close()
    host.close()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
