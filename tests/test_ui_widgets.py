from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from pdf_workbench.ui.widgets.document_toolbar import DocumentToolbar, ToolbarState
from pdf_workbench.ui.widgets.empty_state import EmptyState


def test_document_toolbar_updates_state_and_emits_signals(qtbot: QtBot) -> None:
    toolbar = DocumentToolbar()
    qtbot.addWidget(toolbar)

    toolbar.setState(ToolbarState(True, 2, 8, 1.25))

    assert toolbar.page_field.value() == 3
    assert toolbar.page_field.suffix() == " / 8"
    assert toolbar.zoom_field.currentText() == "125%"
    assert toolbar.previous_button.isEnabled()
    assert toolbar.next_button.isEnabled()

    page_requests: list[int] = []
    zoom_requests: list[float] = []
    toolbar.page_requested.connect(page_requests.append)
    toolbar.zoom_requested.connect(zoom_requests.append)

    toolbar.page_field.setValue(4)
    toolbar.zoom_field.setCurrentText("150%")

    assert page_requests == [3]
    assert zoom_requests == [1.5]


def test_empty_state_shows_recent_files_and_emits_selection(qtbot: QtBot, tmp_path: Path) -> None:
    empty_state = EmptyState()
    qtbot.addWidget(empty_state)

    file_a = tmp_path / "a.pdf"
    file_b = tmp_path / "b.pdf"
    empty_state.set_recent_files([file_a, file_b])

    buttons = empty_state.findChildren(type(empty_state.open_button))
    recent_buttons = [button for button in buttons if button.text() in {file_a.name, file_b.name}]

    assert len(recent_buttons) == 2

    requested: list[Path] = []
    empty_state.recent_file_requested.connect(requested.append)
    qtbot.mouseClick(recent_buttons[0], Qt.MouseButton.LeftButton)

    assert requested == [file_a]
