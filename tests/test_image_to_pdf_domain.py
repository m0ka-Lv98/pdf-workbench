from __future__ import annotations

import math
from pathlib import Path

import pytest

from pdf_workbench.domain.image_to_pdf import (
    A4_PORTRAIT_POINTS,
    ImageFrameRef,
    ImageInput,
    ImageScalingMode,
    ImageSourceRevision,
    ImageToPdfPlan,
    PageGeometry,
    PageMargins,
    PdfOrientation,
    PdfPageSizeMode,
    TransparencyPolicy,
    build_frame_mapping,
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
    label: str | None = None,
    detected_format: str = "PNG",
    color_mode: str = "RGB",
    exif_orientation: int | None = None,
) -> ImageInput:
    return ImageInput(
        path=path.resolve(),
        label=path.name if label is None else label,
        detected_format=detected_format,
        pixel_width=width,
        pixel_height=height,
        frame_count=frame_count,
        color_mode=color_mode,
        has_alpha=False,
        has_icc_profile=False,
        exif_orientation=exif_orientation,
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


def test_source_revision_rejects_invalid_fields(tmp_path: Path) -> None:
    path = (tmp_path / "source.png").resolve()
    kwargs = {
        "resolved_path": path,
        "size_bytes": 1,
        "modified_time_ns": 1,
        "sha256": "a" * 64,
        "detected_format": "PNG",
        "frame_count": 1,
        "pixel_width": 1,
        "pixel_height": 1,
    }

    with pytest.raises(ValueError, match="size_bytes"):
        ImageSourceRevision(**{**kwargs, "size_bytes": -1})
    with pytest.raises(ValueError, match="modified_time_ns"):
        ImageSourceRevision(**{**kwargs, "modified_time_ns": -1})
    with pytest.raises(ValueError, match="sha256"):
        ImageSourceRevision(**{**kwargs, "sha256": "not-sha"})
    with pytest.raises(ValueError, match="detected_format"):
        ImageSourceRevision(**{**kwargs, "detected_format": ""})
    with pytest.raises(ValueError, match="frame_count"):
        ImageSourceRevision(**{**kwargs, "frame_count": 0})
    with pytest.raises(ValueError, match="pixel_width"):
        ImageSourceRevision(**{**kwargs, "pixel_width": 0})
    with pytest.raises(ValueError, match="pixel_height"):
        ImageSourceRevision(**{**kwargs, "pixel_height": 0})
    with pytest.raises(ValueError, match="resolved"):
        ImageSourceRevision(**{**kwargs, "resolved_path": Path("relative.png")})


def test_image_input_and_frame_ref_reject_invalid_fields(tmp_path: Path) -> None:
    path = (tmp_path / "source.png").resolve()

    with pytest.raises(ValueError, match="label"):
        image_input(path, label=" ")
    with pytest.raises(ValueError, match="detected_format"):
        image_input(path, detected_format="")
    with pytest.raises(ValueError, match="color_mode"):
        image_input(path, color_mode="")
    with pytest.raises(ValueError, match="exif_orientation"):
        image_input(path, exif_orientation=9)
    with pytest.raises(ValueError, match="resolved"):
        ImageInput(
            path=Path("relative.png"),
            label="relative.png",
            detected_format="PNG",
            pixel_width=1,
            pixel_height=1,
            frame_count=1,
            color_mode="RGB",
            has_alpha=False,
            has_icc_profile=False,
            exif_orientation=None,
        )
    with pytest.raises(ValueError, match="pixel_width"):
        image_input(path, width=0)
    with pytest.raises(ValueError, match="pixel_height"):
        image_input(path, height=0)
    with pytest.raises(ValueError, match="frame_count"):
        image_input(path, frame_count=0)
    with pytest.raises(ValueError, match="frame input path"):
        ImageFrameRef(Path("relative.png"), path.name, 0, 0, 0)
    with pytest.raises(ValueError, match="input_index"):
        ImageFrameRef(path, path.name, -1, 0, 0)
    with pytest.raises(ValueError, match="frame_index"):
        ImageFrameRef(path, path.name, 0, -1, 0)
    with pytest.raises(ValueError, match="output_page_index"):
        ImageFrameRef(path, path.name, 0, 0, -1)


def test_page_geometry_rejects_invalid_crop_box() -> None:
    with pytest.raises(ValueError, match="four values"):
        PageGeometry(10.0, 10.0, 0.0, 0.0, 5.0, 5.0, (0.0, 1.0, 2.0))
    with pytest.raises(ValueError, match="finite"):
        PageGeometry(10.0, 10.0, 0.0, 0.0, 5.0, 5.0, (0.0, 1.0, 2.0, math.nan))


def test_plan_rejects_invalid_enum_and_mapping_fields(tmp_path: Path) -> None:
    source = image_input(tmp_path / "source.png")
    output = (tmp_path / "out.pdf").resolve()
    mapping = build_frame_mapping((source,))
    kwargs = {
        "inputs": (source,),
        "output_path": output,
        "page_size_mode": PdfPageSizeMode.FIT_IMAGE,
        "orientation": PdfOrientation.AUTO,
        "scaling_mode": ImageScalingMode.FIT,
        "transparency_policy": TransparencyPolicy.WHITE_BACKGROUND,
        "margins": PageMargins(1.0, 1.0, 1.0, 1.0),
        "custom_page_width_points": None,
        "custom_page_height_points": None,
        "frame_mapping": mapping,
        "total_page_count": 1,
    }

    with pytest.raises(ValueError, match="page_size_mode"):
        ImageToPdfPlan(**{**kwargs, "page_size_mode": "fit_image"})
    with pytest.raises(ValueError, match="orientation"):
        ImageToPdfPlan(**{**kwargs, "orientation": "auto"})
    with pytest.raises(ValueError, match="scaling_mode"):
        ImageToPdfPlan(**{**kwargs, "scaling_mode": "fit"})
    with pytest.raises(ValueError, match="transparency_policy"):
        ImageToPdfPlan(**{**kwargs, "transparency_policy": "white"})
    with pytest.raises(ValueError, match="custom page size"):
        ImageToPdfPlan(**{**kwargs, "page_size_mode": PdfPageSizeMode.CUSTOM})
    with pytest.raises(ValueError, match="custom page size must be omitted"):
        ImageToPdfPlan(**{**kwargs, "custom_page_width_points": 10.0})
    with pytest.raises(ValueError, match="frame mapping"):
        ImageToPdfPlan(**{**kwargs, "frame_mapping": ()})
    with pytest.raises(ValueError, match="total_page_count"):
        ImageToPdfPlan(**{**kwargs, "total_page_count": 2})
    with pytest.raises(ValueError, match="output path must be resolved"):
        ImageToPdfPlan(**{**kwargs, "output_path": Path("relative.pdf")})
    with pytest.raises(ValueError, match="PDF"):
        ImageToPdfPlan(**{**kwargs, "output_path": tmp_path / "out.txt"})
    with pytest.raises(ValueError, match="input image"):
        pdf_source = image_input(tmp_path / "source.pdf")
        ImageToPdfPlan(
            **{
                **kwargs,
                "inputs": (pdf_source,),
                "output_path": pdf_source.path,
                "frame_mapping": build_frame_mapping((pdf_source,)),
            }
        )
    with pytest.raises(ValueError, match="default_dpi"):
        ImageToPdfPlan(**{**kwargs, "default_dpi": 2401.0})


def test_margins_that_consume_page_are_rejected(tmp_path: Path) -> None:
    plan = build_image_to_pdf_plan(
        (image_input(tmp_path / "source.png"),),
        tmp_path / "out.pdf",
        page_size_mode=PdfPageSizeMode.CUSTOM,
        custom_page_width_points=10.0,
        custom_page_height_points=10.0,
        margins=PageMargins(5.0, 5.0, 5.0, 5.0),
    )

    with pytest.raises(ValueError, match="margins"):
        build_page_geometry(pixel_width=10, pixel_height=10, dpi_x=96, dpi_y=96, plan=plan)


def test_margins_and_page_orientation_invariants(tmp_path: Path) -> None:
    source = image_input(tmp_path / "source.png", width=10, height=20)

    with pytest.raises(ValueError, match="left_points"):
        PageMargins(0.0, 0.0, 0.0, -1.0)

    letter_plan = build_image_to_pdf_plan(
        (source,),
        tmp_path / "letter.pdf",
        page_size_mode=PdfPageSizeMode.LETTER,
        orientation=PdfOrientation.LANDSCAPE,
    )
    portrait_plan = build_image_to_pdf_plan(
        (source,),
        tmp_path / "portrait.pdf",
        page_size_mode=PdfPageSizeMode.A4,
        orientation=PdfOrientation.PORTRAIT,
    )

    letter_geometry = build_page_geometry(
        pixel_width=source.pixel_width,
        pixel_height=source.pixel_height,
        dpi_x=96,
        dpi_y=96,
        plan=letter_plan,
    )
    portrait_geometry = build_page_geometry(
        pixel_width=source.pixel_width,
        pixel_height=source.pixel_height,
        dpi_x=96,
        dpi_y=96,
        plan=portrait_plan,
    )

    assert letter_geometry.page_width > letter_geometry.page_height
    assert portrait_geometry.page_width < portrait_geometry.page_height
