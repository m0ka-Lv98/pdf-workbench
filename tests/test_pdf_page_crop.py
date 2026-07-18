from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from pdf_workbench.domain.page_crop import PageCropMargins, build_page_crop_plan
from pdf_workbench.services.pdf_page_mutation import PdfPageMutationError, PdfPageMutationService


def create_crop_source_pdf(path: Path) -> Path:
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        (
            b"2 0 obj << /Type /Pages /Count 2 /Kids [3 0 R 4 0 R] "
            b"/MediaBox [10 20 610 820] /CropBox [30 40 590 800] >> endobj\n"
        ),
        b"3 0 obj << /Type /Page /Parent 2 0 R /Resources << >> >> endobj\n",
        (
            b"4 0 obj << /Type /Page /Parent 2 0 R /Resources << >> "
            b"/CropBox [50 70 550 750] /Rotate 90 >> endobj\n"
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


def create_annotation_crop_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Square"),
            NameObject("/Rect"): ArrayObject(
                [
                    NumberObject(10),
                    NumberObject(10),
                    NumberObject(120),
                    NumberObject(120),
                ]
            ),
            NameObject("/Contents"): TextStringObject("annot"),
        }
    )
    writer.add_annotation(page_number=0, annotation=annotation)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def direct_crop_value(path: Path, page_index: int) -> tuple[float, float, float, float] | None:
    reader = PdfReader(str(path))
    root_pages = reader.trailer["/Root"]["/Pages"].get_object()
    page = root_pages["/Kids"][page_index].get_object()
    value = page.get("/CropBox", None)
    if value is None:
        return None
    return tuple(float(component) for component in value)  # type: ignore[return-value]


def annotation_rects(path: Path, page_index: int) -> tuple[tuple[float, float, float, float], ...]:
    reader = PdfReader(str(path))
    page = reader.pages[page_index]
    annots = page.get("/Annots", None)
    if annots is None:
        return ()
    return tuple(
        tuple(float(component) for component in annot.get_object()["/Rect"])  # type: ignore[misc]
        for annot in annots
    )


def test_read_crop_states_reports_inherited_and_direct_crop_boxes(tmp_path: Path) -> None:
    document_path = create_crop_source_pdf(tmp_path / "crop-states.pdf")

    states = PdfPageMutationService().read_crop_states(document_path, (0, 1))

    assert states[0].direct_crop_box_present is False
    assert states[0].direct_crop_box_value is None
    assert states[0].effective_crop_box == (30.0, 40.0, 590.0, 800.0)
    assert states[0].effective_media_box == (10.0, 20.0, 610.0, 820.0)
    assert states[0].effective_rotation == 0
    assert states[1].direct_crop_box_present is True
    assert states[1].direct_crop_box_value == (50.0, 70.0, 550.0, 750.0)
    assert states[1].effective_rotation == 90


def test_crop_pages_materializes_direct_crop_and_undo_restores_inherited_state(
    tmp_path: Path,
) -> None:
    document_path = create_crop_source_pdf(tmp_path / "crop-undo.pdf")
    service = PdfPageMutationService()
    states = service.read_crop_states(document_path, (0,))
    plan = build_page_crop_plan(states, margins=PageCropMargins(10, 20, 30, 40))

    mutation = service.crop_pages(document_path, plan)

    assert direct_crop_value(document_path, 0) == (40.0, 80.0, 560.0, 780.0)
    assert mutation.mutation_result.page_index_transition is not None
    assert mutation.mutation_result.page_index_transition.cache_old_to_new == (None, 1)

    service.undo_page_crop(document_path, mutation.receipt)
    assert direct_crop_value(document_path, 0) is None
    assert service.read_crop_states(document_path, (0,))[0].effective_crop_box == (
        30.0,
        40.0,
        590.0,
        800.0,
    )

    service.redo_page_crop(document_path, mutation.receipt)
    assert direct_crop_value(document_path, 0) == (40.0, 80.0, 560.0, 780.0)


def test_crop_pages_reset_to_media_box(tmp_path: Path) -> None:
    document_path = create_crop_source_pdf(tmp_path / "crop-reset.pdf")
    service = PdfPageMutationService()
    states = service.read_crop_states(document_path, (1,))
    plan = build_page_crop_plan(
        states,
        margins=PageCropMargins(0, 0, 0, 0),
        reset_to_media_box=True,
    )

    service.crop_pages(document_path, plan)

    assert direct_crop_value(document_path, 1) == (10.0, 20.0, 610.0, 820.0)


def test_crop_pages_preserves_annotations_and_annotation_rects(tmp_path: Path) -> None:
    document_path = create_annotation_crop_pdf(tmp_path / "crop-annotations.pdf")
    service = PdfPageMutationService()
    before_snapshot = service.snapshot_document_structure(document_path)
    states = service.read_crop_states(document_path, (0,))
    plan = build_page_crop_plan(states, margins=PageCropMargins(20, 20, 20, 20))

    mutation = service.crop_pages(document_path, plan)

    after_snapshot = service.snapshot_document_structure(document_path)
    assert before_snapshot.pages[0].annotations == after_snapshot.pages[0].annotations
    assert annotation_rects(document_path, 0) == ((10.0, 10.0, 120.0, 120.0),)

    service.undo_page_crop(document_path, mutation.receipt)
    restored_snapshot = service.snapshot_document_structure(document_path)
    assert restored_snapshot == before_snapshot


def test_crop_pages_rejects_redo_when_current_snapshot_drifted(tmp_path: Path) -> None:
    document_path = create_crop_source_pdf(tmp_path / "crop-redo-drift.pdf")
    service = PdfPageMutationService()
    states = service.read_crop_states(document_path, (0,))
    plan = build_page_crop_plan(states, margins=PageCropMargins(10, 20, 30, 40))

    mutation = service.crop_pages(document_path, plan)
    service.undo_page_crop(document_path, mutation.receipt)
    with pikepdf.open(document_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/CropBox"] = pikepdf.Array([35, 45, 585, 795])
        pdf.save(document_path)

    with pytest.raises(PdfPageMutationError, match="前提状態が変化"):
        service.redo_page_crop(document_path, mutation.receipt)
