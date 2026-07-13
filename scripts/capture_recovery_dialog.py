from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter
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


def _relative_geometry(widget: QWidget, origin: QPoint) -> list[int]:
    top_left = widget.mapToGlobal(widget.rect().topLeft()) - origin
    rect = widget.geometry()
    return [top_left.x(), top_left.y(), rect.width(), rect.height()]


def _unique_color_count(image: QImage) -> int:
    colors: set[tuple[int, int, int, int]] = set()
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            colors.add((color.red(), color.green(), color.blue(), color.alpha()))
            if len(colors) >= 128:
                return len(colors)
    return len(colors)


def _brightness(image: QImage, rect: list[int]) -> float:
    x, y, width, height = rect
    total = 0.0
    samples = 0
    for yy in range(y, y + height):
        for xx in range(x, x + width):
            color = image.pixelColor(xx, yy)
            total += (color.red() + color.green() + color.blue()) / 3.0
            samples += 1
    return 0.0 if samples == 0 else total / samples


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


def _background_color(scheme: ColorScheme) -> QColor:
    if scheme is ColorScheme.DARK:
        return QColor("#16181d")
    return QColor("#f3f5f8")


def main() -> int:
    args = build_parser().parse_args()
    width, height = _parse_size(args.window_size)
    color_scheme = ColorScheme(args.color_scheme)

    existing_app = QApplication.instance()
    app = existing_app if isinstance(existing_app, QApplication) else QApplication([])
    apply_application_theme(app, color_scheme)

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

        for _ in range(6):
            app.processEvents()

        dialog_x = max(0, (width - dialog.width()) // 2)
        dialog_y = max(0, (height - dialog.height()) // 2)
        dialog.move(dialog_x, dialog_y)

        invalid_discardable_item = dialog._tree.topLevelItem(1)
        if invalid_discardable_item is None:
            raise RuntimeError("invalid discardable recovery candidate is missing")
        invalid_discardable_item.setCheckState(0, Qt.CheckState.Checked)

        for _ in range(6):
            app.processEvents()

        dialog_pixmap = dialog.grab()
        canvas = QImage(width, height, QImage.Format.Format_ARGB32)
        canvas.fill(_background_color(color_scheme))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.drawPixmap(dialog_x, dialog_y, dialog_pixmap)
        painter.end()

        args.output_png.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(str(args.output_png))

        screen = dialog.screen() or QApplication.primaryScreen()
        available = None if screen is None else screen.availableGeometry()
        global_origin = dialog.mapToGlobal(dialog.rect().topLeft())
        dialog_geometry = [dialog_x, dialog_y, dialog.width(), dialog.height()]
        tree_geometry = _relative_geometry(dialog._tree, global_origin)
        button_row_geometry = (
            [0, 0, 0, 0]
            if dialog._button_row_widget is None
            else _relative_geometry(dialog._button_row_widget, global_origin)
        )
        actual_screen_geometry = (
            [0, 0, 0, 0]
            if available is None
            else [available.x(), available.y(), available.width(), available.height()]
        )
        fits_in_viewport = (
            dialog_x >= 0
            and dialog_y >= 0
            and dialog_x + dialog.width() <= width
            and dialog_y + dialog.height() <= height
        )
        payload = {
            "requested_window_size": [width, height],
            "actual_window_size": [width, height],
            "review_viewport_geometry": [0, 0, width, height],
            "actual_qt_screen_geometry": actual_screen_geometry,
            "dialog_geometry_in_viewport": dialog_geometry,
            "tree_geometry": tree_geometry,
            "button_row_geometry": button_row_geometry,
            "candidate_count": len(candidates),
            "recoverable_count": sum(1 for candidate in candidates if candidate.recoverable),
            "discardable_count": sum(1 for candidate in candidates if candidate.discardable),
            "recover_enabled": dialog._recover_button.isEnabled(),
            "discard_enabled": dialog._discard_button.isEnabled(),
            "later_visible": dialog._later_button.isVisible(),
            "fits_in_review_viewport": fits_in_viewport,
            "unique_color_count": _unique_color_count(canvas),
            "dialog_digest": hashlib.sha256(
                dialog_pixmap.toImage().bits().tobytes(),  # type: ignore[union-attr]
            ).hexdigest(),
            "canvas_digest": hashlib.sha256(canvas.bits().tobytes()).hexdigest(),  # type: ignore[union-attr]
            "dialog_brightness": _brightness(canvas, dialog_geometry),
            "tree_brightness": _brightness(canvas, tree_geometry),
            "button_row_brightness": _brightness(canvas, button_row_geometry),
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
