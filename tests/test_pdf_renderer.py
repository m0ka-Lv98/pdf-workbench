from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QImage

from pdf_workbench.services.pdf_renderer import (
    DocumentMetadata,
    DocumentRevision,
    PdfiumDocumentBackend,
    RenderCacheKey,
    RenderImageCache,
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

    second_revision = make_revision(tmp_path, content=b"two")

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
