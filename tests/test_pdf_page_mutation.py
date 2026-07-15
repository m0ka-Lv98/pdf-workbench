from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader

from pdf_test_utils import create_blank_pdf
from pdf_workbench.services.pdf_page_mutation import (
    PageRotationState,
    PdfPageMutationError,
    PdfPageMutationService,
    PdfPageRotationValidationError,
)


def create_inherited_rotation_fixture(path: Path) -> Path:
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


def page_rotate_values(path: Path) -> tuple[object | None, ...]:
    with pikepdf.open(str(path)) as pdf:
        return tuple(page.obj.get("/Rotate", None) for page in pdf.pages)


def raw_page_rotate_values(path: Path) -> tuple[object | None, ...]:
    reader = PdfReader(str(path))
    root_pages = reader.trailer["/Root"]["/Pages"].get_object()
    return tuple(kid.get_object().get("/Rotate", None) for kid in root_pages["/Kids"])


def test_read_rotation_states_reports_direct_and_inherited_rotation(tmp_path: Path) -> None:
    document_path = create_inherited_rotation_fixture(tmp_path / "rotation-states.pdf")
    service = PdfPageMutationService()

    states = service.read_rotation_states(document_path, (0, 1))

    assert states == (
        PageRotationState(
            page_index=0,
            direct_rotate_present=True,
            direct_rotate_value=90,
            effective_rotation=90,
        ),
        PageRotationState(
            page_index=1,
            direct_rotate_present=False,
            direct_rotate_value=None,
            effective_rotation=180,
        ),
    )


def test_apply_rotation_states_can_remove_direct_rotate_key(tmp_path: Path) -> None:
    document_path = create_inherited_rotation_fixture(tmp_path / "rotation-remove-key.pdf")
    service = PdfPageMutationService()

    service.apply_rotation_states(
        document_path,
        (
            PageRotationState(
                page_index=0,
                direct_rotate_present=False,
                direct_rotate_value=None,
                effective_rotation=180,
            ),
        ),
    )

    reader = PdfReader(str(document_path))
    root_pages = reader.trailer["/Root"]["/Pages"].get_object()
    assert root_pages.get("/Rotate", None) == 180
    assert raw_page_rotate_values(document_path)[0] is None


def test_apply_rotation_states_validates_candidate_before_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document_path = create_blank_pdf(tmp_path / "rotation-validate.pdf", 1)
    original_bytes = document_path.read_bytes()
    service = PdfPageMutationService()

    monkeypatch.setattr(
        service,
        "_validate_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfPageMutationError("validation failed")),
    )

    with pytest.raises(PdfPageMutationError, match="validation failed"):
        service.apply_rotation_states(
            document_path,
            (
                PageRotationState(
                    page_index=0,
                    direct_rotate_present=True,
                    direct_rotate_value=90,
                    effective_rotation=90,
                ),
            ),
        )

    assert document_path.read_bytes() == original_bytes


def test_read_rotation_states_rejects_non_right_angle_rotation(tmp_path: Path) -> None:
    document_path = create_blank_pdf(tmp_path / "rotation-invalid.pdf", 1)
    with pikepdf.open(str(document_path), allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Rotate"] = 45
        pdf.save(str(document_path))
    service = PdfPageMutationService()
    original_bytes = document_path.read_bytes()

    with pytest.raises(PdfPageRotationValidationError, match="90度単位"):
        service.read_rotation_states(document_path, (0,))

    assert document_path.read_bytes() == original_bytes
