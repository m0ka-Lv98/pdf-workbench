from __future__ import annotations

import math

import pytest

from pdf_workbench.domain.page_crop import (
    PageCropMargins,
    PageCropPlan,
    PageCropState,
    PageCropTarget,
    _require_raw_box,
    _rotation_dimensions,
    _validate_target_for_state,
    build_page_crop_plan,
    crop_box_from_display_margins,
)


def make_state(
    *,
    page_index: int = 0,
    crop_box: tuple[float, float, float, float] = (30.0, 40.0, 590.0, 800.0),
    media_box: tuple[float, float, float, float] = (10.0, 20.0, 610.0, 820.0),
    rotation: int = 0,
    direct_crop_box_present: bool = False,
    direct_crop_box_value: tuple[float, float, float, float] | None = None,
) -> PageCropState:
    return PageCropState(
        page_index=page_index,
        direct_crop_box_present=direct_crop_box_present,
        direct_crop_box_value=direct_crop_box_value,
        effective_crop_box=crop_box,
        effective_media_box=media_box,
        effective_rotation=rotation,
    )


def test_page_crop_margins_reject_bool_negative_and_non_finite_values() -> None:
    with pytest.raises(TypeError):
        PageCropMargins(True, 0, 0, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PageCropMargins(-1, 0, 0, 0)
    with pytest.raises(ValueError):
        PageCropMargins(math.nan, 0, 0, 0)
    with pytest.raises(ValueError):
        PageCropMargins(math.inf, 0, 0, 0)


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, (40.0, 80.0, 560.0, 780.0)),
        (90, (50.0, 50.0, 550.0, 770.0)),
        (180, (60.0, 60.0, 580.0, 760.0)),
        (270, (70.0, 70.0, 570.0, 790.0)),
    ],
)
def test_crop_box_from_display_margins_maps_display_edges_for_each_rotation(
    rotation: int,
    expected: tuple[float, float, float, float],
) -> None:
    state = make_state(rotation=rotation)
    margins = PageCropMargins(left=10, top=20, right=30, bottom=40)

    target = crop_box_from_display_margins(state, margins)

    assert target == expected


def test_crop_box_mapping_supports_non_zero_origin() -> None:
    state = make_state(
        crop_box=(30.0, 40.0, 590.0, 800.0),
        media_box=(10.0, 20.0, 610.0, 820.0),
        rotation=0,
    )

    assert crop_box_from_display_margins(
        state,
        PageCropMargins(left=5.0, top=7.0, right=11.0, bottom=13.0),
    ) == (35.0, 53.0, 579.0, 793.0)


def test_build_page_crop_plan_rejects_zero_margin_no_op() -> None:
    state = make_state()

    with pytest.raises(ValueError, match="変化しません"):
        build_page_crop_plan((state,), margins=PageCropMargins(0, 0, 0, 0))


def test_build_page_crop_plan_rejects_display_overflow_and_reports_page_numbers() -> None:
    first = make_state(
        page_index=0,
        crop_box=(0.0, 0.0, 100.0, 200.0),
        media_box=(0.0, 0.0, 100.0, 200.0),
        rotation=0,
    )
    second = make_state(
        page_index=1,
        crop_box=(0.0, 0.0, 50.0, 60.0),
        media_box=(0.0, 0.0, 50.0, 60.0),
        rotation=0,
    )

    with pytest.raises(ValueError, match="2"):
        build_page_crop_plan(
            (first, second),
            margins=PageCropMargins(left=10.0, top=10.0, right=10.0, bottom=60.0),
        )


def test_build_page_crop_plan_reset_to_media_box() -> None:
    state = make_state(
        direct_crop_box_present=True,
        direct_crop_box_value=(40.0, 60.0, 560.0, 760.0),
    )

    plan = build_page_crop_plan(
        (state,),
        margins=PageCropMargins(0, 0, 0, 0),
        reset_to_media_box=True,
    )

    assert plan == PageCropPlan(
        page_indexes=(0,),
        targets=(PageCropTarget(page_index=0, crop_box=(10.0, 20.0, 610.0, 820.0)),),
        reset_to_media_box=True,
    )


def test_build_page_crop_plan_normalizes_state_order_and_rejects_duplicate_indexes() -> None:
    first = make_state(page_index=2)
    second = make_state(page_index=0)

    plan = build_page_crop_plan(
        (first, second),
        margins=PageCropMargins(5.0, 5.0, 5.0, 5.0),
    )

    assert plan.page_indexes == (0, 2)
    assert tuple(target.page_index for target in plan.targets) == (0, 2)


def test_page_crop_state_rejects_invalid_direct_crop_source_flags() -> None:
    with pytest.raises(TypeError, match="page_index must be an integer"):
        PageCropState(  # type: ignore[arg-type]
            page_index="0",
            direct_crop_box_present=False,
            direct_crop_box_value=None,
            effective_crop_box=(30.0, 40.0, 590.0, 800.0),
            effective_media_box=(10.0, 20.0, 610.0, 820.0),
            effective_rotation=0,
        )
    with pytest.raises(ValueError, match="crop_box_inherited"):
        PageCropState(
            page_index=0,
            direct_crop_box_present=True,
            direct_crop_box_value=(30.0, 40.0, 590.0, 800.0),
            effective_crop_box=(30.0, 40.0, 590.0, 800.0),
            effective_media_box=(10.0, 20.0, 610.0, 820.0),
            effective_rotation=0,
            crop_box_inherited=True,
        )
    with pytest.raises(ValueError, match="crop_box_falls_back_to_media_box"):
        PageCropState(
            page_index=0,
            direct_crop_box_present=False,
            direct_crop_box_value=None,
            effective_crop_box=(30.0, 40.0, 590.0, 800.0),
            effective_media_box=(10.0, 20.0, 610.0, 820.0),
            effective_rotation=0,
            crop_box_inherited=True,
            crop_box_falls_back_to_media_box=True,
        )
    with pytest.raises(ValueError, match="direct_crop_box_value"):
        PageCropState(
            page_index=0,
            direct_crop_box_present=True,
            direct_crop_box_value=None,
            effective_crop_box=(30.0, 40.0, 590.0, 800.0),
            effective_media_box=(10.0, 20.0, 610.0, 820.0),
            effective_rotation=0,
        )
    with pytest.raises(
        ValueError,
        match="direct_crop_box_value must be None when direct_crop_box_present is false",
    ):
        PageCropState(
            page_index=0,
            direct_crop_box_present=False,
            direct_crop_box_value=(30.0, 40.0, 590.0, 800.0),
            effective_crop_box=(30.0, 40.0, 590.0, 800.0),
            effective_media_box=(10.0, 20.0, 610.0, 820.0),
            effective_rotation=0,
        )


def test_page_crop_state_rejects_invalid_rotation_and_crop_outside_media_box() -> None:
    with pytest.raises(ValueError, match="effective_rotation"):
        PageCropState(
            page_index=0,
            direct_crop_box_present=False,
            direct_crop_box_value=None,
            effective_crop_box=(30.0, 40.0, 590.0, 800.0),
            effective_media_box=(10.0, 20.0, 610.0, 820.0),
            effective_rotation=45,
        )
    with pytest.raises(ValueError, match="effective_crop_box must stay within effective_media_box"):
        PageCropState(
            page_index=0,
            direct_crop_box_present=False,
            direct_crop_box_value=None,
            effective_crop_box=(0.0, 40.0, 590.0, 800.0),
            effective_media_box=(10.0, 20.0, 610.0, 820.0),
            effective_rotation=0,
        )
    for crop_box in (
        (30.0, 10.0, 590.0, 800.0),
        (30.0, 40.0, 620.0, 800.0),
        (30.0, 40.0, 590.0, 830.0),
    ):
        with pytest.raises(
            ValueError, match="effective_crop_box must stay within effective_media_box"
        ):
            PageCropState(
                page_index=0,
                direct_crop_box_present=False,
                direct_crop_box_value=None,
                effective_crop_box=crop_box,
                effective_media_box=(10.0, 20.0, 610.0, 820.0),
                effective_rotation=0,
            )


def test_page_crop_target_and_plan_reject_invalid_indexes_and_mismatched_targets() -> None:
    with pytest.raises(TypeError, match="page_index must be an integer"):
        PageCropTarget(page_index=False, crop_box=(10.0, 20.0, 30.0, 40.0))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="page_index must be non-negative"):
        PageCropTarget(page_index=-1, crop_box=(10.0, 20.0, 30.0, 40.0))
    with pytest.raises(ValueError, match="crop_box is invalid"):
        PageCropTarget(page_index=0, crop_box=(10.0, 20.0, 30.0))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="crop_box width must be at least 1 point"):
        PageCropTarget(page_index=0, crop_box=(10.0, 20.0, 10.5, 80.0))
    with pytest.raises(ValueError, match="crop_box height must be at least 1 point"):
        PageCropTarget(page_index=0, crop_box=(10.0, 20.0, 80.0, 20.5))
    with pytest.raises(ValueError, match="page_indexes must not be empty"):
        PageCropPlan(page_indexes=(), targets=(), reset_to_media_box=False)
    with pytest.raises(ValueError, match="page_indexes must be unique"):
        PageCropPlan(
            page_indexes=(0, 0),
            targets=(
                PageCropTarget(page_index=0, crop_box=(10.0, 20.0, 30.0, 40.0)),
                PageCropTarget(page_index=0, crop_box=(10.0, 20.0, 30.0, 40.0)),
            ),
            reset_to_media_box=False,
        )
    with pytest.raises(ValueError, match="targets length must match page_indexes"):
        PageCropPlan(
            page_indexes=(0,),
            targets=(),
            reset_to_media_box=False,
        )
    with pytest.raises(ValueError, match="page_indexes must be sorted"):
        PageCropPlan(
            page_indexes=(1, 0),
            targets=(
                PageCropTarget(page_index=1, crop_box=(10.0, 20.0, 30.0, 40.0)),
                PageCropTarget(page_index=0, crop_box=(10.0, 20.0, 30.0, 40.0)),
            ),
            reset_to_media_box=False,
        )
    with pytest.raises(ValueError, match="targets must match page_indexes"):
        PageCropPlan(
            page_indexes=(0,),
            targets=(PageCropTarget(page_index=1, crop_box=(10.0, 20.0, 30.0, 40.0)),),
            reset_to_media_box=False,
        )


def test_build_page_crop_plan_requires_states_and_margins_consistently() -> None:
    state = make_state()

    with pytest.raises(ValueError, match="selected pages must not be empty"):
        build_page_crop_plan((), margins=PageCropMargins(1.0, 1.0, 1.0, 1.0))
    with pytest.raises(ValueError, match="margins are required"):
        build_page_crop_plan((state,), margins=None)
    with pytest.raises(ValueError, match="margins must be zero"):
        build_page_crop_plan(
            (state,),
            margins=PageCropMargins(1.0, 0.0, 0.0, 0.0),
            reset_to_media_box=True,
        )
    with pytest.raises(ValueError, match="selected pages must be unique"):
        build_page_crop_plan(
            (state, make_state(page_index=0)),
            margins=PageCropMargins(1.0, 1.0, 1.0, 1.0),
        )


def test_build_page_crop_plan_rejects_target_below_minimum_extent_and_outside_media_box() -> None:
    narrow = make_state(
        crop_box=(0.0, 0.0, 12.0, 200.0),
        media_box=(0.0, 0.0, 12.0, 200.0),
    )

    with pytest.raises(ValueError, match="1"):
        build_page_crop_plan(
            (narrow,),
            margins=PageCropMargins(left=6.0, top=0.0, right=5.5, bottom=0.0),
        )
    with pytest.raises(ValueError, match="effective_crop_box must stay within effective_media_box"):
        make_state(
            crop_box=(10.0, 10.0, 90.0, 90.0),
            media_box=(20.0, 20.0, 80.0, 80.0),
        )


def test_rotation_dimensions_reject_unsupported_rotation() -> None:
    with pytest.raises(ValueError, match="rotation must be one of 0, 90, 180, 270"):
        _rotation_dimensions((0.0, 0.0, 100.0, 200.0), 45)


def test_crop_box_from_display_margins_rejects_unsupported_rotation() -> None:
    forged_state = object.__new__(PageCropState)
    object.__setattr__(forged_state, "page_index", 0)
    object.__setattr__(forged_state, "direct_crop_box_present", False)
    object.__setattr__(forged_state, "direct_crop_box_value", None)
    object.__setattr__(forged_state, "effective_crop_box", (0.0, 0.0, 100.0, 200.0))
    object.__setattr__(forged_state, "effective_media_box", (0.0, 0.0, 100.0, 200.0))
    object.__setattr__(forged_state, "effective_rotation", 45)
    object.__setattr__(forged_state, "crop_box_inherited", False)
    object.__setattr__(forged_state, "crop_box_falls_back_to_media_box", False)

    with pytest.raises(ValueError, match="unsupported rotation"):
        crop_box_from_display_margins(
            forged_state,
            PageCropMargins(1.0, 1.0, 1.0, 1.0),
        )


def test_require_raw_box_rejects_invalid_shapes_and_values() -> None:
    invalid_values: tuple[object, ...] = (
        (0.0, 0.0, 100.0),
        (0.0, 0.0, 100.0, 100.0, 200.0),
        (False, 0.0, 100.0, 100.0),
        (0.0, 0.0, math.nan, 100.0),
        (0.0, 0.0, math.inf, 100.0),
        "0,0,100,100",
    )

    for value in invalid_values:
        with pytest.raises(ValueError, match="raw is invalid"):
            _require_raw_box(value, label="raw")


def test_page_crop_state_preserves_reverse_order_raw_direct_crop_box() -> None:
    state = PageCropState(
        page_index=0,
        direct_crop_box_present=True,
        direct_crop_box_value=(590.0, 800.0, 30.0, 40.0),
        effective_crop_box=(30.0, 40.0, 590.0, 800.0),
        effective_media_box=(10.0, 20.0, 610.0, 820.0),
        effective_rotation=0,
    )

    assert state.direct_crop_box_value == (590.0, 800.0, 30.0, 40.0)


@pytest.mark.parametrize(
    ("target_box", "message"),
    [
        ((5.0, 40.0, 590.0, 800.0), "crop box must stay within media box"),
        ((30.0, 10.0, 590.0, 800.0), "crop box must stay within media box"),
        ((30.0, 40.0, 620.0, 800.0), "crop box must stay within media box"),
        ((30.0, 40.0, 590.0, 830.0), "crop box must stay within media box"),
        ((30.0, 40.0, 30.4, 800.0), "cropped width must be at least 1 point"),
        ((30.0, 40.0, 590.0, 40.4), "cropped height must be at least 1 point"),
    ],
)
def test_validate_target_for_state_rejects_invalid_target_boxes(
    target_box: tuple[float, float, float, float],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _validate_target_for_state(
            make_state(),
            target_box,
            margins=PageCropMargins(0.0, 0.0, 0.0, 0.0),
        )


def test_validate_target_for_state_allows_missing_margin_context() -> None:
    _validate_target_for_state(
        make_state(),
        (40.0, 50.0, 580.0, 790.0),
        margins=None,
    )


def test_validate_target_for_state_rejects_collapsing_display_margins() -> None:
    state = make_state(crop_box=(0.0, 0.0, 100.0, 200.0), media_box=(0.0, 0.0, 100.0, 200.0))

    with pytest.raises(ValueError, match="displayed width would collapse"):
        _validate_target_for_state(
            state,
            (10.0, 0.0, 90.0, 200.0),
            margins=PageCropMargins(50.0, 0.0, 50.0, 0.0),
        )
    with pytest.raises(ValueError, match="displayed height would collapse"):
        _validate_target_for_state(
            state,
            (0.0, 10.0, 100.0, 190.0),
            margins=PageCropMargins(0.0, 100.0, 0.0, 100.0),
        )
