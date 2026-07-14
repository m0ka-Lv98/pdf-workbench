from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image, ImageChops


@dataclass(frozen=True, slots=True)
class PdfPageSnapshot:
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    rotation: int
    annotation_subtypes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PdfStructureSnapshot:
    page_count: int
    pages: tuple[PdfPageSnapshot, ...]


@dataclass(frozen=True, slots=True)
class VisualComparisonTolerance:
    max_mean_channel_error: float = 1.0
    significant_channel_delta: int = 16
    max_significant_pixel_fraction: float = 0.005


DEFAULT_VISUAL_TOLERANCE = VisualComparisonTolerance()


def compatibility_fixture_dir() -> Path:
    return Path(__file__).with_name("fixtures") / "compatibility"


def compatibility_manifest_path() -> Path:
    return compatibility_fixture_dir() / "manifest.json"


def load_compatibility_manifest() -> dict[str, object]:
    return json.loads(compatibility_manifest_path().read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    from hashlib import sha256

    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 64), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_rotation(value: int) -> int:
    rotation = int(value) % 360
    if rotation not in {0, 90, 180, 270}:
        raise AssertionError(f"rotation must normalize to 0/90/180/270, got {value}")
    return rotation


def normalize_box(values: Sequence[object]) -> tuple[float, float, float, float]:
    if len(values) != 4:
        raise AssertionError(f"expected 4 box values, got {values!r}")
    floats = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in floats):
        raise AssertionError(f"box values must be finite, got {floats!r}")
    left, bottom, right, top = floats
    if right <= left or top <= bottom:
        raise AssertionError(f"invalid box ordering: {floats!r}")
    return floats


def inspect_pdf_structure(path: Path) -> PdfStructureSnapshot:
    with pikepdf.open(path) as pdf:
        pages: list[PdfPageSnapshot] = []
        for page_index, page in enumerate(pdf.pages):
            media_box = normalize_box(page.MediaBox)
            crop_box_obj = page.get("/CropBox", None)
            crop_box = media_box if crop_box_obj is None else normalize_box(crop_box_obj)
            rotation = normalize_rotation(int(page.get("/Rotate", 0)))
            annotation_subtypes: list[str] = []
            annots_obj = page.get("/Annots", None)
            if annots_obj is not None:
                if not isinstance(annots_obj, pikepdf.Array):
                    raise AssertionError(f"{path.name} page {page_index}: /Annots must be an array")
                for annot_ref in annots_obj:
                    annot = (
                        annot_ref.get_object() if hasattr(annot_ref, "get_object") else annot_ref
                    )
                    subtype = annot.get("/Subtype", None)
                    annotation_subtypes.append(str(subtype) if subtype is not None else "")
            pages.append(
                PdfPageSnapshot(
                    media_box=media_box,
                    crop_box=crop_box,
                    rotation=rotation,
                    annotation_subtypes=tuple(annotation_subtypes),
                )
            )
        return PdfStructureSnapshot(page_count=len(pages), pages=tuple(pages))


def assert_boxes_close(
    actual: Sequence[float],
    expected: Sequence[float],
    *,
    abs_tolerance: float = 0.01,
) -> None:
    if len(actual) != 4 or len(expected) != 4:
        raise AssertionError(f"box length mismatch: actual={actual!r} expected={expected!r}")
    for index, (actual_value, expected_value) in enumerate(zip(actual, expected, strict=True)):
        if abs(float(actual_value) - float(expected_value)) > abs_tolerance:
            raise AssertionError(
                "box mismatch at index "
                f"{index}: actual={tuple(actual)!r} expected={tuple(expected)!r}"
            )


def assert_pdf_matches_manifest(path: Path, expected: Mapping[str, object]) -> None:
    snapshot = inspect_pdf_structure(path)
    expected_page_count = int(expected["page_count"])
    expected_pages = expected["pages"]
    if not isinstance(expected_pages, list):
        raise AssertionError(f"{path.name}: expected pages list, got {type(expected_pages)!r}")
    if snapshot.page_count != expected_page_count:
        raise AssertionError(
            f"{path.name}: page_count actual={snapshot.page_count} expected={expected_page_count}"
        )
    if len(expected_pages) != expected_page_count:
        raise AssertionError(
            f"{path.name}: manifest pages length {len(expected_pages)} != {expected_page_count}"
        )
    for page_index, (actual_page, expected_page) in enumerate(
        zip(snapshot.pages, expected_pages, strict=True)
    ):
        if not isinstance(expected_page, Mapping):
            raise AssertionError(f"{path.name} page {page_index}: manifest entry must be a mapping")
        assert_boxes_close(actual_page.media_box, expected_page["media_box"])  # type: ignore[arg-type]
        assert_boxes_close(actual_page.crop_box, expected_page["crop_box"])  # type: ignore[arg-type]
        expected_rotation = normalize_rotation(int(expected_page["rotation"]))
        if actual_page.rotation != expected_rotation:
            raise AssertionError(
                f"{path.name} page {page_index}: rotation "
                f"actual={actual_page.rotation} expected={expected_rotation}"
            )
        expected_annots = tuple(str(value) for value in expected_page["annotation_subtypes"])  # type: ignore[index]
        if actual_page.annotation_subtypes != expected_annots:
            raise AssertionError(
                f"{path.name} page {page_index}: annotations "
                f"actual={actual_page.annotation_subtypes!r} expected={expected_annots!r}"
            )


def render_pdf_pages(path: Path, *, scale: float = 0.5) -> tuple[Image.Image, ...]:
    document = pdfium.PdfDocument(str(path))
    images: list[Image.Image] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            bitmap: Any | None = None
            pil_image: Image.Image | None = None
            try:
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil().convert("RGBA")
                if pil_image.width <= 0 or pil_image.height <= 0:
                    raise AssertionError(
                        f"{path.name} page {page_index}: rendered image dimensions must be positive"
                    )
                images.append(pil_image.copy())
            finally:
                if pil_image is not None:
                    pil_image.close()
                if bitmap is not None and hasattr(bitmap, "close"):
                    bitmap.close()
                page.close()
    finally:
        document.close()
    return tuple(images)


def assert_pdfium_renders_all_pages(
    path: Path,
    *,
    expected_page_count: int,
) -> tuple[Image.Image, ...]:
    images = render_pdf_pages(path)
    if len(images) != expected_page_count:
        raise AssertionError(
            f"{path.name}: PDFium rendered {len(images)} pages, expected {expected_page_count}"
        )
    return images


def normalize_extracted_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.replace("\x00", ""))
    collapsed = re.sub(r"\s+", " ", normalized)
    return collapsed.strip()


def extract_pdfium_text(path: Path) -> str:
    document = pdfium.PdfDocument(str(path))
    chunks: list[str] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            text_page = page.get_textpage()
            try:
                chunks.append(text_page.get_text_range())
            finally:
                text_page.close()
                page.close()
    finally:
        document.close()
    return normalize_extracted_text(" ".join(chunks))


def assert_pdf_contains_text(path: Path, expected_text: str) -> None:
    actual = extract_pdfium_text(path)
    expected = normalize_extracted_text(expected_text)
    if expected not in actual:
        raise AssertionError(f"{path.name}: expected text {expected!r} not found in {actual!r}")


def flatten_on_white(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    composite = Image.alpha_composite(white, rgba)
    return composite.convert("RGBA")


def assert_images_visually_close(
    actual: Image.Image,
    expected: Image.Image,
    *,
    tolerance: VisualComparisonTolerance = DEFAULT_VISUAL_TOLERANCE,
    label: str = "image comparison",
) -> None:
    actual_flat = flatten_on_white(actual)
    expected_flat = flatten_on_white(expected)
    try:
        if actual_flat.size != expected_flat.size:
            raise AssertionError(
                f"{label}: image dimensions differ "
                f"actual={actual_flat.size} expected={expected_flat.size}"
            )
        diff = ImageChops.difference(actual_flat, expected_flat).convert("RGBA")
        histogram = diff.histogram()
        pixel_count = actual_flat.width * actual_flat.height
        total_channels = pixel_count * 4
        weighted_sum = 0
        significant_pixels = 0
        for channel_index in range(4):
            channel_histogram = histogram[channel_index * 256 : (channel_index + 1) * 256]
            for value, count in enumerate(channel_histogram):
                weighted_sum += value * count
            if channel_index == 0:
                significant_pixels = sum(
                    count
                    for value, count in enumerate(channel_histogram)
                    if value > tolerance.significant_channel_delta
                )
        mean_channel_error = weighted_sum / total_channels
        significant_fraction = significant_pixels / pixel_count
        if mean_channel_error > tolerance.max_mean_channel_error or (
            significant_fraction > tolerance.max_significant_pixel_fraction
        ):
            raise AssertionError(
                f"{label}: mean_channel_error={mean_channel_error:.4f}, "
                f"significant_pixel_fraction={significant_fraction:.6f}"
            )
    finally:
        actual_flat.close()
        expected_flat.close()
