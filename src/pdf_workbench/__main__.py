from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

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
        "--expected-search-count",
        type=int,
        default=None,
        help="Expected total result count for startup search diagnostics",
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
        help="Write startup UI diagnostics JSON to the given path",
    )
    parser.add_argument(
        "--quit-after-ms",
        type=int,
        default=None,
        help="Quit the application after the given number of milliseconds",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


class StartupSearchSmokeController:
    def __init__(
        self,
        *,
        app: QApplication,
        window: MainWindow,
        query: str,
        expected_count: int,
        screenshot_path: Path | None,
        ui_state_path: Path | None,
        timeout_ms: int = 10_000,
    ) -> None:
        self._app = app
        self._window = window
        self._query = query
        self._expected_count = expected_count
        self._screenshot_path = screenshot_path
        self._ui_state_path = ui_state_path
        self._phase = "wait-document"
        self._completed = False
        self._final_payload: dict[str, Any] | None = None
        self._exit_code = 1
        self._close_started = False

        self._poll_timer = QTimer(window)
        self._poll_timer.setInterval(50)
        self._poll_timer.timeout.connect(self._poll)

        self._timeout_timer = QTimer(window)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.setInterval(timeout_ms)
        self._timeout_timer.timeout.connect(self._on_timeout)

        self._close_timer = QTimer(window)
        self._close_timer.setInterval(50)
        self._close_timer.timeout.connect(self._poll_window_close)

        self._close_timeout_timer = QTimer(window)
        self._close_timeout_timer.setSingleShot(True)
        self._close_timeout_timer.setInterval(3000)
        self._close_timeout_timer.timeout.connect(self._on_close_timeout)

    def start(self) -> None:
        self._poll_timer.start()
        self._timeout_timer.start()

    def _poll(self) -> None:
        if self._completed:
            return
        document = self._window._current_document()
        if document is None:
            return

        if self._phase == "wait-document":
            self._window.activateWindow()
            self._window.raise_()
            self._window._toolbar_widget.search_button.click()
            self._phase = "wait-search-ui"
            return

        if self._phase == "wait-search-ui":
            self._window.activateWindow()
            self._window.raise_()
            self._app.processEvents()
            if not self._search_widgets_ready():
                return
            self._window._search_bar.search_input.setText(self._query)
            self._window._search_bar.submit_current_query()
            self._app.processEvents()
            self._phase = "wait-results"
            return

        if self._phase == "wait-results":
            state = document.view.search_state
            if not self._diagnostic_conditions_met(state):
                return
            self._app.processEvents()
            self._phase = "finalize"
            QTimer.singleShot(0, self._finalize)

    def _diagnostic_conditions_met(self, state: object) -> bool:
        document = self._window._current_document()
        if document is None:
            return False
        search_input = self._window._search_bar.search_input
        search_toolbar = self._window._search_toolbar
        if search_toolbar is None:
            return False
        if not self._search_widgets_ready():
            return False
        if search_input.text() != self._query:
            return False
        if not search_input.isEnabled():
            return False
        view_state = document.view.search_state
        if view_state.query != self._query:
            return False
        if view_state.total_count != self._expected_count:
            return False
        if view_state.current_index != 1:
            return False
        if search_toolbar.geometry().height() < self._window._search_bar.geometry().height():
            return False
        if (
            self._window._search_bar.geometry().height()
            < self._window._search_bar.search_input.geometry().height()
        ):
            return False
        return self._window._search_bar.counter_label.text() == f"1 / {self._expected_count}"

    def _search_widgets_ready(self) -> bool:
        if self._window._search_toolbar is None:
            return False
        if not self._window._search_toolbar.isVisible():
            return False
        if not self._window._search_bar.isVisible():
            return False
        if not self._window._search_bar.search_input.isVisible():
            return False
        if not self._window._search_bar.search_input.isEnabled():
            return False
        if self._window._search_toolbar.geometry().width() <= 0:
            return False
        if self._window._search_toolbar.geometry().height() <= 0:
            return False
        if self._window._search_bar.geometry().width() <= 0:
            return False
        if self._window._search_bar.geometry().height() <= 0:
            return False
        if self._window._search_bar.search_input.geometry().width() <= 0:
            return False
        return self._window._search_bar.search_input.geometry().height() > 0

    def _finalize(self) -> None:
        if self._completed:
            return
        document = self._window._current_document()
        if document is None:
            self._fail("No current document while finalizing startup diagnostics.")
            return

        state = document.view.search_state
        if not self._diagnostic_conditions_met(state):
            self._phase = "wait-results"
            return

        payload = self._build_state_payload()
        if self._screenshot_path is not None and not self._capture_screenshot():
            self._fail("Failed to capture startup search screenshot.")
            return

        self._begin_shutdown(payload, exit_code=0)

    def _build_state_payload(self) -> dict[str, Any]:
        document = self._window._current_document()
        state = None if document is None else document.view.search_state
        return {
            "search_toolbar_visible": self._window._search_toolbar is not None
            and self._window._search_toolbar.isVisible(),
            "search_bar_visible": self._window._search_bar.isVisible(),
            "search_input_visible": self._window._search_bar.search_input.isVisible(),
            "search_input_enabled": self._window._search_bar.search_input.isEnabled(),
            "search_input_focused": self._window._search_bar.search_input.hasFocus(),
            "search_toolbar_geometry": self._geometry_payload(self._window._search_toolbar),
            "search_bar_geometry": self._geometry_payload(self._window._search_bar),
            "search_input_geometry": self._geometry_payload(self._window._search_bar.search_input),
            "input_text": self._window._search_bar.search_input.text(),
            "query": "" if state is None else state.query,
            "current_index": 0 if state is None else state.current_index,
            "total_count": 0 if state is None else state.total_count,
            "counter_text": self._window._search_bar.counter_label.text(),
            "clean_shutdown": False,
        }

    @staticmethod
    def _geometry_payload(widget: QWidget | None) -> list[int]:
        if widget is None:
            return [0, 0, 0, 0]
        geometry = widget.geometry()
        return [geometry.x(), geometry.y(), geometry.width(), geometry.height()]

    def _capture_screenshot(self) -> bool:
        if self._screenshot_path is None:
            return True
        self._screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        return self._window.grab().save(str(self._screenshot_path))

    def _write_ui_state(self, payload: dict[str, Any]) -> None:
        if self._ui_state_path is None:
            return
        self._ui_state_path.parent.mkdir(parents=True, exist_ok=True)
        self._ui_state_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def _on_timeout(self) -> None:
        payload = self._build_state_payload()
        self._fail(
            "Timed out waiting for packaged search UI readiness: "
            + json.dumps(payload, ensure_ascii=True),
            payload=payload,
        )

    def _fail(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        if self._completed:
            return
        self._final_payload = payload if payload is not None else self._build_state_payload()
        print(message, file=sys.stderr)
        self._begin_shutdown(self._final_payload, exit_code=1)

    def _begin_shutdown(self, payload: dict[str, Any], *, exit_code: int) -> None:
        if self._completed:
            return
        self._completed = True
        self._final_payload = payload
        self._exit_code = exit_code
        self._poll_timer.stop()
        self._timeout_timer.stop()
        self._close_timeout_timer.start()
        self._close_started = True
        self._window.close()
        if not self._window.isVisible():
            self._complete_shutdown(clean_shutdown=True)
            return
        self._close_timer.start()

    def _poll_window_close(self) -> None:
        if not self._close_started:
            return
        if self._window.isVisible():
            return
        self._complete_shutdown(clean_shutdown=True)

    def _on_close_timeout(self) -> None:
        self._complete_shutdown(clean_shutdown=not self._window.isVisible())

    def _complete_shutdown(self, *, clean_shutdown: bool) -> None:
        self._close_timer.stop()
        self._close_timeout_timer.stop()
        payload = (
            self._final_payload if self._final_payload is not None else self._build_state_payload()
        )
        payload["clean_shutdown"] = clean_shutdown
        if self._ui_state_path is not None:
            self._write_ui_state(payload)
        self._app.exit(0 if clean_shutdown and self._exit_code == 0 else self._exit_code)


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

    startup_search_controller: StartupSearchSmokeController | None = None
    if (
        args.open_search
        and args.search_query is not None
        and args.expected_search_count is not None
    ):
        startup_search_controller = StartupSearchSmokeController(
            app=app,
            window=window,
            query=args.search_query,
            expected_count=args.expected_search_count,
            screenshot_path=args.screenshot_path,
            ui_state_path=args.ui_state_path,
        )
        QTimer.singleShot(0, startup_search_controller.start)
    else:
        if args.open_search:
            QTimer.singleShot(300, window.open_search_bar)
        if args.search_query is not None:

            def apply_query() -> None:
                if window.open_search_bar():
                    window._search_bar.search_input.setText(args.search_query or "")
                    window._search_bar.submit_current_query()

            QTimer.singleShot(300, apply_query)
        if args.screenshot_path is not None:

            def capture_screenshot() -> None:
                args.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                window.grab().save(str(args.screenshot_path))

            QTimer.singleShot(300, capture_screenshot)

    if args.quit_after_ms is not None and startup_search_controller is None:
        QTimer.singleShot(args.quit_after_ms, app.quit)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
