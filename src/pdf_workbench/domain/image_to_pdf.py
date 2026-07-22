from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

POINTS_PER_INCH = 72.0
MM_PER_INCH = 25.4
DEFAULT_IMAGE_DPI = 96.0
MIN_IMAGE_DPI = 1.0
MAX_IMAGE_DPI = 2400.0
A4_PORTRAIT_POINTS = (595.2756, 841.8898)
LETTER_PORTRAIT_POINTS = (612.0, 792.0)


class PdfPageSizeMode(StrEnum):
    FIT_IMAGE = "fit_image"
    A4 = "a4"
    LETTER = "letter"
    CUSTOM = "custom"


class PdfOrientation(StrEnum):
    AUTO = "auto"
    PORTRAIT = "portrait"
    LANDSCAPE = "landscape"


class ImageScalingMode(StrEnum):
    FIT = "fit"
    FILL = "fill"
    ACTUAL_SIZE = "actual_size"


class TransparencyPolicy(StrEnum):
    WHITE_BACKGROUND = "white_background"
    BLACK_BACKGROUND = "black_background"
    PRESERVE_ALPHA = "preserve_alpha"


@dataclass(frozen=True, slots=True)
class ImageSourceRevision:
    resolved_path: Path
    size_bytes: int
    modified_time_ns: int
    sha256: str
    detected_format: str
    frame_count: int
    pixel_width: int
    pixel_height: int

    def __post_init__(self) -> None:
        if self.resolved_path != self.resolved_path.expanduser().resolve():
            raise ValueError("image source path must be resolved")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        if self.modified_time_ns < 0:
            raise ValueError("modified_time_ns must be non-negative")
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256):
            raise ValueError("sha256 must be a lowercase SHA-256 hex digest")
        if not self.detected_format:
            raise ValueError("detected_format must not be empty")
        _require_positive_int(self.frame_count, "frame_count")
        _require_positive_int(self.pixel_width, "pixel_width")
        _require_positive_int(self.pixel_height, "pixel_height")


@dataclass(frozen=True, slots=True)
class ImageInput:
    path: Path
    label: str
    detected_format: str
    pixel_width: int
    pixel_height: int
    frame_count: int
    color_mode: str
    has_alpha: bool
    has_icc_profile: bool
    exif_orientation: int | None

    def __post_init__(self) -> None:
        resolved_path = self.path.expanduser().resolve()
        if self.path != resolved_path:
            raise ValueError("image input path must be resolved")
        if not self.label.strip():
            raise ValueError("image input label must not be empty")
        if not self.detected_format:
            raise ValueError("detected_format must not be empty")
        _require_positive_int(self.pixel_width, "pixel_width")
        _require_positive_int(self.pixel_height, "pixel_height")
        _require_positive_int(self.frame_count, "frame_count")
        if not self.color_mode:
            raise ValueError("color_mode must not be empty")
        if self.exif_orientation is not None and self.exif_orientation not in range(1, 9):
            raise ValueError("exif_orientation must be 1-8")


@dataclass(frozen=True, slots=True)
class ImageFrameRef:
    input_path: Path
    input_label: str
    input_index: int
    frame_index: int
    output_page_index: int

    def __post_init__(self) -> None:
        if self.input_path != self.input_path.expanduser().resolve():
            raise ValueError("frame input path must be resolved")
        _require_non_negative_int(self.input_index, "input_index")
        _require_non_negative_int(self.frame_index, "frame_index")
        _require_non_negative_int(self.output_page_index, "output_page_index")


@dataclass(frozen=True, slots=True)
class PageMargins:
    top_points: float
    right_points: float
    bottom_points: float
    left_points: float

    def __post_init__(self) -> None:
        for name, value in (
            ("top_points", self.top_points),
            ("right_points", self.right_points),
            ("bottom_points", self.bottom_points),
            ("left_points", self.left_points),
        ):
            _require_non_negative_finite_number(value, name)

    @property
    def horizontal_points(self) -> float:
        return self.left_points + self.right_points

    @property
    def vertical_points(self) -> float:
        return self.top_points + self.bottom_points


@dataclass(frozen=True, slots=True)
class PageGeometry:
    page_width: float
    page_height: float
    image_x: float
    image_y: float
    image_width: float
    image_height: float
    source_crop_box: tuple[float, float, float, float] | None

    def __post_init__(self) -> None:
        _require_positive_finite_number(self.page_width, "page_width")
        _require_positive_finite_number(self.page_height, "page_height")
        _require_finite_number(self.image_x, "image_x")
        _require_finite_number(self.image_y, "image_y")
        _require_positive_finite_number(self.image_width, "image_width")
        _require_positive_finite_number(self.image_height, "image_height")
        if self.source_crop_box is not None:
            if len(self.source_crop_box) != 4:
                raise ValueError("source_crop_box must contain four values")
            for index, value in enumerate(self.source_crop_box):
                _require_finite_number(value, f"source_crop_box[{index}]")


@dataclass(frozen=True, slots=True)
class ImageToPdfPlan:
    inputs: tuple[ImageInput, ...]
    output_path: Path
    page_size_mode: PdfPageSizeMode
    orientation: PdfOrientation
    scaling_mode: ImageScalingMode
    transparency_policy: TransparencyPolicy
    margins: PageMargins
    custom_page_width_points: float | None
    custom_page_height_points: float | None
    frame_mapping: tuple[ImageFrameRef, ...]
    total_page_count: int
    default_dpi: float = DEFAULT_IMAGE_DPI

    def __post_init__(self) -> None:
        resolved_output = self.output_path.expanduser().resolve()
        if self.output_path != resolved_output:
            raise ValueError("output path must be resolved")
        if self.output_path.suffix.lower() != ".pdf":
            raise ValueError("output path must be a PDF file")
        if not self.inputs:
            raise ValueError("at least one image input is required")
        if len({item.path for item in self.inputs}) != len(self.inputs):
            raise ValueError("image inputs must be unique")
        if self.output_path in {item.path for item in self.inputs}:
            raise ValueError("output path must not match an input image")
        if not isinstance(self.page_size_mode, PdfPageSizeMode):
            raise ValueError("page_size_mode must be PdfPageSizeMode")
        if not isinstance(self.orientation, PdfOrientation):
            raise ValueError("orientation must be PdfOrientation")
        if not isinstance(self.scaling_mode, ImageScalingMode):
            raise ValueError("scaling_mode must be ImageScalingMode")
        if not isinstance(self.transparency_policy, TransparencyPolicy):
            raise ValueError("transparency_policy must be TransparencyPolicy")
        _require_valid_dpi(self.default_dpi, "default_dpi")
        if self.page_size_mode is PdfPageSizeMode.CUSTOM:
            if self.custom_page_width_points is None or self.custom_page_height_points is None:
                raise ValueError("custom page size is required")
            _require_positive_finite_number(self.custom_page_width_points, "custom_page_width")
            _require_positive_finite_number(self.custom_page_height_points, "custom_page_height")
        elif (
            self.custom_page_width_points is not None or self.custom_page_height_points is not None
        ):
            raise ValueError("custom page size must be omitted unless CUSTOM is selected")
        expected_mapping = build_frame_mapping(self.inputs)
        if self.frame_mapping != expected_mapping:
            raise ValueError("frame mapping must match inputs and frame counts")
        if self.total_page_count != len(expected_mapping):
            raise ValueError("total_page_count must equal frame mapping length")


def mm_to_points(value: float) -> float:
    _require_non_negative_finite_number(value, "millimeters")
    return value / MM_PER_INCH * POINTS_PER_INCH


def margins_from_mm(top: float, right: float, bottom: float, left: float) -> PageMargins:
    return PageMargins(
        top_points=mm_to_points(top),
        right_points=mm_to_points(right),
        bottom_points=mm_to_points(bottom),
        left_points=mm_to_points(left),
    )


def build_frame_mapping(inputs: tuple[ImageInput, ...]) -> tuple[ImageFrameRef, ...]:
    mapping: list[ImageFrameRef] = []
    output_page_index = 0
    for input_index, image_input in enumerate(inputs):
        for frame_index in range(image_input.frame_count):
            mapping.append(
                ImageFrameRef(
                    input_path=image_input.path,
                    input_label=image_input.label,
                    input_index=input_index,
                    frame_index=frame_index,
                    output_page_index=output_page_index,
                )
            )
            output_page_index += 1
    return tuple(mapping)


def build_image_to_pdf_plan(
    inputs: tuple[ImageInput, ...],
    output_path: Path,
    *,
    page_size_mode: PdfPageSizeMode = PdfPageSizeMode.FIT_IMAGE,
    orientation: PdfOrientation = PdfOrientation.AUTO,
    scaling_mode: ImageScalingMode = ImageScalingMode.FIT,
    transparency_policy: TransparencyPolicy = TransparencyPolicy.WHITE_BACKGROUND,
    margins: PageMargins | None = None,
    custom_page_width_points: float | None = None,
    custom_page_height_points: float | None = None,
    default_dpi: float = DEFAULT_IMAGE_DPI,
) -> ImageToPdfPlan:
    resolved_output = output_path.expanduser().resolve()
    frame_mapping = build_frame_mapping(inputs)
    return ImageToPdfPlan(
        inputs=inputs,
        output_path=resolved_output,
        page_size_mode=page_size_mode,
        orientation=orientation,
        scaling_mode=scaling_mode,
        transparency_policy=transparency_policy,
        margins=margins if margins is not None else margins_from_mm(10.0, 10.0, 10.0, 10.0),
        custom_page_width_points=custom_page_width_points,
        custom_page_height_points=custom_page_height_points,
        frame_mapping=frame_mapping,
        total_page_count=len(frame_mapping),
        default_dpi=default_dpi,
    )


def build_page_geometry(
    *,
    pixel_width: int,
    pixel_height: int,
    dpi_x: float | None,
    dpi_y: float | None,
    plan: ImageToPdfPlan,
) -> PageGeometry:
    _require_positive_int(pixel_width, "pixel_width")
    _require_positive_int(pixel_height, "pixel_height")
    effective_dpi_x = normalize_dpi(dpi_x, plan.default_dpi)
    effective_dpi_y = normalize_dpi(dpi_y, plan.default_dpi)
    image_physical_width = pixel_width / effective_dpi_x * POINTS_PER_INCH
    image_physical_height = pixel_height / effective_dpi_y * POINTS_PER_INCH
    page_width, page_height = _page_size_for_image(
        plan,
        pixel_width=pixel_width,
        pixel_height=pixel_height,
        image_physical_width=image_physical_width,
        image_physical_height=image_physical_height,
    )
    usable_width = page_width - plan.margins.horizontal_points
    usable_height = page_height - plan.margins.vertical_points
    if usable_width <= 0 or usable_height <= 0:
        raise ValueError("margins leave no usable page area")
    if plan.scaling_mode is ImageScalingMode.ACTUAL_SIZE:
        image_width = image_physical_width
        image_height = image_physical_height
        if image_width > usable_width or image_height > usable_height:
            raise ValueError("actual-size image does not fit inside margins")
        source_crop_box = None
    else:
        scale = (
            min(usable_width / pixel_width, usable_height / pixel_height)
            if plan.scaling_mode is ImageScalingMode.FIT
            else max(usable_width / pixel_width, usable_height / pixel_height)
        )
        image_width = pixel_width * scale
        image_height = pixel_height * scale
        if plan.scaling_mode is ImageScalingMode.FILL:
            crop_width_px = usable_width / scale
            crop_height_px = usable_height / scale
            crop_x = (pixel_width - crop_width_px) / 2.0
            crop_y = (pixel_height - crop_height_px) / 2.0
            source_crop_box = (
                max(0.0, crop_x),
                max(0.0, crop_y),
                min(float(pixel_width), crop_x + crop_width_px),
                min(float(pixel_height), crop_y + crop_height_px),
            )
            image_width = usable_width
            image_height = usable_height
        else:
            source_crop_box = None
    image_x = plan.margins.left_points + (usable_width - image_width) / 2.0
    image_y = plan.margins.bottom_points + (usable_height - image_height) / 2.0
    return PageGeometry(
        page_width=page_width,
        page_height=page_height,
        image_x=image_x,
        image_y=image_y,
        image_width=image_width,
        image_height=image_height,
        source_crop_box=source_crop_box,
    )


def normalize_dpi(value: float | None, default_dpi: float = DEFAULT_IMAGE_DPI) -> float:
    _require_valid_dpi(default_dpi, "default_dpi")
    if value is None:
        return default_dpi
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default_dpi
    if not math.isfinite(float(value)):
        return default_dpi
    if float(value) < MIN_IMAGE_DPI or float(value) > MAX_IMAGE_DPI:
        return default_dpi
    return float(value)


def _page_size_for_image(
    plan: ImageToPdfPlan,
    *,
    pixel_width: int,
    pixel_height: int,
    image_physical_width: float,
    image_physical_height: float,
) -> tuple[float, float]:
    if plan.page_size_mode is PdfPageSizeMode.FIT_IMAGE:
        width = image_physical_width + plan.margins.horizontal_points
        height = image_physical_height + plan.margins.vertical_points
        return _apply_orientation(width, height, plan.orientation, pixel_width, pixel_height)
    if plan.page_size_mode is PdfPageSizeMode.A4:
        width, height = A4_PORTRAIT_POINTS
    elif plan.page_size_mode is PdfPageSizeMode.LETTER:
        width, height = LETTER_PORTRAIT_POINTS
    else:
        assert plan.custom_page_width_points is not None
        assert plan.custom_page_height_points is not None
        width, height = plan.custom_page_width_points, plan.custom_page_height_points
    return _apply_orientation(width, height, plan.orientation, pixel_width, pixel_height)


def _apply_orientation(
    width: float,
    height: float,
    orientation: PdfOrientation,
    image_pixel_width: int,
    image_pixel_height: int,
) -> tuple[float, float]:
    if orientation is PdfOrientation.AUTO:
        if width == height:
            return (min(width, height), max(width, height))
        if image_pixel_width > image_pixel_height:
            return (max(width, height), min(width, height))
        return (min(width, height), max(width, height))
    if orientation is PdfOrientation.LANDSCAPE:
        return (max(width, height), min(width, height))
    return (min(width, height), max(width, height))


def _require_finite_number(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")


def _require_positive_finite_number(value: float, name: str) -> None:
    _require_finite_number(value, name)
    if float(value) <= 0:
        raise ValueError(f"{name} must be positive")


def _require_non_negative_finite_number(value: float, name: str) -> None:
    _require_finite_number(value, name)
    if float(value) < 0:
        raise ValueError(f"{name} must be non-negative")


def _require_positive_int(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_non_negative_int(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_valid_dpi(value: float, name: str) -> None:
    _require_positive_finite_number(value, name)
    if float(value) < MIN_IMAGE_DPI or float(value) > MAX_IMAGE_DPI:
        raise ValueError(f"{name} must be in the supported DPI range")
