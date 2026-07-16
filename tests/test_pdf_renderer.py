from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PySide6.QtGui import QImage
from pytestqt.qtbot import QtBot

import pdf_workbench.services.pdf_renderer as pdf_renderer_module
from pdf_workbench.domain.mutation import PageIndexTransition
from pdf_workbench.services.page_coordinates import PageMetadata
from pdf_workbench.services.pdf_renderer import (
    DocumentMetadata,
    DocumentReleaseRequest,
    DocumentReleaseResult,
    DocumentRevision,
    PageTextIndex,
    PdfiumDocumentBackend,
    PdfRenderService,
    PdfRenderWorker,
    RenderCacheKey,
    RenderFailure,
    RenderImageCache,
    RenderRequest,
    TextCharacterBox,
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


def create_text_pdf(path: Path, pages: list[str]) -> Path:
    objects: list[bytes] = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        f"2 0 obj << /Type /Pages /Count {len(pages)} /Kids [".encode("ascii"),
    ]
    page_object_numbers = [3 + index * 2 for index in range(len(pages))]
    content_object_numbers = [4 + index * 2 for index in range(len(pages))]
    objects[1] += b" ".join(f"{number} 0 R".encode("ascii") for number in page_object_numbers)
    objects[1] += b"] >> endobj\n"
    for page_number, content_number, text in zip(
        page_object_numbers,
        content_object_numbers,
        pages,
        strict=True,
    ):
        content = f"BT /F1 18 Tf 40 100 Td ({text}) Tj ET".encode("latin-1")
        objects.append(
            f"{page_number} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Resources << /Font << /F1 100 0 R >> >> /Contents {content_number} 0 R "
            f">> endobj\n".encode("ascii")
        )
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


class FakeBackend:
    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False
        self.render_calls: list[int] = []

    def page_count(self) -> int:
        return 2

    def page_metadata(self, page_index: int) -> PageMetadata:
        return PageMetadata.from_size(100.0 + page_index, 200.0)

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

    def extract_text_page(self, page_index: int, revision: DocumentRevision) -> PageTextIndex:
        return PageTextIndex(
            revision=revision,
            page_index=page_index,
            text=f"page {page_index}",
            characters=(
                TextCharacterBox(
                    pdfium_index=0,
                    text="p",
                    box=PageMetadata.from_size(10, 10).geometry.visible_box,
                ),
            ),
        )

    def close(self) -> None:
        self.closed = True


class FailingMetadataBackend(FakeBackend):
    def page_metadata(self, page_index: int) -> PageMetadata:
        if page_index == 0:
            raise RuntimeError("metadata failure")
        return super().page_metadata(page_index)


class BlockingBackend(FakeBackend):
    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.render_started = threading.Event()
        self.allow_render_to_finish = threading.Event()
        self.close_call_count = 0

    def render_page(
        self,
        page_index: int,
        logical_zoom: float,
        rotation: int,
        device_pixel_ratio: float,
    ) -> QImage:
        self.render_started.set()
        if not self.allow_render_to_finish.wait(timeout=5):
            raise RuntimeError("test render was not released")
        return super().render_page(page_index, logical_zoom, rotation, device_pixel_ratio)

    def close(self) -> None:
        self.close_call_count += 1
        super().close()


class FailingRenderBackend(FakeBackend):
    def render_page(
        self,
        page_index: int,
        logical_zoom: float,
        rotation: int,
        device_pixel_ratio: float,
    ) -> QImage:
        raise RuntimeError(f"render failed {page_index}")


class FailingCloseBackend(FakeBackend):
    def close(self) -> None:
        raise RuntimeError("close failed")


class FakeRenderableImage:
    def __init__(
        self,
        *,
        name: str,
        converted: object | None = None,
        fail_convert: bool = False,
    ) -> None:
        self.name = name
        self.converted = converted
        self.fail_convert = fail_convert
        self.close_call_count = 0

    def convert(self, _mode: str) -> object:
        if self.fail_convert:
            raise RuntimeError(f"{self.name} convert failed")
        if self.converted is None:
            raise RuntimeError(f"{self.name} missing converted image")
        return self.converted

    def close(self) -> None:
        self.close_call_count += 1


class FakeBitmapResource:
    def __init__(self, *, source_image: object | None = None, fail_to_pil: bool = False) -> None:
        self.source_image = source_image
        self.fail_to_pil = fail_to_pil
        self.close_call_count = 0

    def to_pil(self) -> object:
        if self.fail_to_pil:
            raise RuntimeError("to_pil failed")
        if self.source_image is None:
            raise RuntimeError("missing source image")
        return self.source_image

    def close(self) -> None:
        self.close_call_count += 1


class FakePdfiumPage:
    def __init__(self, *, bitmap: object | None = None, fail_render: bool = False) -> None:
        self.bitmap = bitmap
        self.fail_render = fail_render
        self.close_call_count = 0

    def render(self, *, scale: float, rotation: int) -> object:
        _ = (scale, rotation)
        if self.fail_render:
            raise RuntimeError("render failed")
        if self.bitmap is None:
            raise RuntimeError("missing bitmap")
        return self.bitmap

    def close(self) -> None:
        self.close_call_count += 1


class FakePdfiumDocument:
    def __init__(self, page: FakePdfiumPage) -> None:
        self.page = page

    def __getitem__(self, page_index: int) -> FakePdfiumPage:
        assert page_index == 0
        return self.page


def create_fake_pdfium_backend(page: FakePdfiumPage) -> PdfiumDocumentBackend:
    backend = PdfiumDocumentBackend.__new__(PdfiumDocumentBackend)
    backend._document = FakePdfiumDocument(page)  # type: ignore[attr-defined]
    backend._reader = object()  # type: ignore[attr-defined]
    return backend


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


def test_render_cache_transition_drops_only_affected_pages_and_rekeys_others(
    tmp_path: Path,
) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    old_revision = make_revision(tmp_path, name="before.pdf", content=b"before")
    new_revision = make_revision(tmp_path, name="after.pdf", content=b"after")
    unaffected_key = RenderCacheKey(old_revision, 1, 1.5, 0, 1.0)
    affected_key = RenderCacheKey(old_revision, 0, 1.5, 0, 1.0)
    unrelated_revision = make_revision(tmp_path, name="other.pdf", content=b"other")
    unrelated_key = RenderCacheKey(unrelated_revision, 0, 1.0, 0, 1.0)
    unaffected_image = make_image()
    affected_image = make_image(80, 80)
    unrelated_image = make_image(48, 48)
    expected_total = unaffected_image.sizeInBytes() + unrelated_image.sizeInBytes()

    cache.put(unaffected_key, unaffected_image)
    cache.put(affected_key, affected_image)
    cache.put(unrelated_key, unrelated_image)

    cache.transition_revision(old_revision, new_revision, affected_pages=frozenset({0}))

    assert cache.get(affected_key) is None
    assert cache.get(RenderCacheKey(new_revision, 1, 1.5, 0, 1.0)) is unaffected_image
    assert cache.get(unrelated_key) is unrelated_image
    assert cache.total_bytes == expected_total


@pytest.mark.parametrize("old_first", [True, False])
def test_render_cache_transition_prefers_existing_new_revision_entries_on_collision(
    tmp_path: Path,
    old_first: bool,
) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    old_revision = make_revision(tmp_path, name="before.pdf", content=b"before")
    new_revision = make_revision(tmp_path, name="after.pdf", content=b"after")
    old_main_key = RenderCacheKey(old_revision, 1, 1.5, 0, 1.0)
    new_main_key = RenderCacheKey(new_revision, 1, 1.5, 0, 1.0)
    old_thumb_key = RenderCacheKey(old_revision, 1, 0.25, 0, 1.0)
    new_thumb_key = RenderCacheKey(new_revision, 1, 0.25, 0, 1.0)
    old_main_image = make_image(80, 80)
    new_main_image = make_image(81, 81)
    old_thumb_image = make_image(40, 40)
    new_thumb_image = make_image(41, 41)

    if old_first:
        cache.put(old_main_key, old_main_image)
        cache.put(old_thumb_key, old_thumb_image)
        cache.put(new_main_key, new_main_image)
        cache.put(new_thumb_key, new_thumb_image)
    else:
        cache.put(new_main_key, new_main_image)
        cache.put(new_thumb_key, new_thumb_image)
        cache.put(old_main_key, old_main_image)
        cache.put(old_thumb_key, old_thumb_image)

    cache.transition_revision(old_revision, new_revision, affected_pages=frozenset())

    assert cache.get(new_main_key) is new_main_image
    assert cache.get(new_thumb_key) is new_thumb_image
    assert cache.get(old_main_key) is None
    assert cache.get(old_thumb_key) is None


def test_render_cache_structural_transition_rekeys_originals_and_drops_duplicate_slots(
    tmp_path: Path,
) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    old_revision = make_revision(tmp_path, name="before.pdf", content=b"before")
    new_revision = make_revision(tmp_path, name="after.pdf", content=b"after")
    unrelated_revision = make_revision(tmp_path, name="other.pdf", content=b"other")
    transition = PageIndexTransition(
        old_page_count=5,
        new_page_count=7,
        cache_old_to_new=(0, 1, 3, 4, 6),
        current_page_old_to_new=(0, 2, 3, 5, 6),
    )
    selected_original_key = RenderCacheKey(old_revision, 1, 1.5, 0, 2.0)
    shifted_original_key = RenderCacheKey(old_revision, 2, 0.25, 0, 1.0)
    later_selected_key = RenderCacheKey(old_revision, 3, 1.0, 90, 1.0)
    unrelated_key = RenderCacheKey(unrelated_revision, 0, 1.0, 0, 1.0)
    selected_original_image = make_image(81, 81)
    shifted_original_image = make_image(32, 48)
    later_selected_image = make_image(96, 64)
    unrelated_image = make_image(40, 40)

    cache.put(selected_original_key, selected_original_image)
    cache.put(shifted_original_key, shifted_original_image)
    cache.put(later_selected_key, later_selected_image)
    cache.put(unrelated_key, unrelated_image)

    cache.transition_revision(
        old_revision,
        new_revision,
        affected_pages=frozenset({1, 3}),
        page_index_transition=transition,
    )

    assert cache.get(selected_original_key) is None
    assert cache.get(shifted_original_key) is None
    assert cache.get(later_selected_key) is None
    assert cache.get(RenderCacheKey(new_revision, 1, 1.5, 0, 2.0)) is selected_original_image
    assert cache.get(RenderCacheKey(new_revision, 3, 0.25, 0, 1.0)) is shifted_original_image
    assert cache.get(RenderCacheKey(new_revision, 4, 1.0, 90, 1.0)) is later_selected_image
    assert cache.get(RenderCacheKey(new_revision, 2, 1.5, 0, 2.0)) is None
    assert cache.get(RenderCacheKey(new_revision, 5, 1.0, 90, 1.0)) is None
    assert cache.get(unrelated_key) is unrelated_image
    assert cache.total_bytes == (
        selected_original_image.sizeInBytes()
        + shifted_original_image.sizeInBytes()
        + later_selected_image.sizeInBytes()
        + unrelated_image.sizeInBytes()
    )
    assert all(key.revision != old_revision for key in cache._items)


def test_page_index_transition_rejects_non_integer_and_duplicate_mappings() -> None:
    with pytest.raises(ValueError, match="old_page_count must be an integer"):
        PageIndexTransition(  # type: ignore[arg-type]
            old_page_count=True,
            new_page_count=1,
            cache_old_to_new=(0,),
            current_page_old_to_new=(0,),
        )
    with pytest.raises(ValueError, match="values must be integers or None"):
        PageIndexTransition(  # type: ignore[arg-type]
            old_page_count=1,
            new_page_count=2,
            cache_old_to_new=(False,),
            current_page_old_to_new=(0,),
        )
    with pytest.raises(ValueError, match="must be unique"):
        PageIndexTransition(
            old_page_count=2,
            new_page_count=3,
            cache_old_to_new=(1, 1),
            current_page_old_to_new=(1, 2),
        )


def test_render_cache_structural_undo_transition_drops_duplicate_entries(
    tmp_path: Path,
) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    duplicated_revision = make_revision(tmp_path, name="duplicated.pdf", content=b"duplicated")
    restored_revision = make_revision(tmp_path, name="restored.pdf", content=b"restored")
    transition = PageIndexTransition(
        old_page_count=7,
        new_page_count=5,
        cache_old_to_new=(0, 1, None, 2, 3, None, 4),
        current_page_old_to_new=(0, 1, 1, 2, 3, 3, 4),
    )
    original_key = RenderCacheKey(duplicated_revision, 1, 1.5, 0, 1.0)
    duplicate_key = RenderCacheKey(duplicated_revision, 2, 1.5, 0, 1.0)
    shifted_key = RenderCacheKey(duplicated_revision, 3, 0.25, 0, 2.0)
    original_image = make_image(80, 80)
    duplicate_image = make_image(81, 81)
    shifted_image = make_image(32, 32)

    cache.put(original_key, original_image)
    cache.put(duplicate_key, duplicate_image)
    cache.put(shifted_key, shifted_image)

    cache.transition_revision(
        duplicated_revision,
        restored_revision,
        affected_pages=frozenset({1, 2}),
        page_index_transition=transition,
    )

    assert cache.get(RenderCacheKey(restored_revision, 1, 1.5, 0, 1.0)) is original_image
    assert cache.get(RenderCacheKey(restored_revision, 2, 0.25, 0, 2.0)) is shifted_image
    assert cache.get(RenderCacheKey(restored_revision, 2, 1.5, 0, 1.0)) is None
    assert cache.total_bytes == original_image.sizeInBytes() + shifted_image.sizeInBytes()
    assert all(key.revision != duplicated_revision for key in cache._items)


def test_render_cache_structural_transition_prefers_existing_new_revision_entry_on_collision(
    tmp_path: Path,
) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    old_revision = make_revision(tmp_path, name="before.pdf", content=b"before")
    new_revision = make_revision(tmp_path, name="after.pdf", content=b"after")
    transition = PageIndexTransition(
        old_page_count=5,
        new_page_count=7,
        cache_old_to_new=(0, 1, 3, 4, 6),
        current_page_old_to_new=(0, 2, 3, 5, 6),
    )
    old_key = RenderCacheKey(old_revision, 2, 1.0, 0, 1.0)
    new_key = RenderCacheKey(new_revision, 3, 1.0, 0, 1.0)
    old_image = make_image(72, 72)
    new_image = make_image(73, 73)

    cache.put(new_key, new_image)
    cache.put(old_key, old_image)

    cache.transition_revision(
        old_revision,
        new_revision,
        affected_pages=frozenset(),
        page_index_transition=transition,
    )

    assert cache.get(new_key) is new_image
    assert cache.total_bytes == new_image.sizeInBytes()


def test_render_cache_structural_transition_rejects_out_of_range_keys_without_partial_update(
    tmp_path: Path,
) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    old_revision = make_revision(tmp_path, name="before.pdf", content=b"before")
    new_revision = make_revision(tmp_path, name="after.pdf", content=b"after")
    transition = PageIndexTransition(
        old_page_count=5,
        new_page_count=7,
        cache_old_to_new=(0, 1, 3, 4, 6),
        current_page_old_to_new=(0, 2, 3, 5, 6),
    )
    invalid_key = RenderCacheKey(old_revision, 5, 1.0, 0, 1.0)
    image = make_image(64, 64)
    cache.put(invalid_key, image)
    snapshot = list(cache._items.items())
    total_before = cache.total_bytes

    with pytest.raises(ValueError, match="outside the transition range"):
        cache.transition_revision(
            old_revision,
            new_revision,
            affected_pages=frozenset(),
            page_index_transition=transition,
        )

    assert list(cache._items.items()) == snapshot
    assert cache.total_bytes == total_before


def test_render_service_release_document_waits_for_worker_close(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    backend = FakeBackend("sample")
    service = PdfRenderService(backend_factory=lambda _path: backend)
    qtbot.waitUntil(service._thread.isRunning)
    revision = make_revision(tmp_path)
    document_path = tmp_path / "sample.pdf"

    service.open_document("doc-1", document_path, 1, revision)
    qtbot.waitUntil(lambda: "doc-1" in service._worker._documents)

    result = service.release_document("doc-1", 1, timeout_ms=3000)
    assert result.success is True
    assert result.document_id == "doc-1"
    assert result.requested_generation == 1
    qtbot.waitUntil(lambda: backend.closed is True)
    assert "doc-1" not in service._worker._documents
    assert service.shutdown() is True


def test_render_service_release_document_returns_failure_when_thread_is_not_running(
    qtbot: QtBot,
) -> None:
    service = PdfRenderService()
    qtbot.waitUntil(service._thread.isRunning)
    assert service.shutdown() is True

    result = service.release_document("doc-1", 1, timeout_ms=10)

    assert result.success is False
    assert result.request_id == "not-issued"
    assert result.closed_generation is None
    assert result.message == "renderer thread is not running"


def test_render_worker_release_document_rejects_stale_generation(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    backend = FakeBackend("sample")
    worker = PdfRenderWorker(lambda _path: backend, cache)
    revision = make_revision(tmp_path)
    worker.open_document("doc-1", tmp_path / "sample.pdf", 2, revision)
    results: list[DocumentReleaseResult] = []
    worker.document_released.connect(lambda result: results.append(result))

    worker.release_document(DocumentReleaseRequest("req-1", "doc-1", 1))

    assert results == [
        DocumentReleaseResult(
            request_id="req-1",
            document_id="doc-1",
            requested_generation=1,
            closed_generation=2,
            success=False,
            message="stale generation",
        )
    ]
    assert backend.closed is False


def test_render_worker_release_document_returns_failure_on_close_exception(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    worker = PdfRenderWorker(lambda _path: FailingCloseBackend("broken"), cache)
    revision = make_revision(tmp_path)
    worker.open_document("doc-1", tmp_path / "sample.pdf", 1, revision)
    results: list[DocumentReleaseResult] = []
    worker.document_released.connect(lambda result: results.append(result))

    worker.release_document(DocumentReleaseRequest("req-2", "doc-1", 1))

    assert len(results) == 1
    assert results[0].request_id == "req-2"
    assert results[0].success is False
    assert results[0].message == "close failed"
    assert "doc-1" in worker._documents


def test_render_worker_release_document_without_context_is_idempotent_success(
    tmp_path: Path,
) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    worker = PdfRenderWorker(lambda _path: FakeBackend("unused"), cache)
    results: list[DocumentReleaseResult] = []
    worker.document_released.connect(lambda result: results.append(result))

    worker.release_document(DocumentReleaseRequest("req-idempotent", "missing", 4))

    assert results == [
        DocumentReleaseResult(
            request_id="req-idempotent",
            document_id="missing",
            requested_generation=4,
            closed_generation=None,
            success=True,
        )
    ]


def test_render_worker_release_document_rejects_generation_mismatch(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    backend = FakeBackend("sample")
    worker = PdfRenderWorker(lambda _path: backend, cache)
    revision = make_revision(tmp_path)
    worker.open_document("doc-1", tmp_path / "sample.pdf", 3, revision)
    results: list[DocumentReleaseResult] = []
    worker.document_released.connect(lambda result: results.append(result))

    worker.release_document(DocumentReleaseRequest("req-mismatch", "doc-1", 4))

    assert results == [
        DocumentReleaseResult(
            request_id="req-mismatch",
            document_id="doc-1",
            requested_generation=4,
            closed_generation=3,
            success=False,
            message="generation mismatch",
        )
    ]
    assert backend.closed is False


def test_render_worker_open_document_reports_existing_backend_close_failure(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    first_backend = FailingCloseBackend("first")
    second_backend = FakeBackend("second")
    backends = [first_backend, second_backend]
    worker = PdfRenderWorker(lambda _path: backends.pop(0), cache)
    revision = make_revision(tmp_path)
    failures: list[tuple[str, int, str]] = []
    worker.document_failed.connect(
        lambda document_id, generation, message: failures.append((document_id, generation, message))
    )

    worker.open_document("doc-1", tmp_path / "first.pdf", 1, revision)
    worker.open_document("doc-1", tmp_path / "second.pdf", 2, revision)

    assert failures == [("doc-1", 2, "close failed")]
    assert worker._documents["doc-1"].backend is first_backend
    assert second_backend.closed is False


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
    assert metadata.pages[0].height_points > 0
    assert metadata.pages[0].geometry.visible_box.width > 0
    assert metadata.pages[0].geometry.visible_box.height > 0


def test_pdfium_backend_extracts_text_and_character_boxes(tmp_path: Path) -> None:
    pdf_path = create_text_pdf(tmp_path / "text.pdf", ["Hello world"])

    backend = PdfiumDocumentBackend(pdf_path)
    try:
        page_text = backend.extract_text_page(
            0,
            DocumentRevision.from_path(pdf_path),
        )
    finally:
        backend.close()

    assert page_text.page_index == 0
    assert "Hello world" in page_text.text
    assert page_text.characters
    assert page_text.characters[0].text.strip() == "H"
    assert page_text.characters[0].box.width > 0
    assert page_text.characters[0].box.height > 0


@pytest.mark.parametrize(
    ("logical_zoom", "device_pixel_ratio", "rotation"),
    [
        (float("nan"), 1.0, 0),
        (float("inf"), 1.0, 0),
        (1.0, float("nan"), 0),
        (1.0, float("inf"), 0),
        (1.0, 1.0, 45),
        (1.0, 1.0, 360),
    ],
)
def test_pdfium_backend_rejects_invalid_render_inputs(
    tmp_path: Path,
    logical_zoom: float,
    device_pixel_ratio: float,
    rotation: int,
) -> None:
    from pypdf import PdfWriter

    pdf_path = tmp_path / "invalid-render.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=144, height=144)
    with pdf_path.open("wb") as stream:
        writer.write(stream)

    backend = PdfiumDocumentBackend(pdf_path)
    try:
        with pytest.raises(ValueError):
            backend.render_page(0, logical_zoom, rotation, device_pixel_ratio)
    finally:
        backend.close()


def test_render_request_cache_key_distinguishes_main_and_thumbnail_revision(
    tmp_path: Path,
) -> None:
    revision = make_revision(tmp_path)
    main_request = RenderRequest("doc-1", 1, 0, 1.5, 0, 1.0, 0, revision)
    thumbnail_request = RenderRequest("doc-1", 1, 0, 0.25, 0, 1.0, 10, revision)

    assert main_request.cache_key != thumbnail_request.cache_key


def test_pdfium_backend_closes_render_resources_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    converted = FakeRenderableImage(name="rgba")
    source = FakeRenderableImage(name="source", converted=converted)
    bitmap = FakeBitmapResource(source_image=source)
    page = FakePdfiumPage(bitmap=bitmap)
    backend = create_fake_pdfium_backend(page)
    monkeypatch.setattr(
        pdf_renderer_module,
        "ImageQt",
        lambda _image: QImage(12, 16, QImage.Format.Format_ARGB32),
    )

    image = backend.render_page(0, logical_zoom=1.0, rotation=0, device_pixel_ratio=2.0)

    assert image.width() == 12
    assert image.height() == 16
    assert image.devicePixelRatio() == 2.0
    assert page.close_call_count == 1
    assert bitmap.close_call_count == 1
    assert source.close_call_count == 1
    assert converted.close_call_count == 1


def test_pdfium_backend_closes_resources_when_to_pil_fails() -> None:
    bitmap = FakeBitmapResource(fail_to_pil=True)
    page = FakePdfiumPage(bitmap=bitmap)
    backend = create_fake_pdfium_backend(page)

    with pytest.raises(RuntimeError, match="to_pil failed"):
        backend.render_page(0, logical_zoom=1.0, rotation=0, device_pixel_ratio=1.0)

    assert page.close_call_count == 1
    assert bitmap.close_call_count == 1


def test_pdfium_backend_closes_resources_when_convert_fails() -> None:
    source = FakeRenderableImage(name="source", fail_convert=True)
    bitmap = FakeBitmapResource(source_image=source)
    page = FakePdfiumPage(bitmap=bitmap)
    backend = create_fake_pdfium_backend(page)

    with pytest.raises(RuntimeError, match="source convert failed"):
        backend.render_page(0, logical_zoom=1.0, rotation=0, device_pixel_ratio=1.0)

    assert page.close_call_count == 1
    assert bitmap.close_call_count == 1
    assert source.close_call_count == 1


def test_pdfium_backend_closes_resources_when_qimage_conversion_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    converted = FakeRenderableImage(name="rgba")
    source = FakeRenderableImage(name="source", converted=converted)
    bitmap = FakeBitmapResource(source_image=source)
    page = FakePdfiumPage(bitmap=bitmap)
    backend = create_fake_pdfium_backend(page)
    monkeypatch.setattr(
        pdf_renderer_module,
        "ImageQt",
        lambda _image: (_ for _ in ()).throw(RuntimeError("imageqt failed")),
    )

    with pytest.raises(RuntimeError, match="imageqt failed"):
        backend.render_page(0, logical_zoom=1.0, rotation=0, device_pixel_ratio=1.0)

    assert page.close_call_count == 1
    assert bitmap.close_call_count == 1
    assert source.close_call_count == 1
    assert converted.close_call_count == 1


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


def test_render_worker_indexes_text_pages_in_background(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    backend = FakeBackend("sample")
    worker = PdfRenderWorker(lambda _path: backend, cache)
    revision = make_revision(tmp_path)
    path = tmp_path / "sample.pdf"
    text_pages: list[PageTextIndex] = []
    worker.text_page_indexed.connect(
        lambda _document_id, _revision, page_text: text_pages.append(page_text)
    )

    worker.open_document("doc-1", path, 1, revision)
    worker._process_next()

    assert backend.render_calls == []
    assert len(worker._pending_text_requests) == 1
    assert text_pages and text_pages[0].page_index == 0


def test_service_shutdown_returns_bool(qtbot: QtBot) -> None:
    from pdf_workbench.services.pdf_renderer import PdfRenderService as Service

    service = Service()
    qtbot.waitUntil(service._thread.isRunning)

    assert service.shutdown() is True
    assert service.shutdown() is True


def test_render_service_shutdown_timeout_can_be_retried(qtbot: QtBot, tmp_path: Path) -> None:
    from pdf_workbench.services.pdf_renderer import PdfRenderService as Service

    backend = BlockingBackend("blocking")
    factory_calls: list[Path] = []

    def factory(path: Path) -> BlockingBackend:
        factory_calls.append(path)
        return backend

    service = Service(backend_factory=factory)
    qtbot.waitUntil(service._thread.isRunning)

    revision = make_revision(tmp_path)
    document_path = tmp_path / "blocking.pdf"
    service.open_document("doc-1", document_path, 1, revision)

    qtbot.waitUntil(lambda: "doc-1" in service._worker._documents)
    service.request_render(
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

    assert backend.render_started.wait(timeout=5) is True
    assert service.shutdown(timeout_ms=10) is False
    assert service._thread.isRunning()

    backend.allow_render_to_finish.set()
    qtbot.waitUntil(lambda: backend.close_call_count == 1)

    assert service.shutdown(timeout_ms=3000) is True
    assert not service._thread.isRunning()
    assert service.shutdown(timeout_ms=3000) is True
    assert backend.close_call_count == 1
    assert len(factory_calls) == 1


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


def test_render_worker_processes_main_page_requests_before_thumbnail_requests(
    tmp_path: Path,
) -> None:
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
            page_index=1,
            logical_zoom=0.25,
            rotation=0,
            device_pixel_ratio=1.0,
            priority=10,
            revision=revision,
        )
    )
    worker.enqueue_render(
        RenderRequest(
            document_id="doc-1",
            generation=1,
            page_index=0,
            logical_zoom=1.5,
            rotation=0,
            device_pixel_ratio=1.0,
            priority=0,
            revision=revision,
        )
    )

    worker._process_next()
    worker._process_next()

    assert backend.render_calls == [0, 1]


def test_worker_updates_generation_without_reopening_backend(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    backend = FakeBackend("sample")
    factory_calls = 0

    def factory(_path: Path) -> FakeBackend:
        nonlocal factory_calls
        factory_calls += 1
        return backend

    worker = PdfRenderWorker(factory, cache)
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
    assert len(worker._pending_requests) == 0
    worker.enqueue_render(
        RenderRequest(
            document_id="doc-1",
            generation=2,
            page_index=0,
            logical_zoom=1.0,
            rotation=0,
            device_pixel_ratio=1.0,
            priority=0,
            revision=revision,
        )
    )
    worker._process_next()

    assert backend.closed is False
    assert worker._documents["doc-1"].generation == 2
    assert backend.render_calls == [0]
    assert factory_calls == 1
    assert worker._pending_requests == []


def test_generation_update_does_not_affect_other_document(tmp_path: Path) -> None:
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

    request_a = RenderRequest(
        document_id="doc-a",
        generation=1,
        page_index=0,
        logical_zoom=1.0,
        rotation=0,
        device_pixel_ratio=1.0,
        priority=0,
        revision=revision_a,
    )
    request_b = RenderRequest(
        document_id="doc-b",
        generation=1,
        page_index=0,
        logical_zoom=1.0,
        rotation=0,
        device_pixel_ratio=1.0,
        priority=0,
        revision=revision_b,
    )

    worker.enqueue_render(request_a)
    worker.enqueue_render(request_b)

    worker.update_document_generation("doc-a", 2, revision_a)

    assert all(request.request.document_id == "doc-b" for request in worker._pending_requests)
    assert worker._documents["doc-b"].generation == 1
    assert backends["b"].closed is False

    worker._process_next()

    assert backends["b"].render_calls == [0]


def test_render_worker_failure_includes_cache_key(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    backend = FailingRenderBackend("failing")
    worker = PdfRenderWorker(lambda _path: backend, cache)
    revision = make_revision(tmp_path)
    path = tmp_path / "sample.pdf"
    failures: list[RenderFailure] = []
    worker.render_failed.connect(lambda failure: failures.append(failure))

    worker.open_document("doc-1", path, 1, revision)
    request = RenderRequest(
        document_id="doc-1",
        generation=1,
        page_index=1,
        logical_zoom=0.25,
        rotation=90,
        device_pixel_ratio=2.0,
        priority=10,
        revision=revision,
    )
    worker.enqueue_render(request)
    worker._process_next()

    assert len(failures) == 1
    assert failures[0].document_id == "doc-1"
    assert failures[0].generation == 1
    assert failures[0].cache_key == request.cache_key
    assert failures[0].page_index == 1
    assert failures[0].message == "render failed 1"


def test_worker_closes_backend_when_metadata_read_fails(tmp_path: Path) -> None:
    cache = RenderImageCache(max_bytes=1024 * 1024)
    backend = FailingMetadataBackend("broken")
    worker = PdfRenderWorker(lambda _path: backend, cache)
    revision = make_revision(tmp_path)
    path = tmp_path / "broken.pdf"

    worker.open_document("doc-err", path, 1, revision)

    assert backend.closed is True
    assert "doc-err" not in worker._documents
