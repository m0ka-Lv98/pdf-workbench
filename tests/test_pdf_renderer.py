from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QImage
from pytestqt.qtbot import QtBot

from pdf_workbench.services.pdf_renderer import (
    DocumentMetadata,
    DocumentRevision,
    PageMetadata,
    PdfiumDocumentBackend,
    PdfRenderWorker,
    RenderCacheKey,
    RenderImageCache,
    RenderRequest,
)


def make_image(width: int = 64, height: int = 64) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(0xFF336699)
    return image


def make_revision(
    tmp_path: Path,
    name: str = "sample.pdf",
    content: bytes = b"pdf",
) -> DocumentRevision:
    path = tmp_path / name
    path.write_bytes(content)
    return DocumentRevision.from_path(path)


class FakeBackend:
    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False
        self.render_calls: list[int] = []

    def page_count(self) -> int:
        return 2

    def page_metadata(self, page_index: int) -> PageMetadata:
        return PageMetadata(width_points=100.0 + page_index, height_points=200.0)

    def render_page(
        self,
        page_index: int,
        logical_zoom: float,
        rotation: int,
        device_pixel_ratio: float,
    ) -> QImage:
        self.render_calls.append(page_index)
        image = make_image()
        image.setDevicePixelRatio(device_pixel_ratio)
        return image

    def close(self) -> None:
        self.closed = True


class FailingMetadataBackend(FakeBackend):
    def page_metadata(self, page_index: int) -> PageMetadata:
        if page_index == 0:
            raise RuntimeError("metadata failure")
        return super().page_metadata(page_index)


def test_render_cache_hits_existing_entry(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    revision = make_revision(tmp_path)
    key = RenderCacheKey(revision, 0, 1.5, 0, 2.0)
    image = make_image()

    cache.put(key, image)

    assert cache.get(key) is image


def test_render_cache_evicts_least_recently_used_entries(tmp_path: Path) -> None:
    base_image = make_image(128, 128)
    cache = RenderImageCache(max_bytes=int(base_image.sizeInBytes() * 1.5))
    revision = make_revision(tmp_path)
    first_key = RenderCacheKey(revision, 0, 1.0, 0, 1.0)
    second_key = RenderCacheKey(revision, 1, 1.0, 0, 1.0)

    cache.put(first_key, base_image)
    cache.put(second_key, make_image(128, 128))

    assert cache.get(first_key) is None
    assert cache.get(second_key) is not None


@pytest.mark.parametrize(
    ("scale", "rotation", "device_pixel_ratio"),
    [
        (1.0, 0, 1.0),
        (1.5, 0, 1.0),
        (1.0, 90, 1.0),
        (1.0, 0, 2.0),
    ],
)
def test_render_cache_distinguishes_scale_rotation_and_dpr(
    tmp_path: Path,
    scale: float,
    rotation: int,
    device_pixel_ratio: float,
) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    revision = make_revision(tmp_path)
    baseline_key = RenderCacheKey(revision, 0, 1.0, 0, 1.0)
    cache.put(baseline_key, make_image())

    candidate_key = RenderCacheKey(revision, 0, scale, rotation, device_pixel_ratio)
    expected = baseline_key == candidate_key

    assert (cache.get(candidate_key) is not None) is expected


def test_render_cache_misses_when_file_revision_changes(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    first_revision = make_revision(tmp_path, content=b"one")
    cache.put(RenderCacheKey(first_revision, 0, 1.0, 0, 1.0), make_image())

    second_revision = make_revision(tmp_path, content=b"updated-content")

    assert cache.get(RenderCacheKey(second_revision, 0, 1.0, 0, 1.0)) is None


def test_pdfium_backend_sets_device_pixel_ratio(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    pdf_path = tmp_path / "dpi.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=144, height=144)
    with pdf_path.open("wb") as stream:
        writer.write(stream)

    backend = PdfiumDocumentBackend(pdf_path)
    try:
        image = backend.render_page(0, logical_zoom=1.0, rotation=0, device_pixel_ratio=2.0)
        metadata = DocumentMetadata(
            revision=DocumentRevision.from_path(pdf_path),
            pages=(backend.page_metadata(0),),
        )
    finally:
        backend.close()

    assert image.devicePixelRatio() == 2.0
    assert metadata.page_count == 1
    assert metadata.pages[0].width_points > 0


def test_render_worker_closes_only_target_document_and_keeps_others(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    backends: dict[str, FakeBackend] = {}

    def factory(path: Path) -> FakeBackend:
        backend = FakeBackend(path.stem)
        backends[path.stem] = backend
        return backend

    worker = PdfRenderWorker(factory, cache)
    revision_a = make_revision(tmp_path, name="a.pdf")
    revision_b = make_revision(tmp_path, name="b.pdf")

    worker.open_document("doc-a", tmp_path / "a.pdf", 1, revision_a)
    worker.open_document("doc-b", tmp_path / "b.pdf", 1, revision_b)

    worker.close_document("doc-a", 1)

    assert backends["a"].closed is True
    assert backends["b"].closed is False

    worker.enqueue_render(
        RenderRequest(
            document_id="doc-b",
            generation=1,
            page_index=0,
            logical_zoom=1.0,
            rotation=0,
            device_pixel_ratio=1.0,
            priority=0,
            revision=revision_b,
        )
    )
    worker._process_next()

    assert backends["b"].render_calls == [0]


def test_service_shutdown_returns_bool(qtbot: QtBot) -> None:
    from pdf_workbench.services.pdf_renderer import PdfRenderService as Service

    service = Service()
    qtbot.waitUntil(service._thread.isRunning)

    assert service.shutdown() is True
    assert service.shutdown() is True


def test_render_worker_deduplicates_identical_requests_per_document(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    backend = FakeBackend("sample")
    worker = PdfRenderWorker(lambda _path: backend, cache)
    revision = make_revision(tmp_path)
    path = tmp_path / "sample.pdf"

    worker.open_document("doc-1", path, 2, revision)
    request = RenderRequest(
        document_id="doc-1",
        generation=2,
        page_index=1,
        logical_zoom=1.5,
        rotation=90,
        device_pixel_ratio=2.0,
        priority=0,
        revision=revision,
    )

    worker.enqueue_render(request)
    worker.enqueue_render(request)

    assert len(worker._pending_requests) == 1


def test_worker_updates_generation_without_reopening_backend(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    backend = FakeBackend("sample")
    worker = PdfRenderWorker(lambda _path: backend, cache)
    revision = make_revision(tmp_path)
    path = tmp_path / "sample.pdf"

    worker.open_document("doc-1", path, 1, revision)
    worker.enqueue_render(
        RenderRequest(
            document_id="doc-1",
            generation=1,
            page_index=0,
            logical_zoom=1.0,
            rotation=0,
            device_pixel_ratio=1.0,
            priority=0,
            revision=revision,
        )
    )
    worker.update_document_generation("doc-1", 2, revision)

    assert backend.closed is False
    assert worker._documents["doc-1"].generation == 2
    assert worker._pending_requests == []


def test_worker_closes_backend_when_metadata_read_fails(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    backend = FailingMetadataBackend("broken")
    worker = PdfRenderWorker(lambda _path: backend, cache)
    revision = make_revision(tmp_path)
    path = tmp_path / "broken.pdf"

    worker.open_document("doc-err", path, 1, revision)

    assert backend.closed is True
    assert "doc-err" not in worker._documents
