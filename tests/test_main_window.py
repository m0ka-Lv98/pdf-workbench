from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter
from PySide6.QtCore import QMimeData, QPoint, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from pytestqt.qtbot import QtBot

from pdf_workbench.services.page_coordinates import PageMetadata
from pdf_workbench.services.pdf_renderer import DocumentMetadata, DocumentRevision
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


def test_main_window_opens_and_closes_multiple_documents(
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

    assert window._tabs.count() == 2
    assert window._documents[0].session.source_path == first.resolve()
    assert window._documents[1].session.source_path == second.resolve()
    assert window._stack.currentWidget() is window._tabs
    assert window._tabs.tabBar().elideMode() == Qt.TextElideMode.ElideMiddle
    assert window._tabs.tabBar().usesScrollButtons() is True
    assert window._tabs.tabsClosable() is True

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


def test_main_window_copy_action_tracks_focus_and_selection(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    document_path = tmp_path / "copy.pdf"
    document_path.touch()
    window.open_document(document_path)
    window._prompt_search()

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

    document_path = tmp_path / "copy-priority.pdf"
    document_path.touch()
    window.open_document(document_path)
    window._prompt_search()

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

    document_path = tmp_path / "search-events.pdf"
    document_path.touch()
    window.open_document(document_path)
    window._prompt_search()

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
    )

    assert MainWindow._search_progress_text(state) == "索引作成中 2 / 10\uff082ページ失敗\uff09"
    assert MainWindow._search_progress_text(completed) == "索引完了\uff082ページ失敗\uff09"


def test_main_window_copy_action_falls_back_to_pdf_view_when_line_edit_has_no_selection(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    document_path = tmp_path / "copy-fallback.pdf"
    document_path.touch()
    window.open_document(document_path)
    window._prompt_search()

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

    document_path = tmp_path / "real.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=144, height=144)
    with document_path.open("wb") as output:
        writer.write(output)

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
