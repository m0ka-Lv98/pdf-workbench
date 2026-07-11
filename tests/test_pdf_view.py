from __future__ import annotations

import threading
from pathlib import Path

import pytest
from pypdf import PdfWriter
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, Signal
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget
from pytestqt.qtbot import QtBot

from pdf_workbench.services.page_coordinates import PageMetadata, PdfRect
from pdf_workbench.services.pdf_renderer import (
    DocumentMetadata,
    DocumentRevision,
    PageTextIndex,
    PdfRenderService,
    RenderRequest,
    RenderResult,
    TextCharacterBox,
)
from pdf_workbench.ui.pdf_view import PdfView, PlaceholderState


class FakeRenderService(QObject):
    document_loaded = Signal(object, int, object)
    document_failed = Signal(object, int, str)
    render_succeeded = Signal(object)
    render_failed = Signal(object, int, int, str)
    text_page_indexed = Signal(object, object, object)
    text_index_progress = Signal(object, object, int, int, int)
    text_index_completed = Signal(object, object, int, int)
    text_index_failed = Signal(object, object, int, str)

    def __init__(self, metadata: DocumentMetadata) -> None:
        super().__init__()
        self.metadata = metadata
        self.open_calls: list[tuple[str, Path, int, DocumentRevision]] = []
        self.render_requests: list[RenderRequest] = []
        self.close_calls: list[tuple[str, int]] = []
        self.generation_updates: list[tuple[str, int, DocumentRevision]] = []
        self.shutdown_called = False
        self.text_requests: list[tuple[str, int, int, DocumentRevision]] = []

    def open_document(
        self,
        document_id: str,
        path: Path,
        generation: int,
        revision: DocumentRevision,
    ) -> None:
        self.open_calls.append((document_id, path, generation, revision))
        self.document_loaded.emit(document_id, generation, self.metadata)

    def request_render(self, request: RenderRequest) -> None:
        self.render_requests.append(request)

    def close_document(self, document_id: str, generation: int) -> None:
        self.close_calls.append((document_id, generation))

    def update_document_generation(
        self,
        document_id: str,
        generation: int,
        revision: DocumentRevision,
    ) -> None:
        self.generation_updates.append((document_id, generation, revision))

    def shutdown(self, timeout_ms: int = 3000) -> None:
        self.shutdown_called = True

    def emit_text_index(
        self,
        document_id: str,
        revision: DocumentRevision,
        page_index: int,
        text: str,
        boxes: list[PdfRect],
    ) -> None:
        self.text_page_indexed.emit(
            document_id,
            revision,
            PageTextIndex(
                revision=revision,
                page_index=page_index,
                text=text,
                characters=tuple(
                    TextCharacterBox(pdfium_index=index, text=text[index], box=box)
                    for index, box in enumerate(boxes)
                ),
            ),
        )

    def emit_text_progress(
        self,
        document_id: str,
        revision: DocumentRevision,
        indexed_pages: int,
        total_pages: int,
        failed_pages: int,
    ) -> None:
        self.text_index_progress.emit(
            document_id,
            revision,
            indexed_pages,
            total_pages,
            failed_pages,
        )

    def emit_text_completed(
        self,
        document_id: str,
        revision: DocumentRevision,
        indexed_pages: int,
        failed_pages: int,
    ) -> None:
        self.text_index_completed.emit(
            document_id,
            revision,
            indexed_pages,
            failed_pages,
        )

    def emit_text_failure(
        self,
        document_id: str,
        revision: DocumentRevision,
        page_index: int,
        message: str,
    ) -> None:
        self.text_index_failed.emit(document_id, revision, page_index, message)


class RecordingBackend:
    def __init__(self) -> None:
        self.render_calls: list[tuple[int, float, int, float]] = []
        self.close_call_count = 0
        self.operation_thread_ids: list[int] = []
        self.metadata_thread_ids: list[int] = []
        self.render_started = threading.Event()

    def page_count(self) -> int:
        self.operation_thread_ids.append(threading.get_ident())
        return 1

    def page_metadata(self, page_index: int) -> PageMetadata:
        self.metadata_thread_ids.append(threading.get_ident())
        return PageMetadata.from_size(144.0, 200.0)

    def render_page(
        self,
        page_index: int,
        logical_zoom: float,
        rotation: int,
        device_pixel_ratio: float,
    ) -> QImage:
        self.operation_thread_ids.append(threading.get_ident())
        self.render_calls.append((page_index, logical_zoom, rotation, device_pixel_ratio))
        self.render_started.set()
        image = QImage(144, 200, QImage.Format.Format_ARGB32)
        image.fill(0xFFFFFFFF)
        image.setDevicePixelRatio(device_pixel_ratio)
        return image

    def close(self) -> None:
        self.operation_thread_ids.append(threading.get_ident())
        self.close_call_count += 1


def create_recording_service() -> tuple[PdfRenderService, RecordingBackend, list[Path]]:
    backend = RecordingBackend()
    factory_calls: list[Path] = []

    def factory(path: Path) -> RecordingBackend:
        factory_calls.append(path)
        return backend

    service = PdfRenderService(backend_factory=factory)
    return service, backend, factory_calls


def create_pdf(path: Path, page_count: int) -> Path:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=144, height=200)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def create_stub_pdf(path: Path) -> Path:
    path.write_bytes(b"%PDF-1.7\n")
    return path


def create_text_pdf(path: Path, pages: list[str], rotations: list[int] | None = None) -> Path:
    rotations = rotations or [0] * len(pages)
    if len(rotations) != len(pages):
        raise ValueError("rotations must match pages")

    objects: list[bytes] = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        f"2 0 obj << /Type /Pages /Count {len(pages)} /Kids [".encode("ascii"),
    ]
    page_object_numbers = [3 + index * 2 for index in range(len(pages))]
    content_object_numbers = [4 + index * 2 for index in range(len(pages))]
    objects[1] += b" ".join(f"{number} 0 R".encode("ascii") for number in page_object_numbers)
    objects[1] += b"] >> endobj\n"
    for page_number, content_number, text, rotation in zip(
        page_object_numbers,
        content_object_numbers,
        pages,
        rotations,
        strict=True,
    ):
        content = f"BT /F1 18 Tf 40 100 Td ({text}) Tj ET".encode("latin-1")
        page_dict = (
            f"{page_number} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Resources << /Font << /F1 100 0 R >> >> /Contents {content_number} 0 R"
        )
        if rotation:
            page_dict += f" /Rotate {rotation}"
        page_dict += " >> endobj\n"
        objects.append(page_dict.encode("ascii"))
        objects.append(
            f"{content_number} 0 obj << /Length {len(content)} >> stream\n".encode("ascii")
            + content
            + b"\nendstream\nendobj\n"
        )
    objects.append(b"100 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)
    xref_start = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(pdf)
    return path


def create_metadata(path: Path, page_count: int) -> DocumentMetadata:
    return DocumentMetadata(
        revision=DocumentRevision.from_path(path),
        pages=tuple(PageMetadata.from_size(144.0, 200.0) for _ in range(page_count)),
    )


def make_render_result(request: RenderRequest) -> RenderResult:
    image = QImage(128, 160, QImage.Format.Format_ARGB32)
    image.fill(0xFFAA7733)
    image.setDevicePixelRatio(request.device_pixel_ratio)
    return RenderResult(
        document_id=request.document_id,
        generation=request.generation,
        page_index=request.page_index,
        image=image,
        cache_key=request.cache_key,
    )


def show_view(qtbot: QtBot, view: PdfView) -> QWidget:
    wrapper = QWidget()
    wrapper.resize(520, 420)
    if view.parentWidget() is not None:
        raise AssertionError("view should not be parented for this helper")
    view.setParent(wrapper)
    view.setGeometry(0, 0, 520, 420)
    wrapper.resize(520, 420)
    wrapper.show()
    qtbot.addWidget(wrapper)
    qtbot.waitUntil(wrapper.isVisible)
    return wrapper


def page_point_for_box(view: PdfView, page_index: int, box: PdfRect) -> QPoint:
    page = view._canvas.pages[page_index]
    rect = page._coordinate_mapper().pdf_to_view_rect(box)
    point = page.page_content_rect().topLeft() + rect.center()
    return point.toPoint()


def union_rect(*boxes: PdfRect) -> PdfRect:
    return PdfRect(
        left=min(box.left for box in boxes),
        bottom=min(box.bottom for box in boxes),
        right=max(box.right for box in boxes),
        top=max(box.top for box in boxes),
    )


def send_page_mouse_event(
    page: QWidget,
    event_type: QEvent.Type,
    local_point: QPoint,
    *,
    global_point: QPoint | None = None,
    buttons: Qt.MouseButton = Qt.MouseButton.LeftButton,
) -> None:
    actual_global = global_point or page.mapToGlobal(local_point)
    if event_type in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        button = Qt.MouseButton.LeftButton
    else:
        button = Qt.MouseButton.NoButton
    event = QMouseEvent(
        event_type,
        QPointF(local_point),
        QPointF(actual_global),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(page, event)


def test_visible_and_adjacent_pages_only_are_requested(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "visible.pdf", 20)
    service = FakeRenderService(create_metadata(document_path, 20))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 20)
    qtbot.waitUntil(
        lambda: view._canvas.pages[-1].geometry().top() > view._canvas.pages[0].geometry().top()
    )
    view._request_visible_pages()

    visible = view._visible_pages()
    requested_pages = [request.page_index for request in service.render_requests]
    expected_pages = set(visible)
    if visible[0] > 0:
        expected_pages.add(visible[0] - 1)
    if visible[-1] + 1 < view.page_count:
        expected_pages.add(visible[-1] + 1)

    assert set(requested_pages) == expected_pages
    assert wrapper.isVisible()


def test_opening_large_document_does_not_queue_all_pages(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_stub_pdf(tmp_path / "large.pdf")
    service = FakeRenderService(create_metadata(document_path, 1000))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1000)
    qtbot.waitUntil(
        lambda: view._canvas.pages[-1].geometry().top() > view._canvas.pages[0].geometry().top()
    )
    view._request_visible_pages()

    assert 0 < len(service.render_requests) < 10


def test_zoom_change_discards_old_generation_results(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "zoom.pdf", 5)
    service = FakeRenderService(create_metadata(document_path, 5))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 5)
    qtbot.waitUntil(
        lambda: view._canvas.pages[-1].geometry().top() > view._canvas.pages[0].geometry().top()
    )
    view._request_visible_pages()
    stale_request = service.render_requests[0]

    view.set_zoom(2.0)
    assert service.generation_updates[-1][1] == 2
    view._request_visible_pages()
    fresh_request = service.render_requests[-1]

    service.render_succeeded.emit(make_render_result(stale_request))
    assert view._canvas.pages[stale_request.page_index].state != PlaceholderState.DISPLAYED

    service.render_succeeded.emit(make_render_result(fresh_request))
    assert view._canvas.pages[fresh_request.page_index].state == PlaceholderState.DISPLAYED


def test_rotation_change_notifies_worker_generation(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "rotation.pdf", 3)
    service = FakeRenderService(create_metadata(document_path, 3))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 3)
    qtbot.waitUntil(
        lambda: view._canvas.pages[-1].geometry().top() > view._canvas.pages[0].geometry().top()
    )

    view.set_rotation(90)

    assert service.generation_updates[-1][1] == 2
    assert service.generation_updates[-1][0] == view._document_id
    assert view.page_content_rect(0).height() == pytest.approx(
        view._canvas.pages[0].page_content_rect().height()
    )
    assert view.page_content_rect(0).width() == pytest.approx(
        view._canvas.pages[0].page_content_rect().width()
    )


def test_fast_scroll_does_not_apply_old_offscreen_result(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "scroll.pdf", 12)
    service = FakeRenderService(create_metadata(document_path, 12))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 12)
    qtbot.waitUntil(
        lambda: view._canvas.pages[-1].geometry().top() > view._canvas.pages[0].geometry().top()
    )
    view._request_visible_pages()
    first_request = service.render_requests[0]

    view.set_page(8)
    view._request_visible_pages()

    service.render_succeeded.emit(make_render_result(first_request))
    assert view._canvas.pages[first_request.page_index].state != PlaceholderState.DISPLAYED


def test_closing_view_ignores_late_worker_results(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "close.pdf", 4)
    service = FakeRenderService(create_metadata(document_path, 4))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 4)
    qtbot.waitUntil(
        lambda: view._canvas.pages[-1].geometry().top() > view._canvas.pages[0].geometry().top()
    )
    view._request_visible_pages()
    request = service.render_requests[0]

    view.shutdown()
    service.render_succeeded.emit(make_render_result(request))

    assert service.close_calls
    assert view._canvas.pages[request.page_index].state != PlaceholderState.DISPLAYED


def test_previous_and_next_navigation_scroll_to_target_page(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "nav.pdf", 10)
    service = FakeRenderService(create_metadata(document_path, 10))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 10)
    qtbot.waitUntil(
        lambda: view._canvas.pages[-1].geometry().top() > view._canvas.pages[0].geometry().top()
    )
    view.set_page(4)

    assert view.page_index == 4

    view.set_page(3)

    assert view.page_index == 3


def test_page_content_rect_tracks_rotation_and_zoom(qtbot: QtBot, tmp_path: Path) -> None:
    document_path = create_pdf(tmp_path / "rect.pdf", 1)
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)
    qtbot.waitUntil(lambda: view.page_content_rect(0).width() > 0)

    portrait_rect = view.page_content_rect(0)
    placeholder = view._canvas.pages[0]
    local_rect = placeholder.page_content_rect()
    assert local_rect.width() == pytest.approx(portrait_rect.width())
    assert local_rect.height() == pytest.approx(portrait_rect.height())
    assert local_rect.left() == pytest.approx((placeholder.width() - local_rect.width()) / 2.0)
    assert local_rect.top() == pytest.approx((placeholder.height() - local_rect.height()) / 2.0)
    view.set_rotation(90)
    landscape_rect = view.page_content_rect(0)
    view.set_zoom(2.0)
    zoomed_rect = view.page_content_rect(0)

    assert portrait_rect.width() != landscape_rect.width()
    assert zoomed_rect.width() > landscape_rect.width()
    assert _wrapper.isVisible()


def test_page_placeholder_minimum_extent_and_centering(qtbot: QtBot, tmp_path: Path) -> None:
    document_path = create_pdf(tmp_path / "small.pdf", 1)
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)
    qtbot.waitUntil(lambda: view._canvas.pages[0].width() >= 200)

    placeholder = view._canvas.pages[0]
    content_rect = placeholder.page_content_rect()
    assert placeholder.width() >= 200
    assert placeholder.height() >= 200
    assert content_rect.left() == pytest.approx((placeholder.width() - content_rect.width()) / 2.0)
    assert content_rect.top() == pytest.approx((placeholder.height() - content_rect.height()) / 2.0)
    assert _wrapper.isVisible()


def test_page_view_validation_rejects_invalid_rotation_and_zoom(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "validation.pdf", 1)
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)

    with pytest.raises(ValueError):
        view.set_rotation(45)
    with pytest.raises(ValueError):
        view.set_rotation(360)
    with pytest.raises(ValueError):
        view.set_zoom(float("nan"))
    with pytest.raises(ValueError):
        view.set_zoom(float("inf"))
    assert _wrapper.isVisible()


def test_real_render_service_shuts_down_without_thread_leak(qtbot: QtBot) -> None:
    service = PdfRenderService()

    service.shutdown()
    qtbot.waitUntil(lambda: not service._thread.isRunning())

    assert not service._thread.isRunning()


def test_real_render_service_rerenders_after_zoom_change(qtbot: QtBot, tmp_path: Path) -> None:
    document_path = create_pdf(tmp_path / "zoom-real.pdf", 1)
    service, backend, factory_calls = create_recording_service()
    view = PdfView(render_service=service, debounce_interval_ms=0)
    wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: len(backend.render_calls) >= 1)

    view.set_zoom(2.0)
    qtbot.waitUntil(lambda: len(backend.render_calls) >= 2)

    assert backend.render_calls[-1] == (0, 2.0, 0, 1.0)
    assert len(factory_calls) == 1
    assert backend.close_call_count == 0
    assert threading.get_ident() not in backend.operation_thread_ids

    service.shutdown()
    qtbot.waitUntil(lambda: not service._thread.isRunning())
    assert not service._thread.isRunning()
    assert wrapper.isVisible()


def test_real_render_service_rerenders_after_rotation_change(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "rotation-real.pdf", 1)
    service, backend, factory_calls = create_recording_service()
    view = PdfView(render_service=service, debounce_interval_ms=0)
    wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: len(backend.render_calls) >= 1)

    view.set_rotation(90)
    qtbot.waitUntil(lambda: len(backend.render_calls) >= 2)

    assert backend.render_calls[-1] == (0, 1.5, 90, 1.0)
    assert len(factory_calls) == 1
    assert backend.close_call_count == 0

    service.shutdown()
    qtbot.waitUntil(lambda: not service._thread.isRunning())
    assert not service._thread.isRunning()
    assert wrapper.isVisible()


def test_text_search_highlights_matches_and_navigates_between_results(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "search.pdf", 1)
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)

    page_index = 0
    service.emit_text_index(
        view._document_id,
        create_metadata(document_path, 1).revision,
        page_index,
        "Hello world hello",
        [
            PdfRect(10.0, 10.0, 30.0, 30.0),
            PdfRect(34.0, 10.0, 54.0, 30.0),
            PdfRect(58.0, 10.0, 78.0, 30.0),
            PdfRect(82.0, 10.0, 102.0, 30.0),
            PdfRect(106.0, 10.0, 126.0, 30.0),
            PdfRect(130.0, 10.0, 150.0, 30.0),
            PdfRect(154.0, 10.0, 174.0, 30.0),
            PdfRect(178.0, 10.0, 198.0, 30.0),
            PdfRect(202.0, 10.0, 222.0, 30.0),
            PdfRect(226.0, 10.0, 246.0, 30.0),
            PdfRect(250.0, 10.0, 270.0, 30.0),
            PdfRect(274.0, 10.0, 294.0, 30.0),
            PdfRect(298.0, 10.0, 318.0, 30.0),
            PdfRect(322.0, 10.0, 342.0, 30.0),
            PdfRect(346.0, 10.0, 366.0, 30.0),
            PdfRect(370.0, 10.0, 390.0, 30.0),
            PdfRect(394.0, 10.0, 414.0, 30.0),
        ],
    )

    assert view.search("hello") == 2
    assert view._current_match_index == 0
    assert len(view._canvas.pages[0]._current_match_boxes) == 1
    assert view.next_match() is True
    assert view._current_match_index == 1
    assert view.previous_match() is True
    assert view._current_match_index == 0
    assert _wrapper.isVisible()


def test_text_selection_and_copy_merge_adjacent_boxes_into_single_overlay_run(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "selection.pdf", 1)
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)

    boxes = [
        PdfRect(10.0, 10.0, 20.0, 30.0),
        PdfRect(22.0, 10.0, 32.0, 30.0),
        PdfRect(34.0, 10.0, 44.0, 30.0),
    ]
    service.emit_text_index(
        view._document_id,
        create_metadata(document_path, 1).revision,
        0,
        "abc",
        boxes,
    )
    page = view._canvas.pages[0]
    start_point = page_point_for_box(view, 0, boxes[0])
    end_point = page_point_for_box(view, 0, boxes[2])
    send_page_mouse_event(page, QEvent.Type.MouseButtonPress, start_point)
    send_page_mouse_event(page, QEvent.Type.MouseMove, end_point)
    send_page_mouse_event(
        page,
        QEvent.Type.MouseButtonRelease,
        end_point,
        buttons=Qt.MouseButton.NoButton,
    )

    assert view.selected_text == "abc"
    assert view.copy_selected_text() is True
    assert QApplication.clipboard().text() == "abc"
    assert len(view._canvas.pages[0]._selection_boxes) == 1
    assert view._canvas.pages[0]._selection_boxes[0].box == union_rect(*boxes)
    assert QWidget.mouseGrabber() is None
    assert _wrapper.isVisible()


def test_text_selection_splits_overlay_runs_across_lines(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "selection-lines.pdf", 1)
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)

    boxes = [
        PdfRect(10.0, 50.0, 20.0, 70.0),
        PdfRect(22.0, 50.0, 32.0, 70.0),
        PdfRect(10.0, 20.0, 20.0, 40.0),
        PdfRect(22.0, 20.0, 32.0, 40.0),
    ]
    service.emit_text_index(
        view._document_id,
        create_metadata(document_path, 1).revision,
        0,
        "ab\ncd",
        boxes,
    )
    page = view._canvas.pages[0]
    start_point = page_point_for_box(view, 0, boxes[0])
    end_point = page_point_for_box(view, 0, boxes[3])
    send_page_mouse_event(page, QEvent.Type.MouseButtonPress, start_point)
    send_page_mouse_event(page, QEvent.Type.MouseMove, end_point)
    send_page_mouse_event(
        page,
        QEvent.Type.MouseButtonRelease,
        end_point,
        buttons=Qt.MouseButton.NoButton,
    )

    assert len(page._selection_boxes) == 2


def test_text_selection_does_not_merge_distant_columns(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "selection-columns.pdf", 1)
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)

    boxes = [
        PdfRect(10.0, 50.0, 20.0, 70.0),
        PdfRect(22.0, 50.0, 32.0, 70.0),
        PdfRect(120.0, 50.0, 130.0, 70.0),
        PdfRect(132.0, 50.0, 142.0, 70.0),
    ]
    service.emit_text_index(
        view._document_id,
        create_metadata(document_path, 1).revision,
        0,
        "abcd",
        boxes,
    )

    assert view.search("abcd") == 1
    assert len(view._canvas.pages[0]._match_boxes) == 2


def test_live_mouse_selection_ignores_page_margin_press(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "margin-selection.pdf", 1)
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)
    service.emit_text_index(
        view._document_id,
        create_metadata(document_path, 1).revision,
        0,
        "abc",
        [
            PdfRect(10.0, 10.0, 20.0, 30.0),
            PdfRect(22.0, 10.0, 32.0, 30.0),
            PdfRect(34.0, 10.0, 44.0, 30.0),
        ],
    )
    page = view._canvas.pages[0]
    margin_point = QPoint(2, 2)

    send_page_mouse_event(page, QEvent.Type.MouseButtonPress, margin_point)

    assert view.selected_text == ""
    assert QWidget.mouseGrabber() is None
    assert _wrapper.isVisible()


def test_live_mouse_selection_crosses_pages_with_global_positions(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "cross-pages.pdf", 2)
    service = FakeRenderService(create_metadata(document_path, 2))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 2)
    service.emit_text_index(
        view._document_id,
        create_metadata(document_path, 2).revision,
        0,
        "abc",
        [
            PdfRect(10.0, 10.0, 20.0, 30.0),
            PdfRect(22.0, 10.0, 32.0, 30.0),
            PdfRect(34.0, 10.0, 44.0, 30.0),
        ],
    )
    service.emit_text_index(
        view._document_id,
        create_metadata(document_path, 2).revision,
        1,
        "def",
        [
            PdfRect(10.0, 10.0, 20.0, 30.0),
            PdfRect(22.0, 10.0, 32.0, 30.0),
            PdfRect(34.0, 10.0, 44.0, 30.0),
        ],
    )
    first_page = view._canvas.pages[0]
    second_page = view._canvas.pages[1]
    start_point = page_point_for_box(view, 0, PdfRect(10.0, 10.0, 20.0, 30.0))
    second_local = page_point_for_box(view, 1, PdfRect(34.0, 10.0, 44.0, 30.0))
    second_global = second_page.mapToGlobal(second_local)

    send_page_mouse_event(first_page, QEvent.Type.MouseButtonPress, start_point)
    send_page_mouse_event(
        first_page,
        QEvent.Type.MouseMove,
        first_page.mapFromGlobal(second_global),
        global_point=second_global,
    )
    send_page_mouse_event(
        first_page,
        QEvent.Type.MouseButtonRelease,
        first_page.mapFromGlobal(second_global),
        global_point=second_global,
        buttons=Qt.MouseButton.NoButton,
    )

    assert view.selected_text == "abc\ndef"
    assert len(view._canvas.pages[0]._selection_boxes) == 1
    assert len(view._canvas.pages[1]._selection_boxes) == 1
    assert QWidget.mouseGrabber() is None
    assert _wrapper.isVisible()


def test_text_index_progress_updates_search_state_and_ignores_stale_revision(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "progress.pdf", 1)
    metadata = create_metadata(document_path, 1)
    service = FakeRenderService(metadata)
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)

    service.emit_text_progress(view._document_id, metadata.revision, 0, 10, 2)
    state = view.search_state
    assert state.failed_pages == 2
    assert state.total_pages == 10
    assert state.indexing_completed is False

    other_document = create_pdf(tmp_path / "progress-stale.pdf", 1)
    other_revision = create_metadata(other_document, 1).revision
    service.emit_text_progress(view._document_id, other_revision, 10, 10, 10)

    stale_state = view.search_state
    assert stale_state.failed_pages == 2
    assert stale_state.indexed_pages == 0

    service.emit_text_completed(view._document_id, metadata.revision, 8, 2)
    completed_state = view.search_state
    assert completed_state.indexing_completed is True
    assert completed_state.failed_pages == 2
    assert _wrapper.isVisible()


def test_real_text_pdf_indexes_search_terms_and_survives_rotation(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_text_pdf(tmp_path / "real-text.pdf", ["Hello PDF Workbench"], [90])
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)
    service.emit_text_index(
        view._document_id,
        create_metadata(document_path, 1).revision,
        0,
        "Hello PDF Workbench",
        [
            PdfRect(10.0, 10.0, 18.0, 26.0),
            PdfRect(20.0, 10.0, 28.0, 26.0),
            PdfRect(30.0, 10.0, 38.0, 26.0),
            PdfRect(40.0, 10.0, 48.0, 26.0),
            PdfRect(50.0, 10.0, 58.0, 26.0),
            PdfRect(60.0, 10.0, 68.0, 26.0),
            PdfRect(70.0, 10.0, 78.0, 26.0),
            PdfRect(80.0, 10.0, 88.0, 26.0),
            PdfRect(90.0, 10.0, 98.0, 26.0),
            PdfRect(100.0, 10.0, 108.0, 26.0),
            PdfRect(110.0, 10.0, 118.0, 26.0),
            PdfRect(120.0, 10.0, 128.0, 26.0),
            PdfRect(130.0, 10.0, 138.0, 26.0),
            PdfRect(140.0, 10.0, 148.0, 26.0),
            PdfRect(150.0, 10.0, 158.0, 26.0),
            PdfRect(160.0, 10.0, 168.0, 26.0),
            PdfRect(170.0, 10.0, 178.0, 26.0),
            PdfRect(180.0, 10.0, 188.0, 26.0),
            PdfRect(190.0, 10.0, 198.0, 26.0),
        ],
    )

    assert view.search("PDF") == 1
    assert view.next_match() is True
    assert len(view._canvas.pages[0]._current_match_boxes) == 1
    assert _wrapper.isVisible()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_selection_overlay_runs_preserve_pdf_coordinates_across_rotation(
    qtbot: QtBot,
    tmp_path: Path,
    rotation: int,
) -> None:
    document_path = create_pdf(tmp_path / f"selection-rotation-{rotation}.pdf", 1)
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)
    boxes = [
        PdfRect(10.0, 10.0, 20.0, 30.0),
        PdfRect(22.0, 10.0, 32.0, 30.0),
        PdfRect(34.0, 10.0, 44.0, 30.0),
    ]
    service.emit_text_index(
        view._document_id,
        create_metadata(document_path, 1).revision,
        0,
        "abc",
        boxes,
    )
    view.set_rotation(rotation)
    page = view._canvas.pages[0]
    start_point = page_point_for_box(view, 0, boxes[0])
    end_point = page_point_for_box(view, 0, boxes[2])
    send_page_mouse_event(page, QEvent.Type.MouseButtonPress, start_point)
    send_page_mouse_event(page, QEvent.Type.MouseMove, end_point)
    send_page_mouse_event(
        page,
        QEvent.Type.MouseButtonRelease,
        end_point,
        buttons=Qt.MouseButton.NoButton,
    )

    assert page._selection_boxes[0].box == union_rect(*boxes)


def test_selection_overlay_runs_preserve_pdf_coordinates_after_zoom_change(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "selection-zoom.pdf", 1)
    service = FakeRenderService(create_metadata(document_path, 1))
    view = PdfView(render_service=service, debounce_interval_ms=0)
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 1)
    boxes = [
        PdfRect(10.0, 10.0, 20.0, 30.0),
        PdfRect(22.0, 10.0, 32.0, 30.0),
        PdfRect(34.0, 10.0, 44.0, 30.0),
    ]
    service.emit_text_index(
        view._document_id,
        create_metadata(document_path, 1).revision,
        0,
        "abc",
        boxes,
    )
    view.set_zoom(2.5)
    page = view._canvas.pages[0]
    start_point = page_point_for_box(view, 0, boxes[0])
    end_point = page_point_for_box(view, 0, boxes[2])
    send_page_mouse_event(page, QEvent.Type.MouseButtonPress, start_point)
    send_page_mouse_event(page, QEvent.Type.MouseMove, end_point)
    send_page_mouse_event(
        page,
        QEvent.Type.MouseButtonRelease,
        end_point,
        buttons=Qt.MouseButton.NoButton,
    )

    assert page._selection_boxes[0].box == union_rect(*boxes)
