from __future__ import annotations

import math
from pathlib import Path

import pytest

from pdf_workbench.domain.image_to_pdf import (
    A4_PORTRAIT_POINTS,
    ImageInput,
    ImageScalingMode,
    PdfOrientation,
    PdfPageSizeMode,
    build_image_to_pdf_plan,
    build_page_geometry,
    margins_from_mm,
    mm_to_points,
    normalize_dpi,
)


def image_input(
    path: Path,
    *,
    frame_count: int = 1,
    width: int = 100,
    height: int = 50,
) -> ImageInput:
    return ImageInput(
        path=path.resolve(),
        label=path.name,
        detected_format="PNG",
        pixel_width=width,
        pixel_height=height,
        frame_count=frame_count,
        color_mode="RGB",
        has_alpha=False,
        has_icc_profile=False,
        exif_orientation=None,
    )


def test_frame_mapping_survives_reorder_and_multiframe(tmp_path: Path) -> None:
    first = image_input(tmp_path / "first.png", frame_count=2)
    second = image_input(tmp_path / "second.png", frame_count=1)

    plan = build_image_to_pdf_plan((second, first), tmp_path / "out.pdf")

    assert plan.total_page_count == 3
    assert tuple(frame.input_label for frame in plan.frame_mapping) == (
        "second.png",
        "first.png",
        "first.png",
    )
    assert tuple(frame.frame_index for frame in plan.frame_mapping) == (0, 0, 1)


def test_plan_rejects_duplicate_canonical_paths_and_output_collision(tmp_path: Path) -> None:
    first = image_input(tmp_path / "same.png")

    with pytest.raises(ValueError, match="unique"):
        build_image_to_pdf_plan((first, first), tmp_path / "out.pdf")
    with pytest.raises(ValueError, match="output"):
        build_image_to_pdf_plan((first,), first.path)


def test_mm_to_points_and_bool_rejection() -> None:
    assert mm_to_points(25.4) == pytest.approx(72.0)
    with pytest.raises(ValueError, match="finite"):
        mm_to_points(True)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0.0, -1.0, math.inf, math.nan, 2401.0])
def test_invalid_dpi_falls_back_to_default(value: float) -> None:
    assert normalize_dpi(value, 96.0) == 96.0


def test_a4_auto_orientation_and_fit_geometry_preserve_aspect_ratio(tmp_path: Path) -> None:
    plan = build_image_to_pdf_plan(
        (image_input(tmp_path / "wide.png", width=400, height=200),),
        tmp_path / "out.pdf",
        page_size_mode=PdfPageSizeMode.A4,
        orientation=PdfOrientation.AUTO,
        margins=margins_from_mm(10, 10, 10, 10),
    )

    geometry = build_page_geometry(pixel_width=400, pixel_height=200, dpi_x=96, dpi_y=96, plan=plan)

    assert geometry.page_width == pytest.approx(A4_PORTRAIT_POINTS[1])
    assert geometry.page_height == pytest.approx(A4_PORTRAIT_POINTS[0])
    assert geometry.image_width / geometry.image_height == pytest.approx(2.0)
    assert geometry.source_crop_box is None


def test_fill_geometry_uses_center_crop(tmp_path: Path) -> None:
    plan = build_image_to_pdf_plan(
        (image_input(tmp_path / "wide.png", width=400, height=100),),
        tmp_path / "out.pdf",
        page_size_mode=PdfPageSizeMode.A4,
        scaling_mode=ImageScalingMode.FILL,
        margins=margins_from_mm(10, 10, 10, 10),
    )

    geometry = build_page_geometry(pixel_width=400, pixel_height=100, dpi_x=96, dpi_y=96, plan=plan)

    assert geometry.source_crop_box is not None
    left, top, right, bottom = geometry.source_crop_box
    assert left > 0
    assert top == 0
    assert right < 400
    assert bottom == 100


def test_actual_size_rejects_image_larger_than_usable_rect(tmp_path: Path) -> None:
    plan = build_image_to_pdf_plan(
        (image_input(tmp_path / "huge.png", width=5000, height=5000),),
        tmp_path / "out.pdf",
        page_size_mode=PdfPageSizeMode.A4,
        scaling_mode=ImageScalingMode.ACTUAL_SIZE,
    )

    with pytest.raises(ValueError, match="actual-size"):
        build_page_geometry(pixel_width=5000, pixel_height=5000, dpi_x=96, dpi_y=96, plan=plan)
