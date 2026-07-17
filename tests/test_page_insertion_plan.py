from __future__ import annotations

import pytest

from pdf_workbench.domain.page_insertion import PageInsertionPlan, build_page_insertion_plan


def test_build_page_insertion_plan_computes_inserted_indexes_and_mapping() -> None:
    plan = build_page_insertion_plan(5, 3, (0, 2), 2)

    assert plan.inserted_page_indexes_after == (2, 3)
    assert plan.target_old_to_new == (0, 1, 4, 5, 6)


def test_build_page_insertion_plan_supports_beginning_middle_and_end() -> None:
    assert build_page_insertion_plan(4, 2, (0,), 0).inserted_page_indexes_after == (0,)
    assert build_page_insertion_plan(4, 2, (0,), 2).inserted_page_indexes_after == (2,)
    assert build_page_insertion_plan(4, 2, (0, 1), 4).inserted_page_indexes_after == (4, 5)


@pytest.mark.parametrize(
    ("target_page_count", "source_page_count", "source_page_indexes", "insertion_slot"),
    [
        (0, 2, (0,), 0),
        (4, 0, (0,), 0),
        (4, 2, (), 0),
        (4, 2, (1, 0), 0),
        (4, 2, (0, 0), 0),
        (4, 2, (2,), 0),
        (4, 2, (0,), 5),
        (4, 2, (0,), True),
    ],
)
def test_build_page_insertion_plan_rejects_invalid_inputs(
    target_page_count: object,
    source_page_count: object,
    source_page_indexes: tuple[object, ...],
    insertion_slot: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        build_page_insertion_plan(
            target_page_count,
            source_page_count,
            source_page_indexes,
            insertion_slot,
        )


def test_page_insertion_plan_constructor_enforces_canonical_values() -> None:
    plan = PageInsertionPlan(
        target_page_count=5,
        source_page_count=3,
        source_page_indexes=(0, 2),
        insertion_slot=2,
        inserted_page_indexes_after=(2, 3),
        target_old_to_new=(0, 1, 4, 5, 6),
    )
    assert plan.inserted_page_indexes_after == (2, 3)

    with pytest.raises(ValueError, match="requested insertion"):
        PageInsertionPlan(
            target_page_count=5,
            source_page_count=3,
            source_page_indexes=(0, 2),
            insertion_slot=2,
            inserted_page_indexes_after=(1, 2),
            target_old_to_new=(0, 1, 4, 5, 6),
        )
