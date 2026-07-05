from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter

from pdf_workbench.services.page_coordinates import (
    PageCoordinateMapper,
    PageGeometry,
    PdfPoint,
    PdfRect,
)


def make_geometry(rotation: int = 0) -> PageGeometry:
    return PageGeometry(
        media_box=PdfRect(0.0, 0.0, 200.0, 300.0),
        crop_box=PdfRect(20.0, 30.0, 180.0, 260.0),
        visible_box=PdfRect(20.0, 30.0, 180.0, 260.0),
        intrinsic_rotation=rotation,
    )


def assert_point_close(actual: PdfPoint, expected: PdfPoint) -> None:
    assert actual.x == pytest.approx(expected.x)
    assert actual.y == pytest.approx(expected.y)


def assert_rect_close(actual: PdfRect, expected: PdfRect) -> None:
    assert actual.left == pytest.approx(expected.left)
    assert actual.bottom == pytest.approx(expected.bottom)
    assert actual.right == pytest.approx(expected.right)
    assert actual.top == pytest.approx(expected.top)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_page_coordinate_mapper_round_trips_points(rotation: int) -> None:
    mapper = PageCoordinateMapper(
        geometry=make_geometry(),
        additional_rotation=rotation,
        logical_zoom=1.5,
        device_pixel_ratio=2.0,
    )
    points = [
        PdfPoint(25.0, 35.0),
        PdfPoint(75.0, 120.0),
        PdfPoint(179.0, 259.0),
    ]

    for point in points:
        view_point = mapper.pdf_to_view_point(point)
        assert_point_close(mapper.view_to_pdf_point(view_point), point)
        device_point = mapper.pdf_to_device_point(point)
        assert_point_close(mapper.device_to_pdf_point(device_point), point)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_page_coordinate_mapper_round_trips_rectangles(rotation: int) -> None:
    mapper = PageCoordinateMapper(
        geometry=make_geometry(),
        additional_rotation=rotation,
        logical_zoom=1.25,
        device_pixel_ratio=1.5,
    )
    rect = PdfRect(30.0, 40.0, 70.0, 90.0)

    assert_rect_close(mapper.view_to_pdf_rect(mapper.pdf_to_view_rect(rect)), rect)
    assert_rect_close(mapper.device_to_pdf_rect(mapper.pdf_to_device_rect(rect)), rect)


def test_pdf_rect_helpers_validate_and_intersect() -> None:
    rect = PdfRect.from_tuple((1.0, 2.0, 5.0, 8.0))

    assert rect.width == 4.0
    assert rect.height == 6.0
    assert rect.center == PdfPoint(3.0, 5.0)
    assert rect.corners() == (
        PdfPoint(1.0, 2.0),
        PdfPoint(5.0, 2.0),
        PdfPoint(5.0, 8.0),
        PdfPoint(1.0, 8.0),
    )
    assert rect.intersection(PdfRect(3.0, 4.0, 9.0, 10.0)) == PdfRect(3.0, 4.0, 5.0, 8.0)
    assert rect.intersection(PdfRect(6.0, 4.0, 9.0, 10.0)) is None


@pytest.mark.parametrize(
    "values",
    [
        (0.0, 0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0, 0.0),
        (float("nan"), 0.0, 1.0, 1.0),
        (0.0, float("inf"), 1.0, 1.0),
    ],
)
def test_pdf_rect_rejects_invalid_values(values: tuple[float, float, float, float]) -> None:
    with pytest.raises(ValueError):
        PdfRect(*values)


def test_pdf_rect_normalized_from_reversed_edges() -> None:
    rect = PdfRect.normalized((5.0, 8.0, 1.0, 2.0))

    assert rect == PdfRect(1.0, 2.0, 5.0, 8.0)


def test_page_geometry_from_pdfium_page_uses_crop_and_media_boxes(tmp_path: Path) -> None:
    from pypdfium2 import PdfDocument

    pdf_path = tmp_path / "geometry.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=300)
    page.cropbox.lower_left = (20, 30)
    page.cropbox.upper_right = (180, 260)
    with pdf_path.open("wb") as stream:
        writer.write(stream)

    document = PdfDocument(str(pdf_path))
    try:
        pdfium_page = document[0]
        geometry = PageGeometry.from_pdfium_page(pdfium_page)
    finally:
        pdfium_page.close()
        document.close()

    assert geometry.media_box == PdfRect(0.0, 0.0, 200.0, 300.0)
    assert geometry.crop_box == PdfRect(20.0, 30.0, 180.0, 260.0)
    assert geometry.visible_box == PdfRect(20.0, 30.0, 180.0, 260.0)
    assert geometry.intrinsic_rotation == 0
