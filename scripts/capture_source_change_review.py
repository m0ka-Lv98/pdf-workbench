from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from shutil import copy2

from PySide6.QtWidgets import QApplication

from pdf_test_utils import create_blank_pdf
from pdf_workbench.__main__ import _apply_window_size, _build_ui_state, _flush_layout
from pdf_workbench.core.settings import configure_qsettings
from pdf_workbench.services.pdf_save_service import PdfSaveService
from pdf_workbench.services.session_workspace import SessionWorkspaceManager
from pdf_workbench.services.source_change_monitor import SourceChangeMonitor
from pdf_workbench.ui.main_window import MainWindow
from pdf_workbench.ui.theme import ColorScheme, apply_application_theme


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture source-change review screenshots")
    parser.add_argument("--fixture-pdf", type=Path, required=True)
    parser.add_argument("--output-png", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--window-size", type=str, required=True)
    parser.add_argument(
        "--color-scheme",
        choices=[ColorScheme.LIGHT.value, ColorScheme.DARK.value],
        required=True,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    apply_application_theme(app, ColorScheme(args.color_scheme))
    with tempfile.TemporaryDirectory() as temp_directory:
        temp_root = Path(temp_directory)
        source_path = temp_root / "source.pdf"
        replacement_path = temp_root / "replacement.pdf"
        copy2(args.fixture_pdf, source_path)
        create_blank_pdf(replacement_path, 2)
        settings = configure_qsettings()
        monitor = SourceChangeMonitor(parent=app, poll_interval_ms=2000, debounce_interval_ms=50)
        window = MainWindow(
            settings,
            workspace_manager=SessionWorkspaceManager(temp_root / "sessions"),
            save_service=PdfSaveService(),
            source_change_monitor=monitor,
        )
        _apply_window_size(window, args.window_size)
        window.show()
        app.processEvents()
        window.open_document(source_path)
        copy2(replacement_path, source_path)
        document = window._current_document()
        if document is None:
            raise RuntimeError("document did not open")
        monitor.check_session_now(document.session)
        _flush_layout(window)
        args.output_png.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(args.output_png))
        payload = _build_ui_state(window, requested_window_size=args.window_size)
        banner_geometry = payload["source_change_banner_geometry"]
        payload["source_change_banner_fits_in_viewport"] = (
            banner_geometry[0] >= 0
            and banner_geometry[1] >= 0
            and banner_geometry[0] + banner_geometry[2] <= payload["actual_window_size"][0]
            and banner_geometry[1] + banner_geometry[3] <= payload["actual_window_size"][1]
        )
        args.output_json.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )
        window.close()
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
