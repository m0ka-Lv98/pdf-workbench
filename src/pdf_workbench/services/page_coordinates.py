from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from PySide6.QtCore import QPointF, QRectF, QSizeF
from PySide6.QtGui import QPolygonF


def _require_finite_number(name: str, value: float) -> float:
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise ValueError(f"{name} must be finite")
    return numeric_value


def _require_positive_finite(name: str, value: float) -> float:
    numeric_value = _require_finite_number(name, value)
    if numeric_value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return numeric_value


def _normalize_rotation(rotation: int) -> int:
    if rotation not in {0, 90, 180, 270}:
        raise ValueError("rotation must be one of 0, 90, 180, 270")
    return rotation


@dataclass(frozen=True, slots=True)
class PdfPoint:
    x: float
    y: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _require_finite_number("x", self.x))
        object.__setattr__(self, "y", _require_finite_number("y", self.y))

    def as_tuple(self) -> tuple[float, float]:
        return self.x, self.y

    def to_qpointf(self) -> QPointF:
        return QPointF(self.x, self.y)


@dataclass(frozen=True, slots=True)
class PdfRect:
    left: float
    bottom: float
    right: float
    top: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "left", _require_finite_number("left", self.left))
        object.__setattr__(self, "bottom", _require_finite_number("bottom", self.bottom))
        object.__setattr__(self, "right", _require_finite_number("right", self.right))
        object.__setattr__(self, "top", _require_finite_number("top", self.top))
        if self.right <= self.left:
            raise ValueError("right must be greater than left")
        if self.top <= self.bottom:
            raise ValueError("top must be greater than bottom")

    @classmethod
    def from_tuple(cls, values: tuple[float, float, float, float]) -> PdfRect:
        left, bottom, right, top = values
        return cls(left=left, bottom=bottom, right=right, top=top)

    @classmethod
    def normalized(cls, values: tuple[float, float, float, float]) -> PdfRect:
        left, bottom, right, top = values
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("rect values must be finite")
        return cls(
            left=min(left, right),
            bottom=min(bottom, top),
            right=max(left, right),
            top=max(bottom, top),
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

    def corners(self) -> tuple[PdfPoint, PdfPoint, PdfPoint, PdfPoint]:
        return self.bottom_left, self.bottom_right, self.top_right, self.top_left

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

    def to_qrectf(self) -> QRectF:
        return QRectF(self.left, self.bottom, self.width, self.height)


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
        media_box = PdfRect.normalized(_as_rect_tuple(page.get_mediabox(fallback_ok=True)))
        crop_box = PdfRect.normalized(_as_rect_tuple(page.get_cropbox(fallback_ok=True)))
        visible_box = PdfRect.normalized(_as_rect_tuple(page.get_bbox()))
        return cls(
            media_box=media_box,
            crop_box=crop_box,
            visible_box=visible_box,
            intrinsic_rotation=_normalize_rotation(page.get_rotation()),
        )


@dataclass(frozen=True, slots=True)
class PageMetadata:
    geometry: PageGeometry

    @property
    def width_points(self) -> float:
        return self.geometry.visible_box.width

    @property
    def height_points(self) -> float:
        return self.geometry.visible_box.height

    @classmethod
    def from_size(
        cls,
        width_points: float,
        height_points: float,
        *,
        intrinsic_rotation: int = 0,
    ) -> PageMetadata:
        width_points = _require_positive_finite("width_points", width_points)
        height_points = _require_positive_finite("height_points", height_points)
        box = PdfRect(0.0, 0.0, width_points, height_points)
        geometry = PageGeometry(
            media_box=box,
            crop_box=box,
            visible_box=box,
            intrinsic_rotation=intrinsic_rotation,
        )
        return cls(geometry=geometry)


@dataclass(frozen=True, slots=True)
class PageCoordinateMapper:
    geometry: PageGeometry
    additional_rotation: int
    logical_zoom: float
    device_pixel_ratio: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "additional_rotation",
            _normalize_rotation(self.additional_rotation),
        )
        object.__setattr__(
            self,
            "logical_zoom",
            _require_positive_finite("logical_zoom", self.logical_zoom),
        )
        object.__setattr__(
            self,
            "device_pixel_ratio",
            _require_positive_finite("device_pixel_ratio", self.device_pixel_ratio),
        )

    @property
    def effective_rotation(self) -> int:
        return (self.geometry.intrinsic_rotation + self.additional_rotation) % 360

    @property
    def view_size(self) -> QSizeF:
        width, height = self._unscaled_dimensions()
        return QSizeF(width * self.logical_zoom, height * self.logical_zoom)

    @property
    def device_size(self) -> QSizeF:
        width, height = self._unscaled_dimensions()
        scale = self.logical_zoom * self.device_pixel_ratio
        return QSizeF(width * scale, height * scale)

    def pdf_to_view_point(self, point: PdfPoint) -> QPointF:
        x0, y0 = self._local_coordinates(point)
        rotated_x, rotated_y = self._rotate_local_point(x0, y0)
        return QPointF(rotated_x * self.logical_zoom, rotated_y * self.logical_zoom)

    def view_to_pdf_point(self, point: QPointF) -> PdfPoint:
        scaled_x = point.x() / self.logical_zoom
        scaled_y = point.y() / self.logical_zoom
        x0, y0 = self._unrotate_local_point(scaled_x, scaled_y)
        return self._to_pdf_point(x0, y0)

    def pdf_to_device_point(self, point: PdfPoint) -> QPointF:
        x0, y0 = self._local_coordinates(point)
        rotated_x, rotated_y = self._rotate_local_point(x0, y0)
        scale = self.logical_zoom * self.device_pixel_ratio
        return QPointF(rotated_x * scale, rotated_y * scale)

    def device_to_pdf_point(self, point: QPointF) -> PdfPoint:
        scale = self.logical_zoom * self.device_pixel_ratio
        scaled_x = point.x() / scale
        scaled_y = point.y() / scale
        x0, y0 = self._unrotate_local_point(scaled_x, scaled_y)
        return self._to_pdf_point(x0, y0)

    def pdf_to_view_rect(self, rect: PdfRect) -> QRectF:
        return self.pdf_to_view_polygon(rect.corners()).boundingRect()

    def view_to_pdf_rect(self, rect: QRectF) -> PdfRect:
        return self._rect_from_points(self.view_to_pdf_polygon(self._rect_to_polygon(rect)))

    def pdf_to_device_rect(self, rect: PdfRect) -> QRectF:
        return self.pdf_to_device_polygon(rect.corners()).boundingRect()

    def device_to_pdf_rect(self, rect: QRectF) -> PdfRect:
        return self._rect_from_points(self.device_to_pdf_polygon(self._rect_to_polygon(rect)))

    def pdf_to_view_polygon(self, points: Sequence[PdfPoint]) -> QPolygonF:
        return QPolygonF([self.pdf_to_view_point(point) for point in points])

    def view_to_pdf_polygon(self, polygon: QPolygonF) -> tuple[PdfPoint, ...]:
        return tuple(self.view_to_pdf_point(polygon.at(index)) for index in range(polygon.count()))

    def pdf_to_device_polygon(self, points: Sequence[PdfPoint]) -> QPolygonF:
        return QPolygonF([self.pdf_to_device_point(point) for point in points])

    def device_to_pdf_polygon(self, polygon: QPolygonF) -> tuple[PdfPoint, ...]:
        return tuple(
            self.device_to_pdf_point(polygon.at(index)) for index in range(polygon.count())
        )

    def _unscaled_dimensions(self) -> tuple[float, float]:
        if self.effective_rotation in {0, 180}:
            return self.geometry.visible_box.width, self.geometry.visible_box.height
        return self.geometry.visible_box.height, self.geometry.visible_box.width

    def _local_coordinates(self, point: PdfPoint) -> tuple[float, float]:
        return (
            point.x - self.geometry.visible_box.left,
            point.y - self.geometry.visible_box.bottom,
        )

    def _to_pdf_point(self, x0: float, y0: float) -> PdfPoint:
        return PdfPoint(self.geometry.visible_box.left + x0, self.geometry.visible_box.bottom + y0)

    def _rotate_local_point(self, x0: float, y0: float) -> tuple[float, float]:
        width = self.geometry.visible_box.width
        height = self.geometry.visible_box.height
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
        width = self.geometry.visible_box.width
        height = self.geometry.visible_box.height
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
    def _rect_to_polygon(rect: QRectF) -> QPolygonF:
        return QPolygonF(
            [
                QPointF(rect.left(), rect.top()),
                QPointF(rect.right(), rect.top()),
                QPointF(rect.right(), rect.bottom()),
                QPointF(rect.left(), rect.bottom()),
            ]
        )

    @staticmethod
    def _rect_from_points(points: tuple[PdfPoint, ...]) -> PdfRect:
        xs = [point.x for point in points]
        ys = [point.y for point in points]
        return PdfRect(min(xs), min(ys), max(xs), max(ys))


def _as_rect_tuple(values: Sequence[float]) -> tuple[float, float, float, float]:
    if len(values) != 4:
        raise ValueError("rect must contain four values")
    return cast(tuple[float, float, float, float], tuple(float(value) for value in values))


class _PdfiumPageProtocol(Protocol):
    def get_mediabox(self, fallback_ok: bool = True) -> tuple[float, float, float, float]: ...

    def get_cropbox(self, fallback_ok: bool = True) -> tuple[float, float, float, float]: ...

    def get_bbox(self) -> tuple[float, float, float, float]: ...

    def get_rotation(self) -> int: ...
