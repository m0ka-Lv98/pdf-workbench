from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QListView, QWidget
from pytestqt.qtbot import QtBot

from pdf_workbench.services.page_coordinates import PageMetadata
from pdf_workbench.ui.widgets.page_organizer import PageOrganizer


def _show_organizer(qtbot: QtBot, organizer: PageOrganizer) -> None:
    organizer.resize(220, 640)
    qtbot.addWidget(organizer)
    organizer.show()
    qtbot.waitUntil(organizer.isVisible)


def _row_center(organizer: PageOrganizer, row: int) -> QPoint:
    index = organizer.list_view.model().index(row, 0)
    rect = organizer.list_view.visualRect(index)
    return rect.center()


def _metadata(page_count: int) -> tuple[PageMetadata, ...]:
    return tuple(PageMetadata.from_size(144.0, 200.0) for _ in range(page_count))


def test_page_organizer_exposes_expected_object_names(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(4))

    assert organizer.objectName() == "pageOrganizer"
    assert organizer.findChild(QListView, "pageOrganizerList") is organizer.list_view
    assert organizer.findChild(QWidget, "pageOrganizerHeader") is not None
    assert organizer.findChild(QLabel, "pageOrganizerTitle") is not None
    assert organizer.findChild(QLabel, "pageOrganizerCount") is not None


def test_page_organizer_supports_single_and_range_selection(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(8))

    QTest.mouseClick(
        organizer.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        _row_center(organizer, 2),
    )
    QTest.mouseClick(
        organizer.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ShiftModifier,
        _row_center(organizer, 5),
    )

    assert organizer.selected_page_indexes == (2, 3, 4, 5)
    assert organizer.list_view.currentIndex().row() == 5


def test_page_organizer_supports_control_and_meta_multi_selection(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(8))

    QTest.mouseClick(
        organizer.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        _row_center(organizer, 1),
    )
    QTest.mouseClick(
        organizer.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.ControlModifier,
        _row_center(organizer, 4),
    )
    QTest.mouseClick(
        organizer.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.MetaModifier,
        _row_center(organizer, 6),
    )

    assert organizer.selected_page_indexes == (1, 4, 6)
    assert organizer.list_view.currentIndex().row() == 6


def test_page_organizer_visible_page_signal_prefetches_adjacent_rows(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(40))
    captured: list[tuple[int, ...]] = []
    organizer.visible_thumbnail_pages_changed.connect(lambda indexes: captured.append(indexes))

    scrollbar = organizer.list_view.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum() // 2)
    qtbot.waitUntil(lambda: bool(captured))

    assert captured[-1]
    assert len(captured[-1]) >= len(organizer.visible_page_indexes)
