from __future__ import annotations

import math
import re
from collections.abc import Mapping
from pathlib import Path

import pikepdf
import pytest

from pdf_regression_utils import (
    VisualComparisonTolerance,
    assert_image_has_non_background_content,
    assert_images_visually_close,
    assert_pdf_contains_text,
    assert_pdf_matches_manifest,
    assert_pdfium_renders_all_pages,
    box_within,
    compatibility_fixture_dir,
    file_sha256,
    flatten_on_white,
    inspect_pdf_structure,
    inspect_pdfium_pages,
    load_compatibility_manifest,
    normalize_box,
    normalize_rotation,
    render_pdf_pages,
)

SUBSET_FONT_NAME_PATTERN = re.compile(r"^/[A-Z]{6}\+")


@pytest.fixture(scope="module")
def manifest() -> dict[str, object]:
    payload = load_compatibility_manifest()
    if int(payload["schema_version"]) != 1:
        raise AssertionError(f"unexpected schema version: {payload['schema_version']!r}")
    return payload


@pytest.fixture(scope="module")
def fixture_entries(manifest: Mapping[str, object]) -> list[Mapping[str, object]]:
    fixtures = manifest["fixtures"]
    if not isinstance(fixtures, list):
        raise AssertionError("manifest fixtures must be a list")
    result: list[Mapping[str, object]] = []
    for item in fixtures:
        if not isinstance(item, Mapping):
            raise AssertionError("manifest fixture entry must be a mapping")
        result.append(item)
    names = [str(item["file"]) for item in result]
    assert len(names) == len(set(names))
    return result


@pytest.fixture(scope="module")
def fixture_map(fixture_entries: list[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    return {str(item["file"]): item for item in fixture_entries}


def test_manifest_integrity(
    fixture_entries: list[Mapping[str, object]],
    fixture_map: Mapping[str, Mapping[str, object]],
) -> None:
    fixture_dir = compatibility_fixture_dir()
    actual_files = {path.name for path in fixture_dir.glob("*.pdf")}
    assert set(fixture_map) == actual_files

    for item in fixture_entries:
        name = str(item["file"])
        path = fixture_dir / name
        assert path.exists()
        assert file_sha256(path) == item["sha256"]
        provenance = item["provenance"]
        assert isinstance(provenance, Mapping)
        assert provenance["kind"]
        assert provenance["generator"]
        assert provenance["license"]
        font = provenance["font"]
        assert isinstance(font, Mapping)
        assert font["family"]
        assert font["file_name"]
        assert font["version"]
        assert font["license"]
        expected = item["expected"]
        assert isinstance(expected, Mapping)
        page_count = int(expected["page_count"])
        assert isinstance(expected["content_bearing"], bool)
        pages = expected["pages"]
        assert isinstance(pages, list)
        assert len(pages) == page_count
        for page in pages:
            assert isinstance(page, Mapping)
            assert normalize_rotation(int(page["rotation"])) in {0, 90, 180, 270}
            media_box = normalize_box(page["media_box"])  # type: ignore[arg-type]
            crop_box = normalize_box(page["crop_box"])  # type: ignore[arg-type]
            visible_box = normalize_box(page["visible_box"])  # type: ignore[arg-type]
            for box in (media_box, crop_box, visible_box):
                assert len(box) == 4
                assert all(math.isfinite(float(value)) for value in box)
            assert box_within(crop_box, media_box)
            assert box_within(visible_box, media_box)
            annotations = page["annotations"]
            assert isinstance(annotations, list)
            assert len(page["annotation_subtypes"]) == len(annotations)
            expected_subtypes = [str(annotation["subtype"]) for annotation in annotations]
            assert page["annotation_subtypes"] == expected_subtypes
            for annotation in annotations:
                assert isinstance(annotation, Mapping)
                assert str(annotation["subtype"])
                rect = normalize_box(annotation["rect"])  # type: ignore[arg-type]
                assert box_within(rect, media_box)
                assert isinstance(annotation["has_appearance"], bool)


def test_all_fixtures_match_structural_manifest(
    fixture_map: Mapping[str, Mapping[str, object]],
) -> None:
    fixture_dir = compatibility_fixture_dir()
    for name, item in fixture_map.items():
        assert_pdf_matches_manifest(fixture_dir / name, item["expected"])  # type: ignore[arg-type]


def test_pdfium_renders_all_fixture_pages(
    fixture_map: Mapping[str, Mapping[str, object]],
) -> None:
    fixture_dir = compatibility_fixture_dir()
    for name, item in fixture_map.items():
        expected = item["expected"]
        assert isinstance(expected, Mapping)
        images = assert_pdfium_renders_all_pages(
            fixture_dir / name,
            expected_page_count=int(expected["page_count"]),
        )
        content_bearing = expected["content_bearing"]
        assert isinstance(content_bearing, bool)
        for page_index, image in enumerate(images):
            if content_bearing:
                assert_image_has_non_background_content(
                    image,
                    fixture_name=name,
                    page_index=page_index,
                )
            flattened = flatten_on_white(image)
            flattened.close()
            image.close()


def test_pdfium_geometry_matches_manifest(
    fixture_map: Mapping[str, Mapping[str, object]],
) -> None:
    fixture_dir = compatibility_fixture_dir()
    for name, item in fixture_map.items():
        expected = item["expected"]
        assert isinstance(expected, Mapping)
        structure = inspect_pdf_structure(fixture_dir / name)
        pdfium_pages = inspect_pdfium_pages(fixture_dir / name)
        assert structure.page_count == int(expected["page_count"])
        assert len(pdfium_pages) == int(expected["page_count"])
        for page, pdfium_page in zip(expected["pages"], pdfium_pages, strict=True):  # type: ignore[index]
            assert pdfium_page.rotation == int(page["rotation"])
            assert tuple(float(value) for value in page["visible_box"]) == pdfium_page.visible_box
            assert pdfium_page.rendered_size[0] > 0
            assert pdfium_page.rendered_size[1] > 0


def test_rotation_fixture_uses_non_square_render_sizes(
    fixture_map: Mapping[str, Mapping[str, object]],
) -> None:
    fixture_dir = compatibility_fixture_dir()
    assert "rotations.pdf" in fixture_map
    snapshots = inspect_pdfium_pages(fixture_dir / "rotations.pdf")
    assert len(snapshots) == 4
    assert snapshots[0].rendered_size == snapshots[2].rendered_size
    assert snapshots[1].rendered_size == snapshots[3].rendered_size
    assert snapshots[0].rendered_size[0] > snapshots[0].rendered_size[1]
    assert snapshots[1].rendered_size[0] < snapshots[1].rendered_size[1]
    assert snapshots[0].rendered_size == (
        snapshots[1].rendered_size[1],
        snapshots[1].rendered_size[0],
    )


def test_text_validation_for_english_and_japanese_fixtures(
    fixture_map: Mapping[str, Mapping[str, object]],
) -> None:
    fixture_dir = compatibility_fixture_dir()
    for name in ("english-text.pdf", "japanese-text.pdf"):
        item = fixture_map[name]
        expected = item["expected"]
        assert isinstance(expected, Mapping)
        text_contains = expected["text_contains"]
        assert isinstance(text_contains, list)
        for value in text_contains:
            assert_pdf_contains_text(fixture_dir / name, str(value))


def test_annotations_round_trip_preserves_rectangles_and_appearance(
    tmp_path: Path,
    fixture_map: Mapping[str, Mapping[str, object]],
) -> None:
    source_path = compatibility_fixture_dir() / "annotations.pdf"
    round_trip_path = tmp_path / "annotations.pdf"
    with pikepdf.open(source_path) as pdf:
        pdf.save(round_trip_path)
    expected = fixture_map["annotations.pdf"]["expected"]
    assert isinstance(expected, Mapping)
    assert_pdf_matches_manifest(source_path, expected)  # type: ignore[arg-type]
    assert_pdf_matches_manifest(round_trip_path, expected)  # type: ignore[arg-type]


def test_japanese_fixture_uses_embedded_subset_font() -> None:
    source_path = compatibility_fixture_dir() / "japanese-text.pdf"
    assert_pdf_contains_text(source_path, "PDFワークベンチ 日本語互換性テスト")
    with pikepdf.open(source_path) as pdf:
        page = pdf.pages[0]
        fonts = page.Resources.get("/Font", None)
        assert isinstance(fonts, pikepdf.Dictionary)
        found_embedded_stream = False
        found_subset_name = False
        for font_ref in fonts.values():
            font = font_ref.get_object() if hasattr(font_ref, "get_object") else font_ref
            descendants = font.get("/DescendantFonts", None)
            if descendants is None:
                continue
            for descendant_ref in descendants:
                descendant = (
                    descendant_ref.get_object()
                    if hasattr(descendant_ref, "get_object")
                    else descendant_ref
                )
                descriptor_ref = descendant.get("/FontDescriptor", None)
                if descriptor_ref is None:
                    continue
                descriptor = (
                    descriptor_ref.get_object()
                    if hasattr(descriptor_ref, "get_object")
                    else descriptor_ref
                )
                if any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                    found_embedded_stream = True
                font_name = str(descriptor.get("/FontName", ""))
                if SUBSET_FONT_NAME_PATTERN.match(font_name):
                    found_subset_name = True
        assert found_embedded_stream
        assert found_subset_name


def test_no_full_font_binary_is_committed() -> None:
    compatibility_dir = compatibility_fixture_dir()
    font_like_files = [
        path.name
        for path in compatibility_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".ttf", ".otf", ".ttc", ".otc"}
    ]
    assert font_like_files == []


def test_noop_round_trip_preserves_structure_and_rendering(
    tmp_path: Path,
    fixture_map: Mapping[str, Mapping[str, object]],
) -> None:
    fixture_dir = compatibility_fixture_dir()
    tolerance = VisualComparisonTolerance()
    for name, item in fixture_map.items():
        source_path = fixture_dir / name
        round_trip_path = tmp_path / name
        with pikepdf.open(source_path) as pdf:
            pdf.save(round_trip_path)

        expected = item["expected"]
        assert isinstance(expected, Mapping)
        assert_pdf_matches_manifest(source_path, expected)  # type: ignore[arg-type]
        assert_pdf_matches_manifest(round_trip_path, expected)  # type: ignore[arg-type]

        for value in expected["text_contains"]:  # type: ignore[index]
            assert_pdf_contains_text(round_trip_path, str(value))

        source_images = render_pdf_pages(source_path, scale=0.4)
        round_trip_images = render_pdf_pages(round_trip_path, scale=0.4)
        try:
            assert len(source_images) == len(round_trip_images)
            for page_index, (source_image, round_trip_image) in enumerate(
                zip(source_images, round_trip_images, strict=True)
            ):
                assert_images_visually_close(
                    round_trip_image,
                    source_image,
                    tolerance=tolerance,
                    label=f"{name} page {page_index}",
                )
        finally:
            for image in source_images + round_trip_images:
                image.close()
