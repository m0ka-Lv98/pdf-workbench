from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from pdf_workbench import __version__
from pdf_workbench.core.app_paths import APP_AUTHOR, APP_NAME
from pdf_workbench.core.logging_config import configure_logging
from pdf_workbench.core.settings import configure_qsettings
from pdf_workbench.ui.main_window import MainWindow
from pdf_workbench.ui.theme import ThemeController


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local-first PDF desktop workbench")
    parser.add_argument("pdf", nargs="?", type=Path, help="PDF file to open")
    parser.add_argument(
        "--open-search",
        action="store_true",
        help="Open the search bar after launch",
    )
    parser.add_argument(
        "--search-query",
        type=str,
        default=None,
        help="Prefill the search bar with a query after launch",
    )
    parser.add_argument(
        "--screenshot-path",
        type=Path,
        default=None,
        help="Capture a window screenshot to the given path after launch",
    )
    parser.add_argument(
        "--quit-after-ms",
        type=int,
        default=None,
        help="Quit the application after the given number of milliseconds",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    configure_logging()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_AUTHOR)
    app.setApplicationVersion(__version__)
    theme_controller = ThemeController(app)
    theme_controller.start()
    settings = configure_qsettings()

    window = MainWindow(settings)
    window.show()

    if args.pdf is not None:
        window.open_document(args.pdf)

    def prime_search_ui() -> None:
        if not (args.open_search or args.search_query is not None):
            return
        if not window.open_search_bar():
            return
        if args.search_query is not None:
            window._search_bar.search_input.setText(args.search_query)
            window._search_bar._emit_debounced_search()

    def capture_screenshot() -> None:
        if args.screenshot_path is None:
            return
        args.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(args.screenshot_path))

    QTimer.singleShot(300, prime_search_ui)
    if args.screenshot_path is not None:
        screenshot_delay_ms = 900 if (args.open_search or args.search_query is not None) else 300
        QTimer.singleShot(screenshot_delay_ms, capture_screenshot)
    if args.quit_after_ms is not None:
        QTimer.singleShot(args.quit_after_ms, app.quit)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
