from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QListView, QWidget
from pytestqt.qtbot import QtBot

from pdf_workbench.services.page_coordinates import PageMetadata
from pdf_workbench.services.pdf_renderer import DocumentRevision, RenderCacheKey
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


def _revision(tmp_path, name: str = "organizer.pdf") -> DocumentRevision:
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n")
    return DocumentRevision.from_path(path)


def _image(width: int = 48, height: int = 64) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)
    return image


def test_page_organizer_exposes_expected_object_names(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(4))

    assert organizer.objectName() == "pageOrganizer"
    assert organizer.findChild(QListView, "pageOrganizerList") is organizer.list_view
    assert organizer.findChild(QWidget, "pageOrganizerHeader") is not None
    assert organizer.findChild(QLabel, "pageOrganizerTitle") is not None
    assert organizer.findChild(QLabel, "pageOrganizerCount") is not None


def test_page_organizer_initializes_row_count_display_text_and_selection(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(4))

    assert organizer.row_count == 4
    assert organizer.page_display_text(0) == "Page 1"
    assert organizer.page_display_text(3) == "Page 4"
    assert organizer.current_page_index == 0
    assert organizer.selected_page_indexes == (0,)
    assert organizer.list_view.currentIndex().row() == 0


def test_page_organizer_clear_resets_selection_cache_and_rows(
    qtbot: QtBot,
    tmp_path,
) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(3))
    revision = _revision(tmp_path)
    key = RenderCacheKey(revision, 0, 0.25, 0, 1.0)
    organizer.set_desired_thumbnail_pages((0,))
    assert organizer.prepare_thumbnail_request(0, key, rendering=False) is True
    assert organizer.apply_thumbnail(0, key, _image()) is True
    selections: list[tuple[int, ...]] = []
    organizer.page_selection_changed.connect(lambda value: selections.append(value))

    organizer.clear()

    assert organizer.row_count == 0
    assert organizer.selected_page_indexes == ()
    assert organizer.expected_key_count == 0
    assert organizer.thumbnail_cache_item_count == 0
    assert selections[-1] == ()


def test_page_organizer_filters_invalid_duplicate_selection_indexes(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(5))

    organizer.set_selected_page_indexes((-1, 1, 1, 8, 3), current_index=9)

    assert organizer.selected_page_indexes == (1, 3)
    assert organizer.list_view.currentIndex().row() == 3


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


def test_page_organizer_supports_keyboard_navigation_and_shift_range(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(8))
    organizer.list_view.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(organizer.list_view.hasFocus)

    QTest.keyClick(organizer.list_view, Qt.Key.Key_Down)
    QTest.keyClick(organizer.list_view, Qt.Key.Key_Down)
    QTest.keyClick(organizer.list_view, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)

    assert organizer.list_view.currentIndex().row() == 3
    assert organizer.selected_page_indexes == (2, 3)


def test_page_organizer_programmatic_current_sync_preserves_selection_without_navigation(
    qtbot: QtBot,
) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(8))
    organizer.set_selected_page_indexes((1, 3, 5), current_index=5)
    requested: list[int] = []
    organizer.page_requested.connect(requested.append)

    organizer.set_current_page(6)

    assert organizer.selected_page_indexes == (1, 3, 5)
    assert organizer.list_view.currentIndex().row() == 6
    assert requested == []


def test_page_organizer_user_current_change_emits_single_navigation_request(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(8))
    requested: list[int] = []
    organizer.page_requested.connect(requested.append)

    QTest.mouseClick(
        organizer.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        _row_center(organizer, 4),
    )

    assert requested == [4]
    assert organizer.list_view.currentIndex().row() == 4


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
    assert len(captured[-1]) < 20
    for page_index in captured[-1]:
        assert min(abs(page_index - row) for row in organizer.visible_page_indexes) <= 2


def test_page_organizer_deduplicates_identical_visible_thumbnail_notifications(
    qtbot: QtBot,
) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(40))
    captured: list[tuple[int, ...]] = []
    organizer.visible_thumbnail_pages_changed.connect(lambda indexes: captured.append(indexes))

    organizer.schedule_visible_thumbnail_update(force=True)
    qtbot.waitUntil(lambda: len(captured) == 1)
    organizer.schedule_visible_thumbnail_update()
    QTest.qWait(20)

    assert len(captured) == 1


def test_page_organizer_rejects_late_results_outside_desired_pages(
    qtbot: QtBot,
    tmp_path,
) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(4))
    revision = _revision(tmp_path)
    key = RenderCacheKey(revision, 1, 0.25, 0, 1.0)

    organizer.set_desired_thumbnail_pages((1,))
    assert organizer.prepare_thumbnail_request(1, key, rendering=False) is True
    organizer.set_desired_thumbnail_pages(())

    assert organizer.apply_thumbnail(1, key, _image()) is False
    assert organizer.apply_thumbnail_failure(1, key, "late failure") is False
    assert organizer.expected_key_count == 0


def test_page_organizer_uses_cache_hit_without_duplicate_request(
    qtbot: QtBot,
    tmp_path,
) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(3))
    revision = _revision(tmp_path)
    key = RenderCacheKey(revision, 1, 0.25, 0, 1.0)
    organizer.set_desired_thumbnail_pages((1,))

    assert organizer.prepare_thumbnail_request(1, key, rendering=False) is True
    assert organizer.apply_thumbnail(1, key, _image()) is True
    assert organizer.prepare_thumbnail_request(1, key, rendering=False) is False
    assert organizer.has_thumbnail_image(1) is True
    assert organizer.thumbnail_cache_item_count == 1


def test_page_organizer_eviction_returns_placeholder_for_evicted_row(
    qtbot: QtBot,
    tmp_path,
) -> None:
    from pdf_workbench.ui.widgets.page_organizer import ThumbnailImageCache

    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(3))
    organizer._thumbnail_cache = ThumbnailImageCache(max_items=1, max_bytes=1024 * 1024)
    revision = _revision(tmp_path)
    first_key = RenderCacheKey(revision, 0, 0.25, 0, 1.0)
    second_key = RenderCacheKey(revision, 1, 0.25, 0, 1.0)
    organizer.set_desired_thumbnail_pages((0, 1))

    assert organizer.prepare_thumbnail_request(0, first_key, rendering=False) is True
    assert organizer.apply_thumbnail(0, first_key, _image()) is True
    assert organizer.prepare_thumbnail_request(1, second_key, rendering=False) is True
    assert organizer.apply_thumbnail(1, second_key, _image()) is True

    assert organizer.thumbnail_cache_item_count == 1
    assert organizer.has_thumbnail_image(0) is False
    assert organizer.thumbnail_state(0) == "not_requested"
    assert organizer.has_thumbnail_image(1) is True
