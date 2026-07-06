from __future__ import annotations

from PySide6.QtWidgets import QApplication

from pdf_workbench.ui.theme import ColorScheme, ThemeController, load_stylesheet


def test_load_stylesheet_returns_bootstrap_like_styles() -> None:
    light = load_stylesheet(ColorScheme.LIGHT)
    dark = load_stylesheet(ColorScheme.DARK)

    assert "QToolButton" in light
    assert "QToolButton" in dark
    assert light != dark


def test_theme_controller_applies_stylesheet(qtbot) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)

    controller = ThemeController(app)
    controller.start()

    qtbot.waitUntil(lambda: bool(app.styleSheet()))
    assert "QToolButton" in app.styleSheet()
