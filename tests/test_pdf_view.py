from __future__ import annotations

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

    def shutdown(self, timeout_ms: int = 3000) -> None:
        self.shutdown_called = True


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
    view._request_visible_pages()
    fresh_request = service.render_requests[-1]

    service.render_succeeded.emit(make_render_result(stale_request))
    assert view._canvas.pages[stale_request.page_index].state != PlaceholderState.DISPLAYED

    service.render_succeeded.emit(make_render_result(fresh_request))
    assert view._canvas.pages[fresh_request.page_index].state == PlaceholderState.DISPLAYED


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


def test_background_rendering_integration_uses_real_pdfium(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_pdf(tmp_path / "integration.pdf", 2)
    view = PdfView()
    _wrapper = show_view(qtbot, view)

    view.open_document(document_path)
    qtbot.waitUntil(lambda: view.page_count == 2)
    qtbot.waitUntil(
        lambda: any(page.state == PlaceholderState.DISPLAYED for page in view._canvas.pages),
        timeout=5000,
    )

    assert any(page.state == PlaceholderState.DISPLAYED for page in view._canvas.pages)
    view.shutdown()


def test_real_render_service_shuts_down_without_thread_leak() -> None:
    service = PdfRenderService()

    service.shutdown()

    assert not service._thread.isRunning()
