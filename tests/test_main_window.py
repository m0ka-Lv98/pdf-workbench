from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QMessageBox
from pytestqt.qtbot import QtBot

from pdf_workbench.ui.main_window import MainWindow
from pdf_workbench.ui.pdf_view import PdfView


@pytest.fixture(autouse=True)
def patch_pdf_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open_document(self: PdfView, path: Path) -> None:
        self._path = path
        self._page_index = 0
        self._page_count = 1
        self.state_changed.emit()

    monkeypatch.setattr(PdfView, "open_document", fake_open_document)


def create_settings(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "app.ini"), QSettings.Format.IniFormat)


def test_main_window_opens_and_closes_multiple_documents(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
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

    assert window.close_document_at(1) is True
    assert window._tabs.count() == 1
    assert window._documents[0].session.source_path == first.resolve()


def test_main_window_requires_confirmation_for_modified_document(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
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
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
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
