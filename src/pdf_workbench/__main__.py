from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from pdf_workbench import __version__
from pdf_workbench.core.app_paths import APP_AUTHOR, APP_NAME
from pdf_workbench.core.logging_config import configure_logging
from pdf_workbench.core.settings import configure_qsettings
from pdf_workbench.domain.document_session import SourceStatus
from pdf_workbench.services.pdf_document_validator import PdfDocumentValidator
from pdf_workbench.services.pdf_save_service import PdfSaveService
from pdf_workbench.services.session_recovery import RecoveryCandidate, SessionRecoveryService
from pdf_workbench.services.session_workspace import SessionWorkspaceManager
from pdf_workbench.services.source_change_monitor import SourceChangeMonitor
from pdf_workbench.ui.dialogs.recovery_dialog import RecoveryDialog, RecoveryDialogAction
from pdf_workbench.ui.main_window import MainWindow, RestoreSessionResult
from pdf_workbench.ui.theme import ColorScheme, ThemeController, apply_application_theme

logger = logging.getLogger(__name__)


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
    parser.add_argument(
        "--skip-recovery-prompt",
        action="store_true",
        help="Skip interrupted-session recovery prompts during startup",
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
    validator = PdfDocumentValidator()
    workspace_manager = SessionWorkspaceManager()
    save_service = PdfSaveService(validator)
    recovery_service = SessionRecoveryService(workspace_manager, validator=validator)
    source_change_monitor = SourceChangeMonitor(parent=app)
    window = MainWindow(
        settings,
        workspace_manager=workspace_manager,
        save_service=save_service,
        recovery_service=recovery_service,
        source_change_monitor=source_change_monitor,
    )
    if args.color_scheme is not None:
        apply_application_theme(app, ColorScheme(args.color_scheme))
        window.refresh_theme_assets()
    if args.window_size is not None:
        _apply_window_size(window, args.window_size)
    window.show()

    _perform_initial_document_open(
        window,
        recovery_service=recovery_service,
        cli_pdf=args.pdf,
        skip_recovery_prompt=args.skip_recovery_prompt,
    )

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


def _handle_startup_recovery(
    window: MainWindow,
    recovery_service: SessionRecoveryService,
) -> None:
    scan_result = recovery_service.scan_candidates()
    if not scan_result.candidates:
        return
    dialog = RecoveryDialog(scan_result.candidates, window)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    dialog.exec()
    result = dialog.result_value
    restored_source_paths: set[Path] = set()

    selected = set(id(candidate) for candidate in result.candidates)
    for candidate in scan_result.candidates:
        if result.action is RecoveryDialogAction.RECOVER and id(candidate) in selected:
            if candidate.metadata.source_path in restored_source_paths:
                recovery_service.release_candidate(candidate)
                QMessageBox.information(
                    window,
                    "復旧候補を保持しました",
                    "同じ元ファイルの復旧セッションが既に開かれているため、この候補は後で復旧できるよう保持しました。",
                )
                continue
            restore_result = _restore_candidate(window, recovery_service, candidate)
            if restore_result is not RestoreSessionResult.FAILED:
                restored_source_paths.add(candidate.metadata.source_path)
            continue
        if result.action is RecoveryDialogAction.DISCARD and id(candidate) in selected:
            _discard_candidate(window, recovery_service, candidate)
            continue
        recovery_service.release_candidate(candidate)


def _restore_candidate(
    window: MainWindow,
    recovery_service: SessionRecoveryService,
    candidate: RecoveryCandidate,
) -> RestoreSessionResult:
    try:
        session = recovery_service.restore_candidate(candidate)
    except Exception as exc:
        logger.exception("Failed to restore interrupted session: %s", candidate.workspace_directory)
        QMessageBox.critical(window, "復旧に失敗しました", str(exc))
        recovery_service.release_candidate(candidate)
        return RestoreSessionResult.FAILED
    return window.restore_session(session)


def _discard_candidate(
    window: MainWindow,
    recovery_service: SessionRecoveryService,
    candidate: RecoveryCandidate,
) -> None:
    try:
        recovery_service.discard_candidate(candidate)
    except Exception as exc:
        logger.exception("Failed to discard interrupted session: %s", candidate.workspace_directory)
        QMessageBox.critical(window, "復旧候補を破棄できません", str(exc))


def _perform_initial_document_open(
    window: MainWindow,
    *,
    recovery_service: SessionRecoveryService,
    cli_pdf: Path | None,
    skip_recovery_prompt: bool,
) -> None:
    if not skip_recovery_prompt:
        _handle_startup_recovery(window, recovery_service)
    if cli_pdf is not None:
        window.open_document(cli_pdf)


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
        "source_change_banner_geometry": _geometry(window._source_change_banner),
        "source_change_message_geometry": _geometry(window._source_change_banner.message_label),
        "source_change_save_as_geometry": _geometry(window._source_change_banner.save_as_button),
        "source_change_recheck_geometry": _geometry(window._source_change_banner.recheck_button),
        "source_change_later_geometry": _geometry(window._source_change_banner.later_button),
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
        "tab_title": "" if current_document is None else window._tab_title(current_document),
        "source_status": (
            SourceStatus.UNCHANGED.value
            if current_document is None
            else current_document.session.source_status.value
        ),
        "save_action_enabled": window.save_action.isEnabled(),
        "save_as_action_enabled": window.save_as_action.isEnabled(),
        "source_change_banner_visible": window._source_change_banner.isVisible(),
        "source_change_button_clipping": (
            not (
                window._source_change_banner.rect().contains(
                    window._source_change_banner.save_as_button.geometry()
                )
                and window._source_change_banner.rect().contains(
                    window._source_change_banner.recheck_button.geometry()
                )
                and window._source_change_banner.rect().contains(
                    window._source_change_banner.later_button.geometry()
                )
            )
        ),
        "source_change_horizontal_overflow": (
            window._source_change_banner.sizeHint().width() > window.width()
        ),
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
            "source_change_banner": window._source_change_banner.isVisible(),
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
