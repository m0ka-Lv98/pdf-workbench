from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from pdf_regression_utils import VisualComparisonTolerance, assert_images_visually_close


def make_image(
    *,
    offset_x: int = 0,
    color: tuple[int, int, int, int] = (0, 0, 0, 255),
) -> Image.Image:
    image = Image.new("RGBA", (64, 64), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((16 + offset_x, 16, 36 + offset_x, 36), fill=color)
    return image


def test_visual_comparison_accepts_identical_images() -> None:
    first = make_image()
    second = make_image()
    try:
        assert_images_visually_close(first, second)
    finally:
        first.close()
        second.close()


def test_visual_comparison_accepts_small_noise_within_tolerance() -> None:
    first = make_image()
    second = make_image()
    second.putpixel((10, 10), (250, 250, 250, 255))
    try:
        assert_images_visually_close(first, second)
    finally:
        first.close()
        second.close()


def test_visual_comparison_rejects_dimension_mismatch() -> None:
    first = Image.new("RGBA", (64, 64), (255, 255, 255, 255))
    second = Image.new("RGBA", (32, 64), (255, 255, 255, 255))
    try:
        with pytest.raises(AssertionError, match="dimensions differ"):
            assert_images_visually_close(first, second)
    finally:
        first.close()
        second.close()


def test_visual_comparison_rejects_large_color_shift() -> None:
    first = make_image(color=(0, 0, 0, 255))
    second = make_image(color=(255, 0, 0, 255))
    try:
        with pytest.raises(AssertionError, match="mean_channel_error="):
            assert_images_visually_close(first, second)
    finally:
        first.close()
        second.close()


def test_visual_comparison_rejects_shape_movement() -> None:
    first = make_image()
    second = make_image(offset_x=12)
    try:
        with pytest.raises(AssertionError, match="significant_pixel_fraction="):
            assert_images_visually_close(first, second)
    finally:
        first.close()
        second.close()


def test_visual_comparison_boundary_values_are_explicit() -> None:
    tolerance = VisualComparisonTolerance(
        max_mean_channel_error=1.0,
        significant_channel_delta=16,
        max_significant_pixel_fraction=0.0,
    )
    first = make_image()
    second = make_image()
    second.putpixel((10, 10), (230, 230, 230, 255))
    try:
        with pytest.raises(AssertionError, match="mean_channel_error="):
            assert_images_visually_close(first, second, tolerance=tolerance)
    finally:
        first.close()
        second.close()
