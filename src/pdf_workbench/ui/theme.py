from __future__ import annotations

from enum import StrEnum
from importlib.resources import files

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QApplication


class ColorScheme(StrEnum):
    LIGHT = "light"
    DARK = "dark"


def _stylesheet_name(scheme: ColorScheme) -> str:
    return "bootstrap_dark.qss" if scheme is ColorScheme.DARK else "bootstrap_light.qss"


def load_stylesheet(scheme: ColorScheme) -> str:
    resource = files("pdf_workbench.ui.styles").joinpath(_stylesheet_name(scheme))
    if not resource.is_file():
        raise FileNotFoundError(f"missing theme resource: {resource}")
    return resource.read_text(encoding="utf-8")


def detect_system_color_scheme() -> ColorScheme:
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorScheme"):
        scheme = hints.colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return ColorScheme.DARK
        if scheme == Qt.ColorScheme.Light:
            return ColorScheme.LIGHT
    palette = QGuiApplication.palette()
    window_color = palette.window().color()
    return ColorScheme.DARK if _is_dark(window_color) else ColorScheme.LIGHT


def apply_application_theme(app: QApplication, scheme: ColorScheme) -> None:
    app.setProperty("colorScheme", scheme.value)
    app.setStyleSheet(load_stylesheet(scheme))


def _is_dark(color: QColor) -> bool:
    return color.lightness() < 128


class ThemeController(QObject):
    scheme_changed = Signal(object)

    def __init__(self, app: QApplication, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._app = app
        self._current_scheme = detect_system_color_scheme()

    def start(self) -> None:
        self.apply_system_scheme()
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self.apply_system_scheme)

    def apply_system_scheme(self) -> None:
        scheme = detect_system_color_scheme()
        if scheme == self._current_scheme and self._app.styleSheet():
            return
        self._current_scheme = scheme
        apply_application_theme(self._app, scheme)
        self.scheme_changed.emit(scheme)

    @property
    def current_scheme(self) -> ColorScheme:
        return self._current_scheme
