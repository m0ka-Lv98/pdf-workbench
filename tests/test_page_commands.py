from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader

from pdf_regression_utils import extract_pdfium_text, file_sha256
from pdf_test_utils import create_blank_pdf, create_simple_text_pdf
from pdf_workbench.domain.command_history import (
    CommandExecutionError,
    CommandHistory,
    CommandRedoError,
    CommandUndoError,
)
from pdf_workbench.domain.page_commands import (
    DeletePagesCommand,
    DuplicatePagesCommand,
    InsertPagesCommand,
    ReorderPagesCommand,
    RotatePagesCommand,
)
from pdf_workbench.domain.page_insertion import build_page_insertion_plan
from pdf_workbench.domain.page_reorder import build_page_reorder_plan
from pdf_workbench.services.pdf_page_mutation import (
    PdfPageMutationError,
    PdfPageMutationService,
)
from pdf_workbench.services.pdf_renderer import DocumentRevision


def create_command_fixture(path: Path) -> Path:
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Count 3 /Kids [3 0 R 4 0 R 5 0 R] /Rotate 180 >> endobj\n",
        (
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << >> /Rotate 90 >> endobj\n"
        ),
        (
            b"4 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << >> >> endobj\n"
        ),
        (
            b"5 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << >> >> endobj\n"
        ),
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj)
    xref_start = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(pdf)
    return path


def effective_rotations(path: Path) -> tuple[int, ...]:
    with pikepdf.open(str(path)) as pdf:
        rotations: list[int] = []
        inherited = int(pdf.Root.Pages.get("/Rotate", 0))
        for page in pdf.pages:
            direct = page.obj.get("/Rotate", None)
            rotations.append(int(direct) if direct is not None else inherited)
        return tuple(rotation % 360 for rotation in rotations)


def raw_page_rotate_values(path: Path) -> tuple[object | None, ...]:
    reader = PdfReader(str(path))
    root_pages = reader.trailer["/Root"]["/Pages"].get_object()
    return tuple(kid.get_object().get("/Rotate", None) for kid in root_pages["/Kids"])


def page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


def set_direct_rotate(path: Path, page_index: int, rotation: int) -> None:
    with pikepdf.open(str(path), allow_overwriting_input=True) as pdf:
        pdf.pages[page_index].obj["/Rotate"] = rotation
        pdf.save(str(path))


def test_rotate_pages_command_executes_undoes_and_redoes(tmp_path: Path) -> None:
    document_path = create_command_fixture(tmp_path / "rotate-command.pdf")
    command = RotatePagesCommand(
        document_path,
        (0, 1),
        PdfPageMutationService(),
    )

    execute_change = command.execute()
    assert execute_change.requires_reload is True
    assert execute_change.affected_pages == frozenset({0, 1})
    assert execute_change.mutation_result is not None
    assert effective_rotations(document_path) == (180, 270, 180)

    undo_change = command.undo()
    assert undo_change.requires_reload is True
    assert effective_rotations(document_path) == (90, 180, 180)
    assert raw_page_rotate_values(document_path)[1] is None

    redo_change = command.redo()
    assert redo_change.requires_reload is True
    assert effective_rotations(document_path) == (180, 270, 180)


def test_rotate_pages_command_exposes_latest_mutation_result(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "rotate-result.pdf", 2)
    command = RotatePagesCommand(document_path, (0,), PdfPageMutationService())

    change = command.execute()

    mutation_result = change.mutation_result
    assert mutation_result is not None
    assert mutation_result.affected_pages == frozenset({0})
    assert mutation_result.page_count == 2
    assert isinstance(mutation_result.old_revision, DocumentRevision)
    assert isinstance(mutation_result.new_revision, DocumentRevision)
    assert mutation_result.old_revision != mutation_result.new_revision


def test_rotate_pages_command_rejects_invalid_configuration(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "rotate-invalid.pdf", 1)
    service = PdfPageMutationService()

    with pytest.raises(ValueError, match="must not be empty"):
        RotatePagesCommand(document_path, (), service)
    with pytest.raises(ValueError, match="non-negative"):
        RotatePagesCommand(document_path, (-1,), service)
    with pytest.raises(ValueError, match="clockwise 90-degree rotation"):
        RotatePagesCommand(document_path, (0,), service, degrees=180)


def test_rotate_pages_command_rejects_out_of_range_indexes_on_execute(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "rotate-range.pdf", 1)
    command = RotatePagesCommand(document_path, (1,), PdfPageMutationService())

    with pytest.raises(ValueError, match="out of range"):
        command.execute()


@pytest.mark.parametrize("raw_value", [-90, 0, 360, 450])
def test_rotate_pages_command_undo_restores_exact_direct_rotate_value(
    tmp_path: Path,
    raw_value: int,
) -> None:
    document_path = create_blank_pdf(tmp_path / f"rotate-{raw_value}.pdf", 1)
    set_direct_rotate(document_path, 0, raw_value)
    command = RotatePagesCommand(document_path, (0,), PdfPageMutationService())

    command.execute()
    command.undo()

    assert raw_page_rotate_values(document_path) == (raw_value,)


def test_rotate_pages_command_rejects_non_integer_indexes(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "rotate-non-integer.pdf", 1)
    service = PdfPageMutationService()

    with pytest.raises(TypeError, match="integers"):
        RotatePagesCommand(document_path, (1.9,), service)


def test_duplicate_pages_command_executes_undoes_and_redoes(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "duplicate-command.pdf", 5)
    command = DuplicatePagesCommand(
        document_path,
        (1, 3),
        PdfPageMutationService(),
    )

    execute_change = command.execute()
    assert execute_change.requires_reload is True
    assert execute_change.selected_page_indexes_after == (2, 5)
    assert execute_change.mutation_result is not None
    assert execute_change.mutation_result.page_count == 7
    assert execute_change.mutation_result.page_index_transition is not None
    assert page_count(document_path) == 7

    undo_change = command.undo()
    assert undo_change.requires_reload is True
    assert undo_change.selected_page_indexes_after == (1, 3)
    assert undo_change.mutation_result is not None
    assert undo_change.mutation_result.page_count == 5
    assert undo_change.mutation_result.page_index_transition is not None
    assert page_count(document_path) == 5

    redo_change = command.redo()
    assert redo_change.requires_reload is True
    assert redo_change.selected_page_indexes_after == (2, 5)
    assert redo_change.mutation_result is not None
    assert redo_change.mutation_result.page_count == 7
    assert page_count(document_path) == 7


def test_duplicate_pages_command_rejects_invalid_configuration(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "duplicate-invalid.pdf", 1)
    service = PdfPageMutationService()

    with pytest.raises(ValueError, match="must not be empty"):
        DuplicatePagesCommand(document_path, (), service)
    with pytest.raises(ValueError, match="non-negative"):
        DuplicatePagesCommand(document_path, (-1,), service)
    with pytest.raises(TypeError, match="integers"):
        DuplicatePagesCommand(document_path, (1.5,), service)
    with pytest.raises(TypeError, match="integers"):
        DuplicatePagesCommand(document_path, ("1",), service)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="integers"):
        DuplicatePagesCommand(document_path, (True,), service)  # type: ignore[arg-type]


def test_duplicate_pages_command_normalizes_selection_and_description(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "duplicate-normalize.pdf", 4)
    command = DuplicatePagesCommand(
        document_path,
        (3, 1, 1),
        PdfPageMutationService(),
    )

    assert command.description == "2ページを複製"
    assert command.affected_pages == frozenset({1, 3})

    execute_change = command.execute()
    assert execute_change.selected_page_indexes_after == (2, 5)


def test_duplicate_pages_command_rejects_out_of_range_indexes_on_execute(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "duplicate-range.pdf", 1)
    command = DuplicatePagesCommand(document_path, (1,), PdfPageMutationService())

    with pytest.raises(ValueError, match="out of range"):
        command.execute()


def test_duplicate_pages_command_rejects_undo_and_redo_before_execute(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "duplicate-before-execute.pdf", 1)
    command = DuplicatePagesCommand(document_path, (0,), PdfPageMutationService())

    with pytest.raises(RuntimeError, match="not been executed"):
        command.undo()
    with pytest.raises(RuntimeError, match="not been executed"):
        command.redo()


def test_insert_pages_command_executes_undoes_and_redoes(tmp_path: Path) -> None:
    target_path = create_simple_text_pdf(tmp_path / "insert-target.pdf", ["A", "B"])
    source_path = create_simple_text_pdf(tmp_path / "insert-source.pdf", ["X", "Y"])
    service = PdfPageMutationService()
    command = InsertPagesCommand(
        target_path,
        source_path,
        build_page_insertion_plan(2, 2, (0, 1), 1),
        service,
        current_page_index_before=0,
        selected_page_indexes_before=(0,),
    )

    execute_change = command.execute()
    assert execute_change.requires_reload is True
    assert execute_change.selected_page_indexes_after == (1, 2)
    assert execute_change.current_page_index_after == 1

    undo_change = command.undo()
    assert undo_change.selected_page_indexes_after == (0,)
    assert undo_change.current_page_index_after == 0

    source_path.write_bytes(b"%PDF-1.4\nbroken")
    redo_change = command.redo()
    assert redo_change.selected_page_indexes_after == (1, 2)
    assert redo_change.current_page_index_after == 1

    command.dispose()
    assert command._receipt is None


def test_insert_pages_command_rejects_use_after_dispose(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "insert-dispose-target.pdf", 2)
    source_path = create_blank_pdf(tmp_path / "insert-dispose-source.pdf", 1)
    command = InsertPagesCommand(
        target_path,
        source_path,
        build_page_insertion_plan(2, 1, (0,), 1),
        PdfPageMutationService(),
        current_page_index_before=0,
        selected_page_indexes_before=(0,),
    )

    command.execute()
    command.dispose()

    with pytest.raises(RuntimeError, match="disposed"):
        command.undo()


def test_duplicate_pages_command_redo_preflight_failure_preserves_working_copy(
    tmp_path: Path,
) -> None:
    document_path = create_blank_pdf(tmp_path / "duplicate-redo-preflight.pdf", 3)
    command = DuplicatePagesCommand(document_path, (1,), PdfPageMutationService())

    command.execute()
    command.undo()
    pristine_sha = file_sha256(document_path)

    with pikepdf.open(document_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Rotate"] = 90
        pdf.save(document_path)

    changed_sha = file_sha256(document_path)
    assert changed_sha != pristine_sha

    with pytest.raises(PdfPageMutationError, match="前提状態"):
        command.redo()


def test_delete_pages_command_executes_undoes_and_redoes(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-command.pdf", 5)
    command = DeletePagesCommand(
        document_path,
        (1, 3),
        3,
        PdfPageMutationService(),
    )

    execute_change = command.execute()
    assert execute_change.requires_reload is True
    assert execute_change.selected_page_indexes_after == ()
    assert execute_change.current_page_index_after is None
    assert execute_change.mutation_result is not None
    assert execute_change.mutation_result.page_count == 3
    assert execute_change.mutation_result.page_index_transition is not None
    assert execute_change.mutation_result.page_index_transition.cache_old_to_new == (
        0,
        None,
        1,
        None,
        2,
    )
    assert execute_change.mutation_result.page_index_transition.current_page_old_to_new == (
        0,
        1,
        1,
        2,
        2,
    )
    assert page_count(document_path) == 3

    undo_change = command.undo()
    assert undo_change.selected_page_indexes_after == (1, 3)
    assert undo_change.current_page_index_after == 3
    assert undo_change.mutation_result is not None
    assert undo_change.mutation_result.page_count == 5
    assert undo_change.mutation_result.page_index_transition is not None
    assert undo_change.mutation_result.page_index_transition.cache_old_to_new == (0, 2, 4)
    assert page_count(document_path) == 5

    redo_change = command.redo()
    assert redo_change.selected_page_indexes_after == ()
    assert redo_change.current_page_index_after is None
    assert redo_change.mutation_result is not None
    assert redo_change.mutation_result.page_count == 3
    assert page_count(document_path) == 3


def test_delete_pages_command_rejects_invalid_configuration(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-invalid.pdf", 2)
    service = PdfPageMutationService()

    with pytest.raises(ValueError, match="must not be empty"):
        DeletePagesCommand(document_path, (), 0, service)
    with pytest.raises(ValueError, match="non-negative"):
        DeletePagesCommand(document_path, (-1,), 0, service)
    with pytest.raises(TypeError, match="integers"):
        DeletePagesCommand(document_path, (1.5,), 0, service)
    with pytest.raises(TypeError, match="integers"):
        DeletePagesCommand(document_path, ("1",), 0, service)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="integers"):
        DeletePagesCommand(document_path, (True,), 0, service)  # type: ignore[arg-type]


def test_delete_pages_command_rejects_all_pages_and_out_of_range_on_execute(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-range.pdf", 2)

    with pytest.raises(ValueError, match="out of range"):
        DeletePagesCommand(document_path, (2,), 0, PdfPageMutationService()).execute()
    with pytest.raises(PdfPageMutationError, match="少なくとも1ページは残す必要があります"):
        DeletePagesCommand(document_path, (0, 1), 0, PdfPageMutationService()).execute()


def test_delete_pages_command_normalizes_selection_and_description(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-normalize.pdf", 5)
    command = DeletePagesCommand(document_path, (3, 1, 1), 1, PdfPageMutationService())

    assert command.description == "2ページを削除"
    assert command.affected_pages == frozenset()

    execute_change = command.execute()
    assert execute_change.mutation_result is not None
    assert execute_change.mutation_result.page_index_transition is not None
    assert execute_change.mutation_result.page_index_transition.cache_old_to_new == (
        0,
        None,
        1,
        None,
        2,
    )


def test_delete_pages_command_rejects_undo_and_redo_before_execute(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-before-execute.pdf", 2)
    command = DeletePagesCommand(document_path, (0,), 0, PdfPageMutationService())

    with pytest.raises(RuntimeError, match="not been executed"):
        command.undo()
    with pytest.raises(RuntimeError, match="not been executed"):
        command.redo()


def test_delete_pages_command_redo_preflight_failure_preserves_working_copy(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-redo-preflight.pdf", 3)
    command = DeletePagesCommand(document_path, (1,), 1, PdfPageMutationService())

    command.execute()
    command.undo()
    pristine_sha = file_sha256(document_path)

    with pikepdf.open(document_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Rotate"] = 90
        pdf.save(document_path)

    changed_sha = file_sha256(document_path)
    assert changed_sha != pristine_sha

    with pytest.raises(PdfPageMutationError, match="前提状態"):
        command.redo()


def test_delete_pages_command_dispose_cleans_snapshot_and_rejects_future_use(
    tmp_path: Path,
) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-dispose.pdf", 3)
    command = DeletePagesCommand(document_path, (1,), 1, PdfPageMutationService())

    command.execute()
    receipt = command._receipt
    assert receipt is not None
    assert receipt.undo_snapshot_path.exists()

    command.dispose()
    assert not receipt.undo_snapshot_path.exists()

    command.dispose()

    with pytest.raises(RuntimeError, match="disposed"):
        command.undo()
    with pytest.raises(RuntimeError, match="disposed"):
        command.redo()
    assert page_count(document_path) == 2


@pytest.mark.parametrize(
    ("current_page_index", "expected_error", "expected_message"),
    [
        (-1, ValueError, "current_page_index must stay within the page range"),
        (3, ValueError, "current_page_index must stay within the page range"),
        (4, ValueError, "current_page_index must stay within the page range"),
        (True, TypeError, "current_page_index must be an integer"),
        (1.5, TypeError, "current_page_index must be an integer"),
        ("1", TypeError, "current_page_index must be an integer"),
    ],
)
def test_delete_pages_command_invalid_current_page_preserves_history_cursor(
    tmp_path: Path,
    current_page_index: object,
    expected_error: type[Exception],
    expected_message: str,
) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-invalid-current-page.pdf", 3)
    history = CommandHistory()
    command = DeletePagesCommand(
        document_path,
        (1,),
        current_page_index,  # type: ignore[arg-type]
        PdfPageMutationService(),
    )
    working_sha_before = file_sha256(document_path)

    with pytest.raises(CommandExecutionError) as exc_info:
        history.execute(command)

    assert isinstance(exc_info.value.cause, expected_error)
    assert expected_message in str(exc_info.value.cause)
    assert history.can_undo is False
    assert history.can_redo is False
    assert history.is_dirty is False
    assert file_sha256(document_path) == working_sha_before
    assert list(tmp_path.glob(".*.delete-undo.*.pdf")) == []


def test_delete_pages_command_undo_failure_preserves_history_cursor(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-undo-cursor.pdf", 4)
    service = PdfPageMutationService()
    history = CommandHistory()
    command = DeletePagesCommand(document_path, (1, 3), 2, service)

    history.execute(command)
    deleted_state_sha = file_sha256(document_path)
    receipt = command._receipt
    assert receipt is not None

    def fail_transition(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("undo transition failed")

    service._build_delete_undo_transition = fail_transition  # type: ignore[method-assign]

    with pytest.raises(CommandUndoError):
        history.undo()

    assert history.can_undo is True
    assert history.can_redo is False
    assert file_sha256(document_path) == deleted_state_sha
    assert receipt.undo_snapshot_path.exists()


def test_delete_pages_command_redo_failure_preserves_history_cursor(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-redo-cursor.pdf", 4)
    service = PdfPageMutationService()
    history = CommandHistory()
    command = DeletePagesCommand(document_path, (1, 3), 2, service)

    history.execute(command)
    history.undo()
    original_state_sha = file_sha256(document_path)
    receipt = command._receipt
    assert receipt is not None

    def fail_transition(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("redo transition failed")

    service._build_delete_execute_transition = fail_transition  # type: ignore[method-assign]

    with pytest.raises(CommandRedoError):
        history.redo()

    assert history.can_undo is False
    assert history.can_redo is True
    assert file_sha256(document_path) == original_state_sha
    assert receipt.undo_snapshot_path.exists()


def test_reorder_pages_command_executes_undoes_and_redoes(tmp_path: Path) -> None:
    document_path = create_simple_text_pdf(
        tmp_path / "reorder-command.pdf",
        ["A", "B", "C", "D", "E", "F"],
    )
    command = ReorderPagesCommand(
        document_path,
        build_page_reorder_plan(6, (1, 3), 6),
        PdfPageMutationService(),
    )

    execute_change = command.execute()
    assert execute_change.requires_reload is True
    assert execute_change.affected_pages == frozenset({1, 2, 3, 4, 5})
    assert execute_change.selected_page_indexes_after == (4, 5)
    assert execute_change.current_page_index_after is None
    assert execute_change.mutation_result is not None
    assert execute_change.mutation_result.page_index_transition is not None
    assert execute_change.mutation_result.page_index_transition.current_page_old_to_new == (
        0,
        4,
        1,
        5,
        2,
        3,
    )
    assert extract_pdfium_text(document_path) == "A C E F B D"

    undo_change = command.undo()
    assert undo_change.requires_reload is True
    assert undo_change.selected_page_indexes_after == (1, 3)
    assert undo_change.current_page_index_after is None
    assert extract_pdfium_text(document_path) == "A B C D E F"

    redo_change = command.redo()
    assert redo_change.requires_reload is True
    assert redo_change.selected_page_indexes_after == (4, 5)
    assert redo_change.current_page_index_after is None
    assert extract_pdfium_text(document_path) == "A C E F B D"


def test_reorder_pages_command_description_and_affected_pages(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "reorder-description.pdf", 5)
    single_command = ReorderPagesCommand(
        document_path,
        build_page_reorder_plan(5, (1,), 5),
        PdfPageMutationService(),
    )
    multi_command = ReorderPagesCommand(
        document_path,
        build_page_reorder_plan(5, (1, 3), 5),
        PdfPageMutationService(),
    )
    canonicalized_multi_command = ReorderPagesCommand(
        document_path,
        build_page_reorder_plan(5, (3, 1, 3, 1), 5),
        PdfPageMutationService(),
    )

    assert single_command.description == "1ページを移動"
    assert single_command.affected_pages == frozenset({1, 2, 3, 4})
    assert multi_command.description == "2ページを移動"
    assert multi_command.affected_pages == frozenset({1, 2, 3, 4})
    assert canonicalized_multi_command.description == "2ページを移動"


def test_reorder_pages_command_rejects_undo_and_redo_before_execute(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "reorder-before-execute.pdf", 3)
    command = ReorderPagesCommand(
        document_path,
        build_page_reorder_plan(3, (1,), 3),
        PdfPageMutationService(),
    )

    with pytest.raises(RuntimeError, match="not been executed"):
        command.undo()
    with pytest.raises(RuntimeError, match="not been executed"):
        command.redo()


def test_reorder_pages_command_redo_preflight_failure_preserves_working_copy(
    tmp_path: Path,
) -> None:
    document_path = create_simple_text_pdf(tmp_path / "reorder-redo.pdf", ["A", "B", "C", "D"])
    command = ReorderPagesCommand(
        document_path,
        build_page_reorder_plan(4, (1,), 4),
        PdfPageMutationService(),
    )

    command.execute()
    command.undo()
    pristine_sha = file_sha256(document_path)

    with pikepdf.open(document_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Rotate"] = 90
        pdf.save(document_path)

    changed_sha = file_sha256(document_path)
    assert changed_sha != pristine_sha

    with pytest.raises(PdfPageMutationError, match="前提状態"):
        command.redo()


def test_reorder_pages_command_is_single_history_entry_for_multi_page_move(tmp_path: Path) -> None:
    document_path = create_simple_text_pdf(tmp_path / "reorder-history.pdf", ["A", "B", "C", "D"])
    history = CommandHistory()
    command = ReorderPagesCommand(
        document_path,
        build_page_reorder_plan(4, (1, 3), 4),
        PdfPageMutationService(),
    )

    execute_change = history.execute(command)

    assert execute_change.selected_page_indexes_after == (2, 3)
    assert history.can_undo is True
    assert history.undo_description == "2ページを移動"
    assert extract_pdfium_text(document_path) == "A C B D"

    undo_change = history.undo()

    assert undo_change.selected_page_indexes_after == (1, 3)
    assert history.can_undo is False
    assert history.can_redo is True
    assert history.redo_description == "2ページを移動"
    assert extract_pdfium_text(document_path) == "A B C D"


def test_delete_pages_command_dispose_failure_preserves_receipt_and_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_path = create_blank_pdf(tmp_path / "delete-dispose-failure.pdf", 3)
    command = DeletePagesCommand(document_path, (1,), 1, PdfPageMutationService())

    command.execute()
    receipt = command._receipt
    assert receipt is not None

    def fail_discard(_working_copy_path: Path, _receipt: object) -> None:
        raise PdfPageMutationError("cleanup failed")

    monkeypatch.setattr(
        command._mutation_service,
        "discard_page_deletion_receipt",
        fail_discard,
    )

    with pytest.raises(PdfPageMutationError, match="cleanup failed"):
        command.dispose()

    assert command._receipt is receipt
    assert command._disposed is False
    assert receipt.undo_snapshot_path.exists()
