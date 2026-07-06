from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from pdf_workbench.services.page_coordinates import PageMetadata
from pdf_workbench.services.pdf_renderer import DocumentMetadata, DocumentRevision
from pdf_workbench.ui.pdf_view import PagePlaceholder, PdfView
from pdf_workbench.ui.theme import (
    ColorScheme,
    ThemeController,
    apply_application_theme,
    detect_system_color_scheme,
    load_stylesheet,
)


class _ThemeRenderService(QObject):
    document_loaded = Signal(object, int, object)
    document_failed = Signal(object, int, str)
    render_succeeded = Signal(object)
    render_failed = Signal(object, int, int, str)

    def __init__(self) -> None:
        super().__init__()

    def open_document(self, *args, **kwargs) -> None:
        return None

    def request_render(self, *args, **kwargs) -> None:
        return None

    def close_document(self, *args, **kwargs) -> None:
        return None

    def update_document_generation(self, *args, **kwargs) -> None:
        return None

    def shutdown(self, *args, **kwargs) -> bool:
        return True


def test_load_stylesheet_returns_bootstrap_like_styles() -> None:
    light = load_stylesheet(ColorScheme.LIGHT)
    dark = load_stylesheet(ColorScheme.DARK)

    assert "QToolButton" in light
    assert "QToolButton" in dark
    assert "QMenu" not in light
    assert "QWidget {" not in light
    assert "QTabBar::close-button" not in light
    assert "QToolButton:focus" in light
    assert light != dark


def test_theme_controller_applies_stylesheet_and_tracks_scheme(qtbot) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)

    controller = ThemeController(app)
    controller.start()
    qtbot.waitUntil(lambda: bool(app.styleSheet()))

    assert app.property("colorScheme") in {"light", "dark"}
    assert controller.current_scheme in {ColorScheme.LIGHT, ColorScheme.DARK}
    apply_application_theme(app, controller.current_scheme)
    assert app.property("colorScheme") == controller.current_scheme.value
    assert detect_system_color_scheme() in {ColorScheme.LIGHT, ColorScheme.DARK}


def test_theme_application_does_not_change_geometry(qtbot, tmp_path: Path) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)

    document_path = tmp_path / "theme.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=144, height=200)
    with document_path.open("wb") as output:
        writer.write(output)

    view = PdfView(_ThemeRenderService(), debounce_interval_ms=0)
    qtbot.addWidget(view)
    view._metadata = DocumentMetadata(
        revision=DocumentRevision.from_path(document_path),
        pages=(PageMetadata.from_size(144.0, 200.0),),
    )

    placeholder = PagePlaceholder(0, view._canvas)
    placeholder.configure(
        PageMetadata.from_size(144.0, 200.0),
        1.5,
        0,
        1.0,
    )
    view._canvas.set_pages([placeholder])

    before_placeholder = placeholder.sizeHint()
    before_view = view.page_content_rect(0)
    apply_application_theme(app, ColorScheme.LIGHT)
    after_placeholder = placeholder.sizeHint()
    after_view = view.page_content_rect(0)

    assert before_placeholder == after_placeholder
    assert before_view == after_view
