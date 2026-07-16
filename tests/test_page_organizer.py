from __future__ import annotations

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QListView, QWidget
from pytestqt.qtbot import QtBot

from pdf_workbench.services.page_coordinates import PageMetadata
from pdf_workbench.services.pdf_renderer import DocumentRevision, RenderCacheKey
from pdf_workbench.ui.widgets.page_organizer import PageOrganizer


class _FakeDropEvent:
    def __init__(
        self,
        *,
        source: object,
        mime_data: QMimeData,
        point: QPoint,
    ) -> None:
        self._source = source
        self._mime_data = mime_data
        self._point = QPointF(point)
        self.accepted = False
        self.ignored = False
        self.drop_action: Qt.DropAction | None = None

    def source(self) -> object:
        return self._source

    def mimeData(self) -> QMimeData:
        return self._mime_data

    def position(self) -> QPointF:
        return self._point

    def setDropAction(self, action: Qt.DropAction) -> None:
        self.drop_action = action

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.ignored = True


def _show_organizer(qtbot: QtBot, organizer: PageOrganizer) -> None:
    organizer.resize(220, 640)
    qtbot.addWidget(organizer)
    organizer.show()
    qtbot.waitUntil(organizer.isVisible)


def _row_center(organizer: PageOrganizer, row: int) -> QPoint:
    index = organizer.list_view.model().index(row, 0)
    rect = organizer.list_view.visualRect(index)
    return rect.center()


def _row_upper_point(organizer: PageOrganizer, row: int) -> QPoint:
    index = organizer.list_view.model().index(row, 0)
    rect = organizer.list_view.visualRect(index)
    return QPoint(rect.center().x(), rect.top() + 1)


def _row_lower_point(organizer: PageOrganizer, row: int) -> QPoint:
    index = organizer.list_view.model().index(row, 0)
    rect = organizer.list_view.visualRect(index)
    return QPoint(rect.center().x(), rect.bottom() - 1)


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


def _reorder_mime(rows: tuple[int, ...]) -> QMimeData:
    from pdf_workbench.ui.widgets import page_organizer as organizer_module

    mime_data = QMimeData()
    mime_data.setData(
        organizer_module._REORDER_MIME,
        ",".join(str(row) for row in rows).encode("ascii"),
    )
    return mime_data


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


def test_page_organizer_updates_desired_thumbnails_by_difference_for_large_models(
    qtbot: QtBot,
) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(1000))
    changed_rows: list[int] = []
    organizer.list_view.model().dataChanged.connect(
        lambda top_left, bottom_right, _roles: changed_rows.extend(
            range(top_left.row(), bottom_right.row() + 1)
        )
    )
    organizer.set_selected_page_indexes((2, 4), current_index=4)
    organizer.set_current_page(4)

    organizer.set_desired_thumbnail_pages((0, 1, 2, 3, 4, 5))
    changed_rows.clear()
    organizer.set_desired_thumbnail_pages((500, 501, 502, 503, 504, 505))

    assert organizer.row_count == 1000
    assert organizer.selected_page_indexes == (2, 4)
    assert organizer.current_page_index == 4
    assert len(changed_rows) <= 6
    assert set(changed_rows) <= {0, 1, 2, 3, 4, 5}

    changed_rows.clear()
    organizer.set_desired_thumbnail_pages((500, 501, 502, 503, 504, 505))
    assert changed_rows == []


def test_page_organizer_calculates_insertion_slots_from_drop_position(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(4))

    first_rect = organizer.list_view.visualRect(organizer.list_view.model().index(0, 0))
    last_rect = organizer.list_view.visualRect(organizer.list_view.model().index(3, 0))

    assert (
        organizer.list_view.insertion_slot_for_position(
            QPoint(first_rect.center().x(), first_rect.top() - 8)
        )
        == 0
    )
    assert organizer.list_view.insertion_slot_for_position(_row_upper_point(organizer, 1)) == 1
    assert organizer.list_view.insertion_slot_for_position(_row_lower_point(organizer, 1)) == 2
    assert (
        organizer.list_view.insertion_slot_for_position(
            QPoint(last_rect.center().x(), last_rect.bottom() + 12)
        )
        == 4
    )


def test_page_organizer_drop_emits_sorted_reorder_request_without_reordering_model(
    qtbot: QtBot,
) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(6))
    organizer.set_reordering_enabled(True)
    organizer.set_selected_page_indexes((4, 1, 3), current_index=4)
    captured: list[tuple[tuple[int, ...], int]] = []
    organizer.pages_reorder_requested.connect(
        lambda page_indexes, insertion_slot: captured.append((page_indexes, insertion_slot))
    )
    before_order = tuple(organizer.page_display_text(index) for index in range(organizer.row_count))

    event = _FakeDropEvent(
        source=organizer.list_view,
        mime_data=_reorder_mime((4, 1, 3)),
        point=_row_upper_point(organizer, 5),
    )

    organizer.list_view.dropEvent(event)  # type: ignore[arg-type]

    assert captured == [((1, 3, 4), 5)]
    assert event.accepted is True
    assert event.ignored is False
    assert event.drop_action == Qt.DropAction.MoveAction
    assert (
        tuple(organizer.page_display_text(index) for index in range(organizer.row_count))
        == before_order
    )
    assert organizer.selected_page_indexes == (1, 3, 4)


def test_page_organizer_drop_ignores_noop_and_foreign_payloads(qtbot: QtBot) -> None:
    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(5))
    organizer.set_reordering_enabled(True)
    organizer.set_selected_page_indexes((1, 2), current_index=2)
    captured: list[tuple[tuple[int, ...], int]] = []
    organizer.pages_reorder_requested.connect(
        lambda page_indexes, insertion_slot: captured.append((page_indexes, insertion_slot))
    )

    noop_event = _FakeDropEvent(
        source=organizer.list_view,
        mime_data=_reorder_mime((1, 2)),
        point=_row_upper_point(organizer, 3),
    )
    foreign_event = _FakeDropEvent(
        source=object(),
        mime_data=_reorder_mime((1, 2)),
        point=_row_upper_point(organizer, 0),
    )
    external_payload = _FakeDropEvent(
        source=organizer.list_view,
        mime_data=QMimeData(),
        point=_row_upper_point(organizer, 0),
    )

    organizer.list_view.dropEvent(noop_event)  # type: ignore[arg-type]
    organizer.list_view.dropEvent(foreign_event)  # type: ignore[arg-type]
    organizer.list_view.dropEvent(external_payload)  # type: ignore[arg-type]

    assert captured == []
    assert noop_event.accepted is True
    assert foreign_event.ignored is True
    assert external_payload.ignored is True


def test_page_organizer_start_drag_preserves_multi_selection_and_respects_guards(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    from pdf_workbench.ui.widgets import page_organizer as organizer_module

    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(5))
    organizer.set_reordering_enabled(True)
    organizer.set_selected_page_indexes((1, 3, 4), current_index=4)
    exec_calls: list[Qt.DropAction] = []

    monkeypatch.setattr(
        organizer_module.QDrag,
        "exec",
        lambda self, action: exec_calls.append(action) or action,
    )

    organizer.list_view.startDrag(Qt.DropAction.MoveAction)
    assert exec_calls == [Qt.DropAction.MoveAction]
    assert organizer.selected_page_indexes == (1, 3, 4)

    organizer.set_reordering_enabled(False)
    organizer.list_view.startDrag(Qt.DropAction.MoveAction)
    assert exec_calls == [Qt.DropAction.MoveAction]


def test_page_organizer_large_document_drag_does_not_expand_thumbnail_requests(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
) -> None:
    from pdf_workbench.ui.widgets import page_organizer as organizer_module

    organizer = PageOrganizer()
    _show_organizer(qtbot, organizer)
    organizer.set_document(_metadata(1000))
    organizer.set_reordering_enabled(True)
    visible_requests: list[tuple[int, ...]] = []
    organizer.visible_thumbnail_pages_changed.connect(visible_requests.append)
    organizer.list_view.schedule_visible_thumbnail_update(force=True)
    qtbot.waitUntil(lambda: bool(visible_requests))
    initial_visible = visible_requests[-1]
    visible_requests.clear()
    organizer.set_selected_page_indexes((10, 500, 900), current_index=500)
    exec_calls: list[Qt.DropAction] = []

    monkeypatch.setattr(
        organizer_module.QDrag,
        "exec",
        lambda self, action: exec_calls.append(action) or action,
    )

    organizer.list_view.startDrag(Qt.DropAction.MoveAction)
    QTest.qWait(20)

    assert organizer.row_count == 1000
    assert len(initial_visible) < 20
    assert exec_calls == [Qt.DropAction.MoveAction]
    assert visible_requests
    assert len(visible_requests[-1]) < 20
    assert min(visible_requests[-1]) >= 496
    assert max(visible_requests[-1]) <= 504
    assert organizer.expected_key_count == 0
