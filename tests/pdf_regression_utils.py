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
class PdfAnnotationSnapshot:
    subtype: str
    rect: tuple[float, float, float, float]
    has_appearance: bool


@dataclass(frozen=True, slots=True)
class PdfPageSnapshot:
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    rotation: int
    annotations: tuple[PdfAnnotationSnapshot, ...]


@dataclass(frozen=True, slots=True)
class PdfStructureSnapshot:
    page_count: int
    pages: tuple[PdfPageSnapshot, ...]


@dataclass(frozen=True, slots=True)
class PdfiumPageSnapshot:
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    visible_box: tuple[float, float, float, float]
    rotation: int
    rendered_size: tuple[int, int]


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
            annotations: list[PdfAnnotationSnapshot] = []
            annots_obj = page.get("/Annots", None)
            if annots_obj is not None:
                if not isinstance(annots_obj, pikepdf.Array):
                    raise AssertionError(f"{path.name} page {page_index}: /Annots must be an array")
                for annot_ref in annots_obj:
                    annot = dereference(annot_ref)
                    subtype = str(annot.get("/Subtype", ""))
                    if not subtype:
                        raise AssertionError(
                            f"{path.name} page {page_index}: annotation /Subtype is missing"
                        )
                    rect = normalize_box(annot.get("/Rect", None))
                    if not box_within(rect, media_box):
                        raise AssertionError(
                            f"{path.name} page {page_index}: annotation rect {rect!r} "
                            f"must stay within MediaBox {media_box!r}"
                        )
                    annotations.append(
                        PdfAnnotationSnapshot(
                            subtype=subtype,
                            rect=rect,
                            has_appearance="/AP" in annot,
                        )
                    )
            pages.append(
                PdfPageSnapshot(
                    media_box=media_box,
                    crop_box=crop_box,
                    rotation=rotation,
                    annotations=tuple(annotations),
                )
            )
        return PdfStructureSnapshot(page_count=len(pages), pages=tuple(pages))


def inspect_pdfium_pages(path: Path, *, scale: float = 0.5) -> tuple[PdfiumPageSnapshot, ...]:
    document = pdfium.PdfDocument(str(path))
    snapshots: list[PdfiumPageSnapshot] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            bitmap: Any | None = None
            pil_image: Image.Image | None = None
            try:
                media_box = normalize_box(page.get_mediabox(fallback_ok=True))
                crop_box = normalize_box(page.get_cropbox(fallback_ok=True))
                visible_box = normalize_box(page.get_bbox())
                rotation = normalize_rotation(int(page.get_rotation()))
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil().convert("RGBA")
                rendered_size = (pil_image.width, pil_image.height)
                if rendered_size[0] <= 0 or rendered_size[1] <= 0:
                    raise AssertionError(
                        f"{path.name} page {page_index}: rendered image dimensions must be positive"
                    )
                snapshots.append(
                    PdfiumPageSnapshot(
                        media_box=media_box,
                        crop_box=crop_box,
                        visible_box=visible_box,
                        rotation=rotation,
                        rendered_size=rendered_size,
                    )
                )
            finally:
                if pil_image is not None:
                    pil_image.close()
                if bitmap is not None and hasattr(bitmap, "close"):
                    bitmap.close()
                page.close()
    finally:
        document.close()
    return tuple(snapshots)


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
    structure_snapshot = inspect_pdf_structure(path)
    pdfium_snapshot = inspect_pdfium_pages(path)
    expected_page_count = int(expected["page_count"])
    expected_pages = expected["pages"]
    if not isinstance(expected_pages, list):
        raise AssertionError(f"{path.name}: expected pages list, got {type(expected_pages)!r}")
    if structure_snapshot.page_count != expected_page_count:
        raise AssertionError(
            f"{path.name}: page_count actual={structure_snapshot.page_count} "
            f"expected={expected_page_count}"
        )
    if len(pdfium_snapshot) != expected_page_count:
        raise AssertionError(
            f"{path.name}: PDFium page_count actual={len(pdfium_snapshot)} "
            f"expected={expected_page_count}"
        )
    if len(expected_pages) != expected_page_count:
        raise AssertionError(
            f"{path.name}: manifest pages length {len(expected_pages)} != {expected_page_count}"
        )
    for page_index, (structure_page, pdfium_page, expected_page) in enumerate(
        zip(structure_snapshot.pages, pdfium_snapshot, expected_pages, strict=True)
    ):
        if not isinstance(expected_page, Mapping):
            raise AssertionError(f"{path.name} page {page_index}: manifest entry must be a mapping")
        assert_boxes_close(structure_page.media_box, expected_page["media_box"])  # type: ignore[arg-type]
        assert_boxes_close(structure_page.crop_box, expected_page["crop_box"])  # type: ignore[arg-type]
        assert_boxes_close(pdfium_page.media_box, expected_page["media_box"])  # type: ignore[arg-type]
        assert_boxes_close(pdfium_page.crop_box, expected_page["crop_box"])  # type: ignore[arg-type]
        assert_boxes_close(pdfium_page.visible_box, expected_page["visible_box"])  # type: ignore[arg-type]
        expected_rotation = normalize_rotation(int(expected_page["rotation"]))
        if structure_page.rotation != expected_rotation:
            raise AssertionError(
                f"{path.name} page {page_index}: structure rotation "
                f"actual={structure_page.rotation} expected={expected_rotation}"
            )
        if pdfium_page.rotation != expected_rotation:
            raise AssertionError(
                f"{path.name} page {page_index}: PDFium rotation "
                f"actual={pdfium_page.rotation} expected={expected_rotation}"
            )
        expected_annotations = expected_page["annotations"]  # type: ignore[index]
        if not isinstance(expected_annotations, list):
            raise AssertionError(
                f"{path.name} page {page_index}: expected annotations list, "
                f"got {type(expected_annotations)!r}"
            )
        assert_annotations_match(
            path,
            page_index=page_index,
            actual=structure_page.annotations,
            expected=expected_annotations,
            media_box=structure_page.media_box,
        )


def assert_annotations_match(
    path: Path,
    *,
    page_index: int,
    actual: Sequence[PdfAnnotationSnapshot],
    expected: Sequence[object],
    media_box: tuple[float, float, float, float],
) -> None:
    if len(actual) != len(expected):
        raise AssertionError(
            f"{path.name} page {page_index}: annotation count {len(actual)} != {len(expected)}"
        )
    for annotation_index, (actual_annotation, expected_annotation) in enumerate(
        zip(actual, expected, strict=True)
    ):
        if not isinstance(expected_annotation, Mapping):
            raise AssertionError(
                f"{path.name} page {page_index}: annotation expectation must be a mapping"
            )
        if actual_annotation.subtype != str(expected_annotation["subtype"]):
            raise AssertionError(
                f"{path.name} page {page_index}: annotation {annotation_index} subtype "
                f"{actual_annotation.subtype!r} != {expected_annotation['subtype']!r}"
            )
        assert_boxes_close(actual_annotation.rect, expected_annotation["rect"])  # type: ignore[arg-type]
        if actual_annotation.has_appearance is not bool(expected_annotation["has_appearance"]):
            raise AssertionError(
                f"{path.name} page {page_index}: annotation {annotation_index} "
                f"has_appearance {actual_annotation.has_appearance!r} != "
                f"{expected_annotation['has_appearance']!r}"
            )
        if not box_within(actual_annotation.rect, media_box):
            raise AssertionError(
                f"{path.name} page {page_index}: annotation {annotation_index} "
                f"rect {actual_annotation.rect!r} must stay within MediaBox {media_box!r}"
            )


def box_within(
    inner: Sequence[float],
    outer: Sequence[float],
    *,
    tolerance: float = 0.01,
) -> bool:
    return (
        float(inner[0]) >= float(outer[0]) - tolerance
        and float(inner[1]) >= float(outer[1]) - tolerance
        and float(inner[2]) <= float(outer[2]) + tolerance
        and float(inner[3]) <= float(outer[3]) + tolerance
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
            text_page = None
            try:
                text_page = page.get_textpage()
                chunks.append(text_page.get_text_range())
            finally:
                if text_page is not None:
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
    rgba.close()
    white.close()
    rgb = composite.convert("RGB")
    composite.close()
    return rgb


def assert_image_has_non_background_content(
    image: Image.Image,
    *,
    fixture_name: str,
    page_index: int,
    background: tuple[int, int, int] = (255, 255, 255),
) -> None:
    flattened = flatten_on_white(image)
    background_image = None
    diff = None
    try:
        background_image = Image.new("RGB", flattened.size, background)
        diff = ImageChops.difference(flattened, background_image)
        if diff.getbbox() is None:
            raise AssertionError(
                f"{fixture_name} page {page_index}: content-bearing page rendered as blank white"
            )
    finally:
        if diff is not None:
            diff.close()
        if background_image is not None:
            background_image.close()
        flattened.close()


def assert_images_visually_close(
    actual: Image.Image,
    expected: Image.Image,
    *,
    tolerance: VisualComparisonTolerance = DEFAULT_VISUAL_TOLERANCE,
    label: str = "image comparison",
) -> None:
    actual_flat = flatten_on_white(actual)
    expected_flat = flatten_on_white(expected)
    diff: Image.Image | None = None
    channels: tuple[Image.Image, ...] = ()
    rg_max: Image.Image | None = None
    channel_max: Image.Image | None = None
    try:
        if actual_flat.size != expected_flat.size:
            raise AssertionError(
                f"{label}: image dimensions differ "
                f"actual={actual_flat.size} expected={expected_flat.size}"
            )
        diff = ImageChops.difference(actual_flat, expected_flat)
        channels = diff.split()
        pixel_count = actual_flat.width * actual_flat.height
        total_channels = pixel_count * 3
        weighted_sum = 0
        for channel in channels:
            histogram = channel.histogram()
            for value, count in enumerate(histogram):
                weighted_sum += value * count
        rg_max = ImageChops.lighter(channels[0], channels[1])
        channel_max = ImageChops.lighter(rg_max, channels[2])
        significant_pixels = sum(
            count
            for value, count in enumerate(channel_max.histogram())
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
        for channel in channels:
            channel.close()
        if rg_max is not None:
            rg_max.close()
        if diff is not None:
            diff.close()
        if channel_max is not None:
            channel_max.close()
        actual_flat.close()
        expected_flat.close()


def dereference(value: Any) -> Any:
    return value.get_object() if hasattr(value, "get_object") else value
