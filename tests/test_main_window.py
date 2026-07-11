from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QImage, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMessageBox, QTabBar, QToolButton
from pytestqt.qtbot import QtBot

from pdf_test_utils import copy_pdf_fixture, create_blank_pdf, create_image_only_pdf
from pdf_workbench.services.page_coordinates import PageMetadata
from pdf_workbench.services.pdf_renderer import (
    DocumentMetadata,
    DocumentRevision,
    PdfiumDocumentBackend,
    PdfRenderService,
)
from pdf_workbench.ui.main_window import MainWindow
from pdf_workbench.ui.pdf_view import PdfView


def patch_pdf_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open_document(self: PdfView, path: Path) -> None:
        self._path = path
        self._current_page_index = 0
        self._metadata = DocumentMetadata(
            revision=DocumentRevision.from_path(path),
            pages=(PageMetadata.from_size(144.0, 144.0),),
        )
        self.state_changed.emit()

    monkeypatch.setattr(PdfView, "open_document", fake_open_document)


def create_settings(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "app.ini"), QSettings.Format.IniFormat)


def show_window(qtbot: QtBot, window: MainWindow) -> None:
    window.show()
    qtbot.waitUntil(window.isVisible)


def assert_search_ui_ready(window: MainWindow) -> None:
    assert window._search_toolbar is not None
    assert window._search_surface is not None
    assert window._search_toolbar.isVisible()
    assert window._search_surface.isVisible()
    assert window._search_bar.isVisible()
    assert window._search_bar.search_input.isVisible()
    if QApplication.platformName() != "offscreen":
        assert window._search_bar.search_input.hasFocus()
    assert window._search_toolbar.geometry().width() > 0
    assert window._search_toolbar.geometry().height() > 0
    assert window._search_surface.geometry().width() > 0
    assert window._search_surface.geometry().height() > 0
    assert window._search_bar.geometry().width() > 0
    assert window._search_bar.geometry().height() > 0
    assert window._search_bar.search_input.geometry().width() > 0
    assert window._search_bar.search_input.geometry().height() > 0
    assert window._search_toolbar.geometry().height() >= window._search_bar.geometry().height()
    assert (
        window._search_bar.geometry().height()
        >= window._search_bar.search_input.geometry().height()
    )
    assert window._search_surface.geometry().height() >= window._search_bar.geometry().height()
    search_top = window._search_toolbar.mapTo(
        window,
        window._search_toolbar.rect().topLeft(),
    ).y()
    if window._main_toolbar is not None:
        assert search_top >= window._main_toolbar.geometry().bottom()
    tab_bar_bottom = (
        window._tabs.tabBar()
        .mapTo(
            window,
            window._tabs.tabBar().rect().bottomLeft(),
        )
        .y()
    )
    assert search_top >= tab_bar_bottom
    toolbar_right = window._search_toolbar.rect().right()
    surface_right = window._search_surface.geometry().right()
    assert toolbar_right - surface_right < 40


def assert_button_icon_valid(button: QToolButton) -> None:
    icon = button.icon()
    assert not icon.isNull()
    pixmap = icon.pixmap(16, 16)
    assert not pixmap.isNull()


class DelayedTextBackend(PdfiumDocumentBackend):
    def __init__(self, path: Path, delay_seconds: float) -> None:
        super().__init__(path)
        self._delay_seconds = delay_seconds

    def extract_text_page(self, page_index: int, revision: DocumentRevision):  # type: ignore[override]
        time.sleep(self._delay_seconds)
        return super().extract_text_page(page_index, revision)


def create_real_main_window(
    qtbot: QtBot,
    tmp_path: Path,
    *,
    delay_seconds: float = 0.0,
) -> MainWindow:
    settings = create_settings(tmp_path)

    def backend_factory(path: Path) -> PdfiumDocumentBackend:
        if delay_seconds > 0:
            return DelayedTextBackend(path, delay_seconds)
        return PdfiumDocumentBackend(path)

    service = PdfRenderService(backend_factory=backend_factory)
    window = MainWindow(settings, render_service=service)
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible)
    return window


def test_main_window_opens_and_closes_multiple_documents(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)

    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.touch()
    second.touch()

    window.open_document(first)
    window.open_document(second)

    assert window._tabs.count() == 2
    assert window._documents[0].session.source_path == first.resolve()
    assert window._documents[1].session.source_path == second.resolve()
    assert window._stack.currentWidget() is window._tabs
    assert window._tabs.tabBar().elideMode() == Qt.TextElideMode.ElideMiddle
    assert window._tabs.tabBar().usesScrollButtons() is True
    assert window._tabs.tabsClosable() is False
    assert 32 <= window._tabs.tabBar().height() <= 42
    assert window._tabs.tabBar().drawBase() is False
    close_button = window._tabs.tabBar().tabButton(0, QTabBar.ButtonPosition.RightSide)
    assert isinstance(close_button, QToolButton)
    assert close_button.objectName() == "tabCloseButton"

    assert window.close_document_at(1) is True
    assert window._tabs.count() == 1
    assert window._documents[0].session.source_path == first.resolve()


def test_main_window_requires_confirmation_for_modified_document(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)

    document_path = tmp_path / "modified.pdf"
    document_path.touch()
    window.open_document(document_path)
    window._documents[0].session.mark_modified("test change")

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.No,
    )
    assert window.close_current_document() is False
    assert window._tabs.count() == 1

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    assert window.close_current_document() is True
    assert window._tabs.count() == 0


def test_main_window_persists_recent_files_and_geometry(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)

    document_path = tmp_path / "recent.pdf"
    document_path.touch()

    window.resize(900, 700)
    window.open_document(document_path)
    window._save_window_state()

    reopened = MainWindow(settings)
    qtbot.addWidget(reopened)

    assert reopened._recent_files[0] == document_path.resolve()
    assert settings.value(MainWindow._GEOMETRY_KEY) is not None


def test_main_window_avoids_duplicate_tabs_for_same_document(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)
    show_window(qtbot, window)

    document_path = tmp_path / "duplicate.pdf"
    document_path.touch()

    window.open_document(document_path)
    window.open_document(document_path)

    assert window._tabs.count() == 1
    assert window._tabs.currentIndex() == 0
    assert window._recent_files == [document_path.resolve()]


def test_main_window_drops_missing_recent_files_from_menu(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    missing_path = tmp_path / "missing.pdf"
    settings.setValue(
        MainWindow._RECENT_FILES_KEY,
        f'["{missing_path}"]',
    )

    window = MainWindow(settings)
    qtbot.addWidget(window)

    assert window._recent_files == []
    assert window.recent_files_menu.actions()[0].isEnabled() is False


def test_main_window_starts_on_empty_state_and_updates_toolbar(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    assert window._stack.currentWidget() is window._empty_state
    assert window._toolbar_widget.page_field.text() == "—"
    assert window._toolbar_widget.zoom_field.currentText() == "100%"

    document_path = tmp_path / "state.pdf"
    document_path.touch()
    window.open_document(document_path)

    assert window._stack.currentWidget() is window._tabs
    assert window._toolbar_widget.page_field.value() == 1
    assert window._toolbar_widget.zoom_field.currentText() == "100%"
    assert window._documents[0].session.zoom_factor == 1.0
    assert window._documents[0].view.zoom_factor == pytest.approx(1.5)


def test_main_window_search_toolbar_starts_hidden(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    assert window._search_toolbar is not None
    assert window._search_toolbar.isHidden()
    assert window.find_action.isEnabled() is False
    assert window._toolbar_widget.search_button.isEnabled() is False


def test_main_window_toolbar_search_button_opens_search_ui(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "toolbar-search.pdf", 1)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: bool(window._documents[0].view._canvas.pages))

    assert window._toolbar_widget.search_button.isEnabled() is True
    QTest.mouseClick(window._toolbar_widget.search_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._search_ui_is_ready())
    assert_search_ui_ready(window)
    document = window._documents[0]
    surface_bottom = window._search_surface.mapTo(
        window,
        window._search_surface.rect().bottomLeft(),
    ).y()
    page_top = (
        document.view._canvas.pages[0]
        .mapTo(
            window,
            document.view._canvas.pages[0].rect().topLeft(),
        )
        .y()
    )
    assert page_top >= surface_bottom + 8


def test_main_window_responsive_toolbar_keeps_controls_visible_at_800_width(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.resize(800, 600)
    show_window(qtbot, window)

    document_path = tmp_path / "responsive.pdf"
    document_path.touch()
    window.open_document(document_path)

    for widget in (
        window._toolbar_widget.open_button,
        window._toolbar_widget.search_button,
        window._toolbar_widget.previous_button,
        window._toolbar_widget.page_field,
        window._toolbar_widget.next_button,
        window._toolbar_widget.zoom_out_button,
        window._toolbar_widget.zoom_field,
        window._toolbar_widget.zoom_in_button,
        window._toolbar_widget.rotate_button,
    ):
        assert widget.geometry().width() > 0
        assert widget.geometry().height() > 0
        assert (
            widget.mapTo(window._toolbar_widget, widget.rect().topRight()).x()
            <= window._toolbar_widget.width()
        )
    assert window._main_toolbar is not None
    assert window._main_toolbar.minimumSizeHint().width() <= 800
    assert 56 <= window._toolbar_widget.page_field.width() <= 64
    assert 90 <= window._toolbar_widget.zoom_field.width() <= 104


def test_main_window_status_left_container_has_margin_and_valid_icon(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    layout = window._status_left.layout()
    assert isinstance(layout, QHBoxLayout)
    assert 16 <= layout.contentsMargins().left() <= 18
    assert not window._status_icon.pixmap().isNull()
    assert window._status_message.geometry().left() > window._status_icon.geometry().right()
    assert window.statusBar().currentMessage() == ""
    assert window._status_message.text() == "準備完了"
    assert len(window.findChildren(QLabel, "statusMessageLabel")) == 1

    right_layout = window._status_right.layout()
    assert isinstance(right_layout, QHBoxLayout)
    assert 16 <= right_layout.contentsMargins().right() <= 18


def test_main_window_status_message_uses_custom_label_and_resets_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    window._set_status_message("エラーです", error=True, timeout_ms=10)

    assert window.statusBar().currentMessage() == ""
    assert window._status_message.text() == "エラーです"
    assert window._status_icon.property("error") is True
    assert window._status_icon.geometry().right() < window._status_message.geometry().left()

    qtbot.waitUntil(lambda: window._status_message.text() == "準備完了")
    assert window.statusBar().currentMessage() == ""
    assert window._status_icon.property("error") is False


def test_main_window_uses_custom_tab_close_buttons(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.touch()
    second.touch()
    window.open_document(first)
    window.open_document(second)

    tab_bar = window._tabs.tabBar()
    first_button = tab_bar.tabButton(0, QTabBar.ButtonPosition.RightSide)
    second_button = tab_bar.tabButton(1, QTabBar.ButtonPosition.RightSide)
    assert isinstance(first_button, QToolButton)
    assert isinstance(second_button, QToolButton)
    assert first_button.toolTip() == "閉じる"
    assert first_button.accessibleName() == "タブを閉じる"
    assert_button_icon_valid(first_button)
    assert_button_icon_valid(second_button)

    QTest.mouseClick(first_button, Qt.MouseButton.LeftButton)
    assert window._tabs.count() == 1
    assert window._documents[0].session.source_path == second.resolve()

    remaining_button = tab_bar.tabButton(0, QTabBar.ButtonPosition.RightSide)
    assert isinstance(remaining_button, QToolButton)
    QTest.mouseClick(remaining_button, Qt.MouseButton.LeftButton)
    assert window._tabs.count() == 0


def test_main_window_refreshes_tab_close_button_icons_on_theme_change(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "theme.pdf"
    document_path.touch()
    window.open_document(document_path)
    button = window._tabs.tabBar().tabButton(0, QTabBar.ButtonPosition.RightSide)
    assert isinstance(button, QToolButton)
    assert_button_icon_valid(button)

    from pdf_workbench.ui.theme import ColorScheme, apply_application_theme

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    apply_application_theme(app, ColorScheme.DARK)
    window.refresh_theme_assets()
    assert_button_icon_valid(button)


def test_main_window_find_action_opens_search_ui(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "menu-search.pdf"
    document_path.touch()
    window.open_document(document_path)

    assert window.find_action.isEnabled() is True
    window.find_action.trigger()
    qtbot.waitUntil(lambda: window._search_ui_is_ready())
    assert_search_ui_ready(window)


def test_main_window_search_shortcut_opens_search_ui(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "shortcut-search.pdf"
    document_path.touch()
    window.open_document(document_path)
    document = window._documents[0]
    document.view.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(document.view.hasFocus)

    expected_sequence = (
        QKeySequence("Meta+F") if sys.platform == "darwin" else QKeySequence("Ctrl+F")
    )
    QTest.keySequence(window, expected_sequence)

    qtbot.waitUntil(lambda: window._search_ui_is_ready())
    assert_search_ui_ready(window)


def test_main_window_open_search_bar_refocuses_existing_input(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "refocus-search.pdf"
    document_path.touch()
    window.open_document(document_path)

    assert window.open_search_bar() is True
    window._toolbar_widget.page_field.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(window._toolbar_widget.page_field.hasFocus)

    assert window.open_search_bar() is True
    assert_search_ui_ready(window)


def test_main_window_copy_action_tracks_focus_and_selection(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "copy.pdf"
    document_path.touch()
    window.open_document(document_path)
    assert window.open_search_bar() is True

    line_edit = window._search_bar.search_input
    line_edit.setText("selected")
    line_edit.setSelection(0, len(line_edit.text()))
    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: line_edit))
    window._update_actions()

    assert window.copy_action.isEnabled()

    document = window._documents[0]
    document.view._selection = None
    document.view._search_query = ""
    window._on_focus_changed(line_edit, document.view)

    assert QApplication.focusWidget() is not None


def test_main_window_copy_action_prefers_line_edit_selection(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "copy-priority.pdf"
    document_path.touch()
    window.open_document(document_path)
    assert window.open_search_bar() is True

    line_edit = window._search_bar.search_input
    line_edit.setText("line-edit")
    line_edit.setSelection(0, 4)
    document = window._documents[0]
    document.view._selection = object()  # type: ignore[assignment]
    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: line_edit))

    copied: list[str] = []
    monkeypatch.setattr(line_edit, "copy", lambda: copied.append(line_edit.selectedText()))
    monkeypatch.setattr(document.view, "copy_selected_text", lambda: False)

    window._copy_selection()

    assert copied == ["line"]


def test_main_window_search_bar_enter_and_shift_enter_fire_once(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "search-events.pdf"
    document_path.touch()
    window.open_document(document_path)
    assert window.open_search_bar() is True

    next_calls = 0
    previous_calls = 0

    def record_next() -> bool:
        nonlocal next_calls
        next_calls += 1
        return False

    def record_previous() -> bool:
        nonlocal previous_calls
        previous_calls += 1
        return False

    monkeypatch.setattr(window._documents[0].view, "next_match", record_next)
    monkeypatch.setattr(window._documents[0].view, "previous_match", record_previous)

    QTest.keyClick(window._search_bar.search_input, Qt.Key.Key_Return)
    QTest.keyClick(
        window._search_bar.search_input,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.ShiftModifier,
    )

    assert next_calls == 1
    assert previous_calls == 1


def test_main_window_search_progress_text_uses_failed_page_count() -> None:
    from pdf_workbench.ui.pdf_view import PdfSearchState

    state = PdfSearchState(
        query="",
        current_index=0,
        total_count=0,
        indexed_pages=2,
        total_pages=10,
        failed_pages=2,
        indexing_completed=False,
    )
    completed = PdfSearchState(
        query="",
        current_index=0,
        total_count=0,
        indexed_pages=8,
        total_pages=10,
        failed_pages=2,
        indexing_completed=True,
        text_pages_with_content=8,
    )

    assert MainWindow._search_progress_text(state) == "索引作成中 2 / 10\uff082ページ失敗\uff09"
    assert MainWindow._search_progress_text(completed) == "索引完了\uff082ページ失敗\uff09"


def test_main_window_search_progress_text_distinguishes_no_text_and_ocr() -> None:
    from pdf_workbench.ui.pdf_view import PdfSearchState

    no_text_state = PdfSearchState(
        query="abc",
        current_index=0,
        total_count=0,
        indexed_pages=1,
        total_pages=1,
        failed_pages=0,
        indexing_completed=True,
        text_pages_with_content=0,
        image_only_pages=0,
        empty_text_pages=1,
    )
    image_state = PdfSearchState(
        query="abc",
        current_index=0,
        total_count=0,
        indexed_pages=1,
        total_pages=1,
        failed_pages=0,
        indexing_completed=True,
        text_pages_with_content=0,
        image_only_pages=1,
        empty_text_pages=1,
    )

    assert MainWindow._search_progress_text(no_text_state) == "テキストレイヤーがありません"
    assert MainWindow._search_progress_text(image_state) == "OCRが必要な画像PDF"


def test_main_window_real_search_updates_after_index_completion(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = copy_pdf_fixture(
        "real-search-english.pdf",
        tmp_path / "real-search-english.pdf",
    )
    window = create_real_main_window(qtbot, tmp_path, delay_seconds=0.35)

    window.open_document(document_path)
    assert window.open_search_bar() is True
    search_input = window._search_bar.search_input
    search_input.clear()
    search_input.setText("Alpha")
    window._search_bar.submit_current_query()

    qtbot.waitUntil(lambda: window._documents[0].view.search_state.query == "Alpha", timeout=8000)
    qtbot.waitUntil(lambda: window._documents[0].view.search_state.total_count == 2, timeout=8000)
    qtbot.waitUntil(lambda: window._documents[0].view.search_state.current_index == 1, timeout=8000)
    qtbot.waitUntil(lambda: window._documents[0].view.search_state.indexing_completed, timeout=8000)

    document = window._documents[0]
    assert document.view.search_state.failed_pages == 0
    assert window._search_bar.counter_label.text() == "1 / 2"
    assert window._search_bar.progress_label.text() == ""
    assert len(document.view._canvas.pages[0]._current_match_boxes) == 1
    assert len(document.view._canvas.pages[0]._match_boxes) == 2

    QTest.keyClick(window, Qt.Key.Key_F3)
    qtbot.waitUntil(lambda: document.view.search_state.current_index == 2)
    assert window._search_bar.counter_label.text() == "2 / 2"

    QTest.keyClick(window, Qt.Key.Key_F3, Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: document.view.search_state.current_index == 1)

    document.view.close_document()
    assert document.view._render_service.shutdown()


def test_main_window_real_search_supports_japanese_text(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = copy_pdf_fixture(
        "real-search-japanese.pdf",
        tmp_path / "real-search-japanese.pdf",
    )
    window = create_real_main_window(qtbot, tmp_path)

    window.open_document(document_path)
    assert window.open_search_bar() is True
    search_input = window._search_bar.search_input
    search_input.clear()
    search_input.setText("検索")
    window._search_bar.submit_current_query()

    qtbot.waitUntil(lambda: window._documents[0].view.search_state.query == "検索", timeout=8000)
    qtbot.waitUntil(lambda: window._documents[0].view.search_state.indexing_completed, timeout=8000)
    qtbot.waitUntil(lambda: window._documents[0].view.search_state.total_count == 2, timeout=8000)

    document = window._documents[0]
    assert document.view.search_state.current_index == 1
    assert window._search_bar.counter_label.text() == "1 / 2"
    assert window._search_bar.progress_label.text() == ""
    assert len(document.view._canvas.pages[0]._current_match_boxes) == 1

    document.view.close_document()
    assert document.view._render_service.shutdown()


def test_main_window_real_search_reports_blank_pdf_without_text_layer(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_blank_pdf(tmp_path / "blank.pdf", 1)
    window = create_real_main_window(qtbot, tmp_path)

    window.open_document(document_path)
    assert window.open_search_bar() is True
    window._search_bar.search_input.setText("Alpha")
    window._search_bar.submit_current_query()

    qtbot.waitUntil(lambda: window._documents[0].view.search_state.indexing_completed, timeout=8000)

    assert window._search_bar.progress_label.text() == "テキストレイヤーがありません"
    assert window._search_bar.counter_label.text() == "0 / 0"

    document = window._documents[0]
    document.view.close_document()
    assert document.view._render_service.shutdown()


def test_main_window_real_search_reports_image_pdf_needs_ocr(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_image_only_pdf(tmp_path / "image-only.pdf")
    window = create_real_main_window(qtbot, tmp_path)

    window.open_document(document_path)
    assert window.open_search_bar() is True
    window._search_bar.search_input.setText("scan")
    window._search_bar.submit_current_query()

    qtbot.waitUntil(lambda: window._documents[0].view.search_state.indexing_completed, timeout=8000)

    assert window._search_bar.progress_label.text() == "OCRが必要な画像PDF"
    assert window._search_bar.counter_label.text() == "0 / 0"

    document = window._documents[0]
    document.view.close_document()
    assert document.view._render_service.shutdown()


def test_main_window_copy_action_falls_back_to_pdf_view_when_line_edit_has_no_selection(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "copy-fallback.pdf"
    document_path.touch()
    window.open_document(document_path)
    assert window.open_search_bar() is True

    line_edit = window._search_bar.search_input
    line_edit.setText("line-edit")
    line_edit.deselect()
    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: line_edit))

    copied = {"pdf": 0}
    monkeypatch.setattr(
        window._documents[0].view,
        "copy_selected_text",
        lambda: copied.__setitem__("pdf", copied["pdf"] + 1) or True,
    )

    window._copy_selection()

    assert copied["pdf"] == 1


@pytest.mark.parametrize(
    ("user_zoom", "logical_zoom"),
    [
        (0.25, 0.375),
        (1.0, 1.5),
        (1.25, 1.875),
        (2.0, 3.0),
        (3.33, pytest.approx(4.995)),
        (4.0, 6.0),
        (5.0, 7.5),
    ],
)
def test_main_window_maps_user_zoom_to_logical_zoom(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
    user_zoom: float,
    logical_zoom: float,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    document_path = tmp_path / "zoom.pdf"
    document_path.touch()
    window.open_document(document_path)
    window._set_zoom_from_toolbar(user_zoom)

    assert window._documents[0].session.zoom_factor == pytest.approx(user_zoom)
    assert window._documents[0].view.zoom_factor == pytest.approx(logical_zoom)


def test_main_window_accepts_pdf_drag_and_drop(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    document_path = tmp_path / "drop.pdf"
    document_path.touch()
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(document_path))])

    drag_event = QDragEnterEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dragEnterEvent(drag_event)
    assert drag_event.isAccepted()

    drop_event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(drop_event)

    assert window._tabs.count() == 1
    assert window._documents[0].session.source_path == document_path.resolve()


def test_main_window_opens_real_pdf_document(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    document_path = create_blank_pdf(tmp_path / "real.pdf", 1)

    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 1)

    assert window._tabs.count() == 1
    assert window._documents[0].session.source_path == document_path.resolve()
    assert window._documents[0].view.page_count == 1


def test_main_window_shares_one_render_service_across_tabs(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.touch()
    second.touch()

    window.open_document(first)
    window.open_document(second)

    assert window._documents[0].view._render_service is window._render_service
    assert window._documents[1].view._render_service is window._render_service


def test_main_window_keeps_open_when_render_service_shutdown_times_out(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    monkeypatch.setattr(window._render_service, "shutdown", lambda timeout_ms=3000: False)

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is False
    monkeypatch.undo()
    window._render_service._thread.quit()
    assert window._render_service._thread.wait(5000) is True


def test_main_window_diagnostic_capture_stays_at_800_by_600(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    from pdf_workbench.__main__ import _apply_window_size, _build_ui_state

    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    document_path = tmp_path / "diag.pdf"
    document_path.touch()
    _apply_window_size(window, "800x600")
    window.show()
    qtbot.waitUntil(window.isVisible)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window.width() == 800 and window.height() == 600)

    screenshot_path = tmp_path / "window-800x600.png"
    assert window.grab().save(str(screenshot_path))
    image = QImage(str(screenshot_path))

    payload = _build_ui_state(window, requested_window_size="800x600")

    assert not image.isNull()
    assert image.width() == 800
    assert image.height() == 600
    assert payload["actual_window_size"] == [800, 600]
