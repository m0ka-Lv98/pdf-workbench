from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Final

from pdf_workbench.services.page_coordinates import PdfRect

_MINIMUM_EXTENT: Final[float] = 1.0
_EPSILON: Final[float] = 1e-6
_ROUND_DIGITS: Final[int] = 6


def _require_non_negative_finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number")
    numeric_value = float(value)
    if not math.isfinite(numeric_value):
        raise ValueError(f"{name} must be finite")
    if numeric_value < 0:
        raise ValueError(f"{name} must be non-negative")
    return numeric_value


def _require_page_index(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError(f"{label} must be an integer")
    page_index = int(value)
    if page_index < 0:
        raise ValueError(f"{label} must be non-negative")
    return page_index


def _canonical_box(
    values: tuple[float, float, float, float],
    *,
    label: str,
) -> tuple[float, float, float, float]:
    try:
        rect = PdfRect.normalized(values)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    return _stable_box(rect.as_tuple())


def _stable_box(values: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    rounded = tuple(round(float(value), _ROUND_DIGITS) for value in values)
    return rounded  # type: ignore[return-value]


def _rotation_dimensions(
    box: tuple[float, float, float, float],
    rotation: int,
) -> tuple[float, float]:
    rect = PdfRect.from_tuple(box)
    if rotation in {0, 180}:
        return rect.width, rect.height
    if rotation in {90, 270}:
        return rect.height, rect.width
    raise ValueError("rotation must be one of 0, 90, 180, 270")


@dataclass(frozen=True, slots=True)
class PageCropMargins:
    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "left", _require_non_negative_finite("left", self.left))
        object.__setattr__(self, "top", _require_non_negative_finite("top", self.top))
        object.__setattr__(self, "right", _require_non_negative_finite("right", self.right))
        object.__setattr__(self, "bottom", _require_non_negative_finite("bottom", self.bottom))

    @property
    def is_zero(self) -> bool:
        return (
            self.left <= _EPSILON
            and self.top <= _EPSILON
            and self.right <= _EPSILON
            and self.bottom <= _EPSILON
        )


@dataclass(frozen=True, slots=True)
class PageCropState:
    page_index: int
    direct_crop_box_present: bool
    direct_crop_box_value: tuple[float, float, float, float] | None
    effective_crop_box: tuple[float, float, float, float]
    effective_media_box: tuple[float, float, float, float]
    effective_rotation: int
    crop_box_inherited: bool = False
    crop_box_falls_back_to_media_box: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "page_index",
            _require_page_index(self.page_index, label="page_index"),
        )
        object.__setattr__(
            self,
            "effective_crop_box",
            _canonical_box(self.effective_crop_box, label="effective_crop_box"),
        )
        object.__setattr__(
            self,
            "effective_media_box",
            _canonical_box(self.effective_media_box, label="effective_media_box"),
        )
        if self.effective_rotation not in {0, 90, 180, 270}:
            raise ValueError("effective_rotation must be one of 0, 90, 180, 270")
        if self.crop_box_inherited and self.direct_crop_box_present:
            raise ValueError(
                "crop_box_inherited cannot be true when direct_crop_box_present is true"
            )
        if self.crop_box_falls_back_to_media_box and (
            self.direct_crop_box_present or self.crop_box_inherited
        ):
            raise ValueError("crop_box_falls_back_to_media_box is inconsistent with crop source")
        if self.direct_crop_box_present:
            if self.direct_crop_box_value is None:
                raise ValueError(
                    "direct_crop_box_value is required when direct_crop_box_present is true"
                )
            object.__setattr__(
                self,
                "direct_crop_box_value",
                tuple(float(value) for value in self.direct_crop_box_value),
            )
        media = PdfRect.from_tuple(self.effective_media_box)
        crop = PdfRect.from_tuple(self.effective_crop_box)
        if crop.left < media.left - _EPSILON:
            raise ValueError("effective_crop_box must stay within effective_media_box")
        if crop.bottom < media.bottom - _EPSILON:
            raise ValueError("effective_crop_box must stay within effective_media_box")
        if crop.right > media.right + _EPSILON:
            raise ValueError("effective_crop_box must stay within effective_media_box")
        if crop.top > media.top + _EPSILON:
            raise ValueError("effective_crop_box must stay within effective_media_box")

    @property
    def displayed_size(self) -> tuple[float, float]:
        return _rotation_dimensions(self.effective_crop_box, self.effective_rotation)


@dataclass(frozen=True, slots=True)
class PageCropTarget:
    page_index: int
    crop_box: tuple[float, float, float, float]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "page_index",
            _require_page_index(self.page_index, label="page_index"),
        )
        object.__setattr__(self, "crop_box", _canonical_box(self.crop_box, label="crop_box"))


@dataclass(frozen=True, slots=True)
class PageCropPlan:
    page_indexes: tuple[int, ...]
    targets: tuple[PageCropTarget, ...]
    reset_to_media_box: bool

    def __post_init__(self) -> None:
        normalized_indexes = tuple(
            _require_page_index(value, label="page_indexes") for value in self.page_indexes
        )
        if not normalized_indexes:
            raise ValueError("page_indexes must not be empty")
        if tuple(sorted(normalized_indexes)) != normalized_indexes:
            raise ValueError("page_indexes must be sorted")
        if len(set(normalized_indexes)) != len(normalized_indexes):
            raise ValueError("page_indexes must be unique")
        if len(self.targets) != len(normalized_indexes):
            raise ValueError("targets length must match page_indexes")
        for page_index, target in zip(normalized_indexes, self.targets, strict=True):
            if target.page_index != page_index:
                raise ValueError("targets must match page_indexes")
        object.__setattr__(self, "page_indexes", normalized_indexes)


def crop_box_from_display_margins(
    state: PageCropState,
    margins: PageCropMargins,
) -> tuple[float, float, float, float]:
    left, bottom, right, top = state.effective_crop_box
    if state.effective_rotation == 0:
        target = (
            left + margins.left,
            bottom + margins.bottom,
            right - margins.right,
            top - margins.top,
        )
    elif state.effective_rotation == 90:
        target = (
            left + margins.top,
            bottom + margins.left,
            right - margins.bottom,
            top - margins.right,
        )
    elif state.effective_rotation == 180:
        target = (
            left + margins.right,
            bottom + margins.top,
            right - margins.left,
            top - margins.bottom,
        )
    elif state.effective_rotation == 270:
        target = (
            left + margins.bottom,
            bottom + margins.right,
            right - margins.top,
            top - margins.left,
        )
    else:
        raise ValueError("unsupported rotation")
    return _stable_box(target)


def build_page_crop_plan(
    states: tuple[PageCropState, ...],
    *,
    margins: PageCropMargins | None = None,
    reset_to_media_box: bool = False,
) -> PageCropPlan:
    if not states:
        raise ValueError("selected pages must not be empty")
    if reset_to_media_box and margins is not None and not margins.is_zero:
        raise ValueError("margins must be zero when reset_to_media_box is enabled")
    if not reset_to_media_box and margins is None:
        raise ValueError("margins are required when reset_to_media_box is false")
    normalized_states = tuple(sorted(states, key=lambda state: state.page_index))
    if len({state.page_index for state in normalized_states}) != len(normalized_states):
        raise ValueError("selected pages must be unique")
    if tuple(state.page_index for state in normalized_states) != tuple(
        sorted(state.page_index for state in normalized_states)
    ):
        raise ValueError("selected pages must be sorted")

    invalid_page_numbers: list[int] = []
    targets: list[PageCropTarget] = []
    for state in normalized_states:
        try:
            target_box = (
                state.effective_media_box
                if reset_to_media_box
                else crop_box_from_display_margins(
                    state,
                    margins if margins is not None else PageCropMargins(0, 0, 0, 0),
                )
            )
            _validate_target_for_state(state, target_box, margins=margins)
            targets.append(PageCropTarget(page_index=state.page_index, crop_box=target_box))
        except ValueError:
            invalid_page_numbers.append(state.page_index + 1)
    if invalid_page_numbers:
        pages = ", ".join(str(page_number) for page_number in invalid_page_numbers)
        raise ValueError(f"次のページではトリミングを適用できません: {pages}")
    if all(
        target.crop_box == state.effective_crop_box
        for target, state in zip(targets, normalized_states, strict=True)
    ):
        raise ValueError("トリミング後の表示範囲が変化しません")
    return PageCropPlan(
        page_indexes=tuple(state.page_index for state in normalized_states),
        targets=tuple(targets),
        reset_to_media_box=reset_to_media_box,
    )


def _validate_target_for_state(
    state: PageCropState,
    target_box: tuple[float, float, float, float],
    *,
    margins: PageCropMargins | None,
) -> None:
    target_rect = PdfRect.from_tuple(target_box)
    media_rect = PdfRect.from_tuple(state.effective_media_box)
    if target_rect.left < media_rect.left - _EPSILON:
        raise ValueError("crop box must stay within media box")
    if target_rect.bottom < media_rect.bottom - _EPSILON:
        raise ValueError("crop box must stay within media box")
    if target_rect.right > media_rect.right + _EPSILON:
        raise ValueError("crop box must stay within media box")
    if target_rect.top > media_rect.top + _EPSILON:
        raise ValueError("crop box must stay within media box")
    if target_rect.width < _MINIMUM_EXTENT - _EPSILON:
        raise ValueError("cropped width must be at least 1 point")
    if target_rect.height < _MINIMUM_EXTENT - _EPSILON:
        raise ValueError("cropped height must be at least 1 point")
    if margins is None:
        return
    displayed_width, displayed_height = state.displayed_size
    if margins.left + margins.right >= displayed_width - _EPSILON:
        raise ValueError("displayed width would collapse")
    if margins.top + margins.bottom >= displayed_height - _EPSILON:
        raise ValueError("displayed height would collapse")
