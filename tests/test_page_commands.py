from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader

from pdf_test_utils import create_blank_pdf
from pdf_workbench.domain.page_commands import RotatePagesCommand
from pdf_workbench.services.pdf_page_mutation import PdfPageMutationService
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


def test_rotate_pages_command_executes_undoes_and_redoes(tmp_path: Path) -> None:
    document_path = create_command_fixture(tmp_path / "rotate-command.pdf")
    events: list[str] = []
    command = RotatePagesCommand(
        document_path,
        (0, 1),
        PdfPageMutationService(),
        prepare_mutation=lambda: events.append("prepare"),
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
    assert events == ["prepare", "prepare", "prepare"]


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
