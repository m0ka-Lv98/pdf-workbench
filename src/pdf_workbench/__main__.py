from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from pdf_workbench import __version__
from pdf_workbench.core.app_paths import APP_AUTHOR, APP_NAME
from pdf_workbench.core.logging_config import configure_logging
from pdf_workbench.core.settings import configure_qsettings
from pdf_workbench.ui.main_window import MainWindow
from pdf_workbench.ui.theme import ColorScheme, ThemeController, apply_application_theme


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
        "--ui-state-path",
        type=Path,
        default=None,
        help="Write current UI geometry and theme state to the given path",
    )
    parser.add_argument(
        "--color-scheme",
        choices=[ColorScheme.LIGHT.value, ColorScheme.DARK.value],
        default=None,
        help="Override the application color scheme for diagnostics",
    )
    parser.add_argument(
        "--quit-after-ms",
        type=int,
        default=None,
        help="Quit the application after the given number of milliseconds",
    )
    parser.add_argument(
        "--window-size",
        type=str,
        default=None,
        help="Resize the main window to WIDTHxHEIGHT before startup actions",
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
    if args.color_scheme is not None:
        apply_application_theme(app, ColorScheme(args.color_scheme))
        window.refresh_theme_assets()
    if args.window_size is not None:
        _apply_window_size(window, args.window_size)
    window.show()

    if args.pdf is not None:
        window.open_document(args.pdf)

    def run_startup_actions() -> None:
        if args.window_size is not None:
            _apply_window_size(window, args.window_size)
        if (
            (args.open_search or args.search_query is not None)
            and window.open_search_bar()
            and args.search_query is not None
        ):
            window._search_bar.search_input.setText(args.search_query)
            window._search_bar.submit_current_query()
        if args.screenshot_path is not None or args.ui_state_path is not None:
            QTimer.singleShot(350, capture_outputs)

    def capture_outputs() -> None:
        if args.window_size is not None:
            _apply_window_size(window, args.window_size)
        _flush_layout(window)
        if args.screenshot_path is not None:
            args.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.screenshot_path))
        if args.ui_state_path is not None:
            args.ui_state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = _build_ui_state(
                window,
                requested_window_size=args.window_size,
            )
            args.ui_state_path.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
                encoding="utf-8",
            )

    QTimer.singleShot(300, run_startup_actions)
    if args.quit_after_ms is not None:
        QTimer.singleShot(args.quit_after_ms, app.quit)

    return app.exec()


def _build_ui_state(window: MainWindow, *, requested_window_size: str | None) -> dict[str, Any]:
    app = QApplication.instance()
    active_theme = None if app is None else app.property("colorScheme")
    current_document = window._current_document()
    pdf_canvas_target = None if current_document is None else current_document.view
    first_page = None
    if current_document is not None and current_document.view._canvas.pages:
        first_page = current_document.view._canvas.pages[0]
    return {
        "requested_window_size": _parse_window_size(requested_window_size),
        "actual_window_size": [window.width(), window.height()],
        "window_minimum_size_hint": [
            window.minimumSizeHint().width(),
            window.minimumSizeHint().height(),
        ],
        "main_toolbar_geometry": _geometry(window._main_toolbar),
        "tab_bar_geometry": _geometry(window._tabs.tabBar()),
        "search_row_geometry": _geometry(window._search_toolbar),
        "search_surface_geometry": _geometry(window._search_surface),
        "status_bar_geometry": _geometry(window.statusBar()),
        "status_left_geometry": _geometry(window._status_left),
        "status_icon_geometry": _geometry(window._status_icon),
        "status_message_geometry": _geometry(window._status_message),
        "status_message_text": window._status_message.text(),
        "native_status_message": window.statusBar().currentMessage(),
        "page_input_geometry": _geometry(window._toolbar_widget.page_field),
        "zoom_control_geometry": _geometry(window._toolbar_widget.zoom_field),
        "pdf_canvas_geometry": _geometry(pdf_canvas_target),
        "search_surface_window_geometry": _window_geometry(window, window._search_surface),
        "search_input_surface_geometry": _window_geometry(
            window,
            window._search_bar.search_input_surface,
        ),
        "search_input_surface_border_geometry": _window_geometry(
            window,
            window._search_bar.search_input_surface,
        ),
        "search_input_surface_size": [
            window._search_bar.search_input_surface.width(),
            window._search_bar.search_input_surface.height(),
        ],
        "search_icon_geometry": _window_geometry(window, window._search_bar.search_icon),
        "search_line_edit_geometry": _window_geometry(window, window._search_bar.search_input),
        "search_clear_button_geometry": _window_geometry(window, window._search_bar.clear_button),
        "first_page_window_geometry": _window_geometry(window, first_page),
        "active_theme": active_theme,
        "search_query": window._search_bar.search_input.text(),
        "search_counter": window._search_bar.counter_label.text(),
        "search_progress_text": window._search_bar.progress_label.text(),
        "visible_controls": {
            "open": window._toolbar_widget.open_button.isVisible(),
            "search": window._toolbar_widget.search_button.isVisible(),
            "previous": window._toolbar_widget.previous_button.isVisible(),
            "next": window._toolbar_widget.next_button.isVisible(),
            "zoom_out": window._toolbar_widget.zoom_out_button.isVisible(),
            "zoom_in": window._toolbar_widget.zoom_in_button.isVisible(),
            "rotate": window._toolbar_widget.rotate_button.isVisible(),
            "search_surface": (
                window._search_surface is not None and window._search_surface.isVisible()
            ),
        },
    }


def _geometry(widget: object) -> list[int]:
    geometry = getattr(widget, "geometry", None)
    if geometry is None:
        return [0, 0, 0, 0]
    rect = geometry()
    return [rect.x(), rect.y(), rect.width(), rect.height()]


def _window_geometry(window: MainWindow, widget: QWidget | None) -> list[int]:
    if widget is None:
        return [0, 0, 0, 0]
    top_left = widget.mapTo(window, QPoint(0, 0))
    geometry = widget.geometry()
    return [top_left.x(), top_left.y(), geometry.width(), geometry.height()]


def _parse_window_size(size: str | None) -> list[int] | None:
    if size is None:
        return None
    width_text, separator, height_text = size.partition("x")
    if separator != "x":
        return None
    return [int(width_text), int(height_text)]


def _apply_window_size(window: MainWindow, size: str) -> None:
    parsed = _parse_window_size(size)
    if parsed is None:
        raise ValueError("--window-size must use WIDTHxHEIGHT format")
    width, height = parsed
    window.setFixedSize(width, height)
    _flush_layout(window)


def _flush_layout(window: MainWindow) -> None:
    app = QApplication.instance()
    if app is None:
        return
    for _ in range(3):
        app.processEvents()
    layout = window.layout()
    if layout is not None:
        layout.activate()
    central_layout = window.centralWidget().layout() if window.centralWidget() is not None else None
    if central_layout is not None:
        central_layout.activate()
    window.updateGeometry()
    window.repaint()
    for _ in range(3):
        app.processEvents()


if __name__ == "__main__":
    raise SystemExit(main())
