from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

import pikepdf
import pytest

from pdf_regression_utils import (
    VisualComparisonTolerance,
    assert_images_visually_close,
    assert_pdf_contains_text,
    assert_pdf_matches_manifest,
    assert_pdfium_renders_all_pages,
    compatibility_fixture_dir,
    file_sha256,
    flatten_on_white,
    inspect_pdf_structure,
    load_compatibility_manifest,
    normalize_rotation,
    render_pdf_pages,
)


@pytest.fixture(scope="module")
def manifest() -> dict[str, object]:
    payload = load_compatibility_manifest()
    if int(payload["schema_version"]) != 1:
        raise AssertionError(f"unexpected schema version: {payload['schema_version']!r}")
    return payload


@pytest.fixture(scope="module")
def fixture_map(manifest: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    fixtures = manifest["fixtures"]
    if not isinstance(fixtures, list):
        raise AssertionError("manifest fixtures must be a list")
    result: dict[str, Mapping[str, object]] = {}
    for item in fixtures:
        if not isinstance(item, Mapping):
            raise AssertionError("manifest fixture entry must be a mapping")
        result[str(item["file"])] = item
    return result


def test_manifest_integrity(
    manifest: Mapping[str, object],
    fixture_map: Mapping[str, Mapping[str, object]],
) -> None:
    fixture_dir = compatibility_fixture_dir()
    manifest_files = set(fixture_map)
    assert len(manifest_files) == len(fixture_map)
    actual_files = {path.name for path in fixture_dir.glob("*.pdf")}
    assert manifest_files == actual_files

    for name, item in fixture_map.items():
        path = fixture_dir / name
        assert path.exists()
        assert file_sha256(path) == item["sha256"]
        provenance = item["provenance"]
        assert isinstance(provenance, Mapping)
        assert provenance["kind"]
        assert provenance["generator"]
        assert provenance["license"]
        expected = item["expected"]
        assert isinstance(expected, Mapping)
        page_count = int(expected["page_count"])
        pages = expected["pages"]
        assert isinstance(pages, list)
        assert len(pages) == page_count
        for page in pages:
            assert isinstance(page, Mapping)
            assert normalize_rotation(int(page["rotation"])) in {0, 90, 180, 270}
            for box_name in ("media_box", "crop_box"):
                box = page[box_name]
                assert len(box) == 4
                assert all(math.isfinite(float(value)) for value in box)


def test_all_fixtures_match_structural_manifest(
    fixture_map: Mapping[str, Mapping[str, object]],
) -> None:
    fixture_dir = compatibility_fixture_dir()
    for name, item in fixture_map.items():
        assert_pdf_matches_manifest(fixture_dir / name, item["expected"])  # type: ignore[arg-type]


def test_pdfium_renders_all_fixture_pages(fixture_map: Mapping[str, Mapping[str, object]]) -> None:
    fixture_dir = compatibility_fixture_dir()
    for name, item in fixture_map.items():
        expected = item["expected"]
        assert isinstance(expected, Mapping)
        images = assert_pdfium_renders_all_pages(
            fixture_dir / name,
            expected_page_count=int(expected["page_count"]),
        )
        for image in images:
            flattened = flatten_on_white(image)
            flattened.close()
            image.close()


def test_pdfium_geometry_matches_manifest(fixture_map: Mapping[str, Mapping[str, object]]) -> None:
    fixture_dir = compatibility_fixture_dir()
    for name, item in fixture_map.items():
        expected = item["expected"]
        assert isinstance(expected, Mapping)
        snapshot = inspect_pdf_structure(fixture_dir / name)
        assert snapshot.page_count == int(expected["page_count"])


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
