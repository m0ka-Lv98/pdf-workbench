from __future__ import annotations

import pytest

from pdf_workbench.domain.page_replacement import (
    PageReplacementPlan,
    build_page_replacement_plan,
)


def test_build_page_replacement_plan_builds_expected_mappings() -> None:
    plan = build_page_replacement_plan(8, 6, (1, 4, 7), (0, 2, 5))

    assert plan.target_page_indexes == (1, 4, 7)
    assert plan.source_page_indexes == (0, 2, 5)
    assert plan.replacement_pairs == ((1, 0), (4, 2), (7, 5))
    assert plan.replaced_page_indexes_after == (1, 4, 7)
    assert plan.execute_cache_old_to_new == (0, None, 2, 3, None, 5, 6, None)
    assert plan.execute_current_page_old_to_new == tuple(range(8))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"target_page_count": 0}, "target_page_count must be positive"),
        ({"source_page_count": 0}, "source_page_count must be positive"),
        ({"target_page_indexes": ()}, "target_page_indexes must not be empty"),
        ({"source_page_indexes": ()}, "source_page_indexes must not be empty"),
        (
            {"target_page_indexes": (2, 1), "replacement_pairs": ((2, 0), (1, 1))},
            "target_page_indexes must be ascending",
        ),
        (
            {"source_page_indexes": (0, 0), "replacement_pairs": ((1, 0), (2, 0))},
            "source_page_indexes must be unique",
        ),
        (
            {"source_page_indexes": (0,), "replacement_pairs": ((1, 0),)},
            "target and source page selections must be the same length",
        ),
        (
            {"target_page_indexes": (1, 4), "replacement_pairs": ((1, 0), (4, 3))},
            "target_page_indexes must stay within the page range",
        ),
        (
            {"replacement_pairs": ((1, 0), (3, 2))},
            "replacement_pairs does not match the replacement selections",
        ),
        (
            {"replaced_page_indexes_after": (1, 3)},
            "replaced_page_indexes_after does not match target_page_indexes",
        ),
        (
            {"execute_cache_old_to_new": (0, 1, 2, 3)},
            "execute_cache_old_to_new does not match the replacement selection",
        ),
        (
            {"execute_current_page_old_to_new": (1, 0, 2, 3)},
            "execute_current_page_old_to_new does not preserve current page indexes",
        ),
    ],
)
def test_page_replacement_plan_rejects_invalid_invariants(
    kwargs: dict[str, object],
    message: str,
) -> None:
    valid = dict(
        target_page_count=4,
        source_page_count=4,
        target_page_indexes=(1, 2),
        source_page_indexes=(0, 3),
        replacement_pairs=((1, 0), (2, 3)),
        replaced_page_indexes_after=(1, 2),
        execute_cache_old_to_new=(0, None, None, 3),
        execute_current_page_old_to_new=(0, 1, 2, 3),
    )
    valid.update(kwargs)

    with pytest.raises((TypeError, ValueError), match=message):
        PageReplacementPlan(**valid)


def test_page_replacement_plan_rejects_bool_values() -> None:
    with pytest.raises(TypeError, match="target_page_count must be an integer"):
        build_page_replacement_plan(True, 3, (0,), (0,))  # type: ignore[arg-type]
