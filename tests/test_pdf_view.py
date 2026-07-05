from __future__ import annotations

import threading
from pathlib import Path

from pypdf import PdfWriter
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from pdf_workbench.services.pdf_renderer import (
    DocumentMetadata,
    DocumentRevision,
    PageMetadata,
    PdfRenderService,
    RenderRequest,
    RenderResult,
)
from pdf_workbench.ui.pdf_view import PdfView, PlaceholderState


class FakeRenderService(QObject):
    document_loaded = Signal(object, int, object)
    document_failed = Signal(object, int, str)
    render_succeeded = Signal(object)
    render_failed = Signal(object, int, int, str)

    def __init__(self, metadata: DocumentMetadata) -> None:
        super().__init__()
        self.metadata = metadata
        self.open_calls: list[tuple[str, Path, int, DocumentRevision]] = []
        self.render_requests: list[RenderRequest] = []
        self.close_calls: list[tuple[str, int]] = []
        self.generation_updates: list[tuple[str, int, DocumentRevision]] = []
        self.shutdown_called = False

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
        return PageMetadata(144.0, 200.0)

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


def create_metadata(path: Path, page_count: int) -> DocumentMetadata:
    return DocumentMetadata(
        revision=DocumentRevision.from_path(path),
        pages=tuple(PageMetadata(144.0, 200.0) for _ in range(page_count)),
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
    assert view.page_content_rect(0).height() == view._canvas.pages[0].height()
    assert view.page_content_rect(0).width() == view._canvas.pages[0].width()


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
    view.set_rotation(90)
    landscape_rect = view.page_content_rect(0)
    view.set_zoom(2.0)
    zoomed_rect = view.page_content_rect(0)

    assert portrait_rect.width() != landscape_rect.width()
    assert zoomed_rect.width() > landscape_rect.width()
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
