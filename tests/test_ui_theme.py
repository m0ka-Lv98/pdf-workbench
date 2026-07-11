from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from pdf_workbench.services.page_coordinates import PageMetadata
from pdf_workbench.services.pdf_renderer import DocumentMetadata, DocumentRevision
from pdf_workbench.ui.icon_provider import IconName, IconProvider, is_icon_valid
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


def test_load_stylesheet_returns_workbench_theme_styles() -> None:
    light = load_stylesheet(ColorScheme.LIGHT)
    dark = load_stylesheet(ColorScheme.DARK)

    assert "QWidget#searchSurface" in light
    assert "QFrame#toolbarSeparator" in light
    assert "QWidget#documentToolbar QPushButton#openPdfButton" in light
    assert "QScrollArea#pdfScrollArea" in light
    assert "QMenuBar" in light
    assert "QTabWidget#documentTabs QTabBar::tab:selected" in light
    assert "bootstrap_" not in light
    assert "bootstrap_" not in dark
    assert "#2563eb" in light.lower()
    assert "#60a5fa" in dark.lower()
    assert "QWidget#documentToolbar QComboBox" in light
    assert "QWidget#emptyState QPushButton" in light
    assert "QWidget#emptyState QLabel#emptyStateTitle" in light
    assert 'QFrame#pageCard[renderState="error"]' in light
    assert light != dark


def test_svg_icon_resources_are_available() -> None:
    for name in IconName:
        svg = IconProvider.load_svg(name)
        assert "<svg" in svg
        assert "currentColor" in svg


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


def test_icon_provider_returns_valid_icons_for_both_schemes(qtbot) -> None:
    app = QApplication.instance()
    assert isinstance(app, QApplication)

    apply_application_theme(app, ColorScheme.LIGHT)
    light_icon = IconProvider.icon(IconName.SEARCH)
    apply_application_theme(app, ColorScheme.DARK)
    dark_icon = IconProvider.icon(IconName.SEARCH)

    assert is_icon_valid(light_icon)
    assert is_icon_valid(dark_icon)
