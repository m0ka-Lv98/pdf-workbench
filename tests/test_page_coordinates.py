from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QPolygonF

from pdf_workbench.services.page_coordinates import (
    PageCoordinateMapper,
    PageGeometry,
    PageMetadata,
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


def assert_point_close_qt(actual: QPointF, expected: QPointF) -> None:
    assert actual.x() == pytest.approx(expected.x(), abs=1.0)
    assert actual.y() == pytest.approx(expected.y(), abs=1.0)


def assert_point_close_pdf(actual: PdfPoint, expected: PdfPoint) -> None:
    assert actual.x == pytest.approx(expected.x)
    assert actual.y == pytest.approx(expected.y)


def assert_rect_close(actual: PdfRect, expected: PdfRect) -> None:
    assert actual.left == pytest.approx(expected.left)
    assert actual.bottom == pytest.approx(expected.bottom)
    assert actual.right == pytest.approx(expected.right)
    assert actual.top == pytest.approx(expected.top)


def assert_qrect_close(actual: QRectF, expected: QRectF) -> None:
    assert actual.left() == pytest.approx(expected.left())
    assert actual.top() == pytest.approx(expected.top())
    assert actual.width() == pytest.approx(expected.width())
    assert actual.height() == pytest.approx(expected.height())


def _coords(point: object) -> tuple[float, float]:
    if hasattr(point, "x") and hasattr(point, "y"):
        x_attr = point.x  # type: ignore[attr-defined]
        y_attr = point.y  # type: ignore[attr-defined]
        x = x_attr() if callable(x_attr) else x_attr
        y = y_attr() if callable(y_attr) else y_attr
        return float(x), float(y)
    x, y = point  # type: ignore[misc]
    return float(x), float(y)


def _build_oracle_pdf(path: Path, intrinsic_rotation: int) -> Path:
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    page.mediabox.lower_left = (-20, -10)
    page.mediabox.upper_right = (580, 790)
    page.cropbox.lower_left = (50, 100)
    page.cropbox.upper_right = (450, 700)
    page.rotate(intrinsic_rotation)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


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
        assert_point_close_pdf(mapper.view_to_pdf_point(view_point), point)
        device_point = mapper.pdf_to_device_point(point)
        assert_point_close_pdf(mapper.device_to_pdf_point(device_point), point)


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


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_page_coordinate_mapper_round_trips_polygons(rotation: int) -> None:
    mapper = PageCoordinateMapper(
        geometry=make_geometry(),
        additional_rotation=rotation,
        logical_zoom=1.1,
        device_pixel_ratio=1.25,
    )
    polygon = (
        PdfPoint(22.0, 32.0),
        PdfPoint(50.0, 32.0),
        PdfPoint(50.0, 60.0),
        PdfPoint(22.0, 60.0),
    )

    assert tuple(mapper.view_to_pdf_polygon(mapper.pdf_to_view_polygon(polygon))) == polygon
    assert tuple(mapper.device_to_pdf_polygon(mapper.pdf_to_device_polygon(polygon))) == polygon


@pytest.mark.parametrize("intrinsic_rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("additional_rotation", [0, 90, 180, 270])
def test_page_coordinate_mapper_uses_pdfium_posconv_oracle(
    tmp_path: Path,
    intrinsic_rotation: int,
    additional_rotation: int,
) -> None:
    from pypdfium2 import PdfDocument

    pdf_path = _build_oracle_pdf(tmp_path / f"oracle-{intrinsic_rotation}.pdf", intrinsic_rotation)

    document = PdfDocument(str(pdf_path))
    max_error_x = 0.0
    max_error_y = 0.0
    try:
        pdfium_page = document[0]
        geometry = PageGeometry.from_pdfium_page(pdfium_page)
        mapper = PageCoordinateMapper(
            geometry=geometry,
            additional_rotation=additional_rotation,
            logical_zoom=1.5,
            device_pixel_ratio=2.0,
        )
        bitmap = pdfium_page.render(
            scale=mapper.logical_zoom * mapper.device_pixel_ratio,
            rotation=additional_rotation,
        )
        posconv = bitmap.get_posconv(pdfium_page)

        points = [
            geometry.visible_box.bottom_left,
            geometry.visible_box.bottom_right,
            geometry.visible_box.top_left,
            geometry.visible_box.top_right,
            geometry.visible_box.center,
            PdfPoint(123.0, 234.0),
        ]
        for pdf_point in points:
            device_point = mapper.pdf_to_device_point(pdf_point)
            oracle_point = posconv.to_bitmap(pdf_point.x, pdf_point.y)
            expected = QPointF(*_coords(oracle_point))
            max_error_x = max(max_error_x, abs(device_point.x() - expected.x()))
            max_error_y = max(max_error_y, abs(device_point.y() - expected.y()))
            assert_point_close_qt(device_point, expected)
            back = mapper.device_to_pdf_point(device_point)
            assert_point_close_pdf(back, pdf_point)
            oracle_pdf_point = PdfPoint(
                *_coords(
                    posconv.to_page(
                        round(device_point.x()),
                        round(device_point.y()),
                    )
                )
            )
            assert oracle_pdf_point.x == pytest.approx(pdf_point.x, abs=1.0)
            assert oracle_pdf_point.y == pytest.approx(pdf_point.y, abs=1.0)
    finally:
        pdfium_page.close()
        document.close()

    assert max_error_x <= 1.0
    assert max_error_y <= 1.0


@pytest.mark.parametrize("zoom,dpr", [(1.0, 1.0), (1.5, 2.0), (2.75, 1.25)])
def test_page_coordinate_mapper_respects_zoom_and_dpr_independently(
    zoom: float,
    dpr: float,
) -> None:
    geometry = make_geometry()
    mapper = PageCoordinateMapper(
        geometry,
        additional_rotation=0,
        logical_zoom=zoom,
        device_pixel_ratio=dpr,
    )
    baseline = PageCoordinateMapper(
        geometry,
        additional_rotation=0,
        logical_zoom=zoom,
        device_pixel_ratio=1.0,
    )

    point = PdfPoint(33.0, 77.0)
    assert mapper.view_size == baseline.view_size
    assert_point_close_qt(
        mapper.pdf_to_view_point(point),
        baseline.pdf_to_view_point(point),
    )
    rect = PdfRect(30.0, 40.0, 70.0, 90.0)
    assert_qrect_close(mapper.pdf_to_view_rect(rect), baseline.pdf_to_view_rect(rect))


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_polygon_conversion_preserves_vertices_and_order(rotation: int) -> None:
    mapper = PageCoordinateMapper(
        geometry=make_geometry(rotation=90),
        additional_rotation=rotation,
        logical_zoom=1.75,
        device_pixel_ratio=1.5,
    )
    polygons = [
        QPolygonF(),
        QPolygonF([QPointF(25.0, 35.0)]),
        QPolygonF([QPointF(25.0, 35.0), QPointF(40.0, 50.0)]),
        QPolygonF([QPointF(25.0, 35.0), QPointF(35.0, 80.0), QPointF(60.0, 45.0)]),
        QPolygonF(
            [
                QPointF(25.0, 35.0),
                QPointF(60.0, 35.0),
                QPointF(45.0, 55.0),
                QPointF(60.0, 80.0),
                QPointF(25.0, 80.0),
            ]
        ),
    ]
    for polygon in polygons:
        mapped = mapper.pdf_to_view_polygon(
            tuple(PdfPoint(point.x(), point.y()) for point in polygon)
        )
        assert mapped.count() == polygon.count()
        round_tripped = mapper.view_to_pdf_polygon(mapped)
        assert len(round_tripped) == polygon.count()


def test_invalid_rectangles_rejected_by_mapper() -> None:
    mapper = PageCoordinateMapper(make_geometry(), 0, 1.0, 1.0)
    with pytest.raises(ValueError):
        mapper.pdf_to_view_rect(PdfRect(10.0, 10.0, 10.0, 20.0))
    with pytest.raises(ValueError):
        mapper.pdf_to_view_rect(PdfRect(10.0, 10.0, 20.0, 10.0))


def test_page_coordinate_mapper_rejects_invalid_scale_and_rotation() -> None:
    geometry = make_geometry()

    with pytest.raises(ValueError):
        PageCoordinateMapper(
            geometry,
            additional_rotation=45,
            logical_zoom=1.0,
            device_pixel_ratio=1.0,
        )
    with pytest.raises(ValueError):
        PageCoordinateMapper(
            geometry,
            additional_rotation=0,
            logical_zoom=0.0,
            device_pixel_ratio=1.0,
        )
    with pytest.raises(ValueError):
        PageCoordinateMapper(
            geometry,
            additional_rotation=0,
            logical_zoom=float("nan"),
            device_pixel_ratio=1.0,
        )
    with pytest.raises(ValueError):
        PageCoordinateMapper(
            geometry,
            additional_rotation=0,
            logical_zoom=1.0,
            device_pixel_ratio=float("inf"),
        )


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


def test_page_geometry_rejects_invalid_rotation() -> None:
    box = PdfRect(0.0, 0.0, 10.0, 20.0)
    with pytest.raises(ValueError):
        PageGeometry(box, box, box, 45)


def test_page_metadata_from_size_derives_geometry() -> None:
    metadata = PageMetadata.from_size(144.0, 200.0, intrinsic_rotation=90)

    assert metadata.geometry.media_box == PdfRect(0.0, 0.0, 144.0, 200.0)
    assert metadata.geometry.crop_box == PdfRect(0.0, 0.0, 144.0, 200.0)
    assert metadata.geometry.visible_box == PdfRect(0.0, 0.0, 144.0, 200.0)
    assert metadata.width_points == 144.0
    assert metadata.height_points == 200.0
    assert metadata.geometry.intrinsic_rotation == 90


@pytest.mark.parametrize("width,height", [(0.0, 200.0), (-1.0, 200.0), (144.0, 0.0), (144.0, -1.0)])
def test_page_metadata_from_size_rejects_invalid_extents(width: float, height: float) -> None:
    with pytest.raises(ValueError):
        PageMetadata.from_size(width, height)


def test_page_metadata_from_size_rejects_invalid_rotation() -> None:
    with pytest.raises(ValueError):
        PageMetadata.from_size(144.0, 200.0, intrinsic_rotation=45)


def test_page_geometry_from_pdfium_page_uses_bbox(tmp_path: Path) -> None:
    from pypdfium2 import PdfDocument

    pdf_path = tmp_path / "geometry.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=300)
    page.cropbox.lower_left = (20, 30)
    page.cropbox.upper_right = (180, 260)
    with pdf_path.open("wb") as stream:
        writer.write(stream)

    document = PdfDocument(str(pdf_path))
    closed = False
    try:
        pdfium_page = document[0]
        original_close = pdfium_page.close

        def tracked_close() -> None:
            nonlocal closed
            closed = True
            original_close()

        pdfium_page.close = tracked_close  # type: ignore[assignment]
        geometry = PageGeometry.from_pdfium_page(pdfium_page)
    finally:
        if not closed:
            pdfium_page.close()
        document.close()

    assert geometry.media_box == PdfRect(0.0, 0.0, 200.0, 300.0)
    assert geometry.crop_box == PdfRect(20.0, 30.0, 180.0, 260.0)
    assert geometry.visible_box == PdfRect(20.0, 30.0, 180.0, 260.0)
    assert geometry.intrinsic_rotation == 0
    assert closed is True
