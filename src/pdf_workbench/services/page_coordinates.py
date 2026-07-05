from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


def _require_finite(name: str, value: float) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _normalize_rotation(rotation: int) -> int:
    if rotation % 90 != 0:
        raise ValueError("rotation must be a multiple of 90 degrees")
    normalized = rotation % 360
    if normalized not in {0, 90, 180, 270}:
        raise ValueError("rotation must be one of 0, 90, 180, 270")
    return normalized


@dataclass(frozen=True, slots=True)
class PdfPoint:
    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _require_finite("x", self.x))
        object.__setattr__(self, "y", _require_finite("y", self.y))

    def as_tuple(self) -> tuple[float, float]:
        return self.x, self.y


@dataclass(frozen=True, slots=True)
class PdfRect:
    left: float
    bottom: float
    right: float
    top: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "left", _require_finite("left", self.left))
        object.__setattr__(self, "bottom", _require_finite("bottom", self.bottom))
        object.__setattr__(self, "right", _require_finite("right", self.right))
        object.__setattr__(self, "top", _require_finite("top", self.top))
        if self.right <= self.left:
            raise ValueError("right must be greater than left")
        if self.top <= self.bottom:
            raise ValueError("top must be greater than bottom")

    @classmethod
    def from_tuple(cls, values: Iterable[float]) -> PdfRect:
        left, bottom, right, top = tuple(values)
        return cls(left=left, bottom=bottom, right=right, top=top)

    @classmethod
    def normalized(cls, values: Iterable[float]) -> PdfRect:
        left, bottom, right, top = tuple(values)
        if not all(math.isfinite(value) for value in (left, bottom, right, top)):
            raise ValueError("rect values must be finite")
        normalized_left = min(left, right)
        normalized_right = max(left, right)
        normalized_bottom = min(bottom, top)
        normalized_top = max(bottom, top)
        return cls(
            left=normalized_left,
            bottom=normalized_bottom,
            right=normalized_right,
            top=normalized_top,
        )

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def center(self) -> PdfPoint:
        return PdfPoint(self.left + self.width / 2, self.bottom + self.height / 2)

    @property
    def bottom_left(self) -> PdfPoint:
        return PdfPoint(self.left, self.bottom)

    @property
    def bottom_right(self) -> PdfPoint:
        return PdfPoint(self.right, self.bottom)

    @property
    def top_left(self) -> PdfPoint:
        return PdfPoint(self.left, self.top)

    @property
    def top_right(self) -> PdfPoint:
        return PdfPoint(self.right, self.top)

    def intersection(self, other: PdfRect) -> PdfRect | None:
        left = max(self.left, other.left)
        bottom = max(self.bottom, other.bottom)
        right = min(self.right, other.right)
        top = min(self.top, other.top)
        if right <= left or top <= bottom:
            return None
        return PdfRect(left=left, bottom=bottom, right=right, top=top)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.left, self.bottom, self.right, self.top

    def corners(self) -> tuple[PdfPoint, PdfPoint, PdfPoint, PdfPoint]:
        return (self.bottom_left, self.bottom_right, self.top_right, self.top_left)


@dataclass(frozen=True, slots=True)
class PageGeometry:
    media_box: PdfRect
    crop_box: PdfRect
    visible_box: PdfRect
    intrinsic_rotation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "intrinsic_rotation", _normalize_rotation(self.intrinsic_rotation))

    @classmethod
    def from_pdfium_page(cls, page: _PdfiumPageProtocol) -> PageGeometry:
        mediabox = PdfRect.normalized(page.get_mediabox())
        cropbox = PdfRect.normalized(page.get_cropbox())
        visible_box = cropbox.intersection(mediabox)
        if visible_box is None:
            raise ValueError("crop box must intersect media box")
        return cls(
            media_box=mediabox,
            crop_box=cropbox,
            visible_box=visible_box,
            intrinsic_rotation=_normalize_rotation(page.get_rotation()),
        )


@dataclass(frozen=True, slots=True)
class PageCoordinateMapper:
    geometry: PageGeometry
    additional_rotation: int
    logical_zoom: float
    device_pixel_ratio: float

    def __post_init__(self) -> None:
        if self.logical_zoom <= 0:
            raise ValueError("logical_zoom must be positive")
        if self.device_pixel_ratio <= 0:
            raise ValueError("device_pixel_ratio must be positive")
        normalized_rotation = _normalize_rotation(self.additional_rotation)
        object.__setattr__(self, "additional_rotation", normalized_rotation)

    @property
    def effective_rotation(self) -> int:
        return (self.geometry.intrinsic_rotation + self.additional_rotation) % 360

    @property
    def view_scale(self) -> float:
        return self.logical_zoom

    @property
    def device_scale(self) -> float:
        return self.logical_zoom * self.device_pixel_ratio

    @property
    def _local_width(self) -> float:
        return self.geometry.visible_box.width

    @property
    def _local_height(self) -> float:
        return self.geometry.visible_box.height

    def pdf_to_view_point(self, point: PdfPoint) -> PdfPoint:
        x0, y0 = self._local_coordinates(point)
        view_x, view_y = self._rotate_local_point(x0, y0)
        return PdfPoint(view_x * self.view_scale, view_y * self.view_scale)

    def view_to_pdf_point(self, point: PdfPoint) -> PdfPoint:
        x = point.x / self.view_scale
        y = point.y / self.view_scale
        x0, y0 = self._unrotate_local_point(x, y)
        return self._to_pdf_point(x0, y0)

    def pdf_to_device_point(self, point: PdfPoint) -> PdfPoint:
        x0, y0 = self._local_coordinates(point)
        view_x, view_y = self._rotate_local_point(x0, y0)
        return PdfPoint(view_x * self.device_scale, view_y * self.device_scale)

    def device_to_pdf_point(self, point: PdfPoint) -> PdfPoint:
        x = point.x / self.device_scale
        y = point.y / self.device_scale
        x0, y0 = self._unrotate_local_point(x, y)
        return self._to_pdf_point(x0, y0)

    def pdf_to_view_rect(self, rect: PdfRect) -> PdfRect:
        return self._rect_from_points(self.pdf_to_view_point(point) for point in rect.corners())

    def view_to_pdf_rect(self, rect: PdfRect) -> PdfRect:
        return self._rect_from_points(self.view_to_pdf_point(point) for point in rect.corners())

    def pdf_to_device_rect(self, rect: PdfRect) -> PdfRect:
        return self._rect_from_points(self.pdf_to_device_point(point) for point in rect.corners())

    def device_to_pdf_rect(self, rect: PdfRect) -> PdfRect:
        return self._rect_from_points(self.device_to_pdf_point(point) for point in rect.corners())

    def pdf_to_view_polygon(self, points: Iterable[PdfPoint]) -> tuple[PdfPoint, ...]:
        return tuple(self.pdf_to_view_point(point) for point in points)

    def view_to_pdf_polygon(self, points: Iterable[PdfPoint]) -> tuple[PdfPoint, ...]:
        return tuple(self.view_to_pdf_point(point) for point in points)

    def pdf_to_device_polygon(self, points: Iterable[PdfPoint]) -> tuple[PdfPoint, ...]:
        return tuple(self.pdf_to_device_point(point) for point in points)

    def device_to_pdf_polygon(self, points: Iterable[PdfPoint]) -> tuple[PdfPoint, ...]:
        return tuple(self.device_to_pdf_point(point) for point in points)

    def _local_coordinates(self, point: PdfPoint) -> tuple[float, float]:
        return point.x - self.geometry.visible_box.left, point.y - self.geometry.visible_box.bottom

    def _to_pdf_point(self, x0: float, y0: float) -> PdfPoint:
        return PdfPoint(
            self.geometry.visible_box.left + x0,
            self.geometry.visible_box.bottom + y0,
        )

    def _rotate_local_point(self, x0: float, y0: float) -> tuple[float, float]:
        width = self._local_width
        height = self._local_height
        match self.effective_rotation:
            case 0:
                return x0, height - y0
            case 90:
                return y0, x0
            case 180:
                return width - x0, y0
            case 270:
                return height - y0, width - x0
            case _:
                raise AssertionError("rotation must be normalized")

    def _unrotate_local_point(self, view_x: float, view_y: float) -> tuple[float, float]:
        width = self._local_width
        height = self._local_height
        match self.effective_rotation:
            case 0:
                return view_x, height - view_y
            case 90:
                return view_y, view_x
            case 180:
                return width - view_x, view_y
            case 270:
                return width - view_y, height - view_x
            case _:
                raise AssertionError("rotation must be normalized")

    @staticmethod
    def _rect_from_points(points: Iterable[PdfPoint]) -> PdfRect:
        point_list = tuple(points)
        xs = [point.x for point in point_list]
        ys = [point.y for point in point_list]
        return PdfRect(
            left=min(xs),
            bottom=min(ys),
            right=max(xs),
            top=max(ys),
        )


class _PdfiumPageProtocol(Protocol):
    def get_mediabox(self) -> Iterable[float]: ...

    def get_cropbox(self) -> Iterable[float]: ...

    def get_rotation(self) -> int: ...
