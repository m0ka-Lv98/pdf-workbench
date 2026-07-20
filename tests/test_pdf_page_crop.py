from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from pdf_workbench.domain.mutation import PageIndexTransition
from pdf_workbench.domain.page_crop import (
    PageCropMargins,
    PageCropState,
    build_page_crop_plan,
)
from pdf_workbench.services.pdf_page_mutation import (
    AnnotationParentState,
    PdfAnnotationStructureSnapshot,
    PdfNamedDestinationSnapshot,
    PdfOutlineItemSnapshot,
    PdfPageMutationError,
    PdfPageMutationService,
)


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


def mutation_candidate_paths(path: Path) -> set[Path]:
    return set(path.parent.glob(f".{path.stem}.mutation.*.tmp.pdf"))


def unsafe_page_crop_state(
    state: PageCropState,
    **changes: object,
) -> PageCropState:
    values = {field.name: getattr(state, field.name) for field in fields(PageCropState)}
    values.update(changes)
    invalid_state = object.__new__(PageCropState)
    for name, value in values.items():
        object.__setattr__(invalid_state, name, value)
    return invalid_state


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


def test_crop_receipt_rejects_invalid_direct_raw_crop_values(tmp_path: Path) -> None:
    document_path = create_crop_source_pdf(tmp_path / "crop-receipt-raw.pdf")
    service = PdfPageMutationService()
    states = service.read_crop_states(document_path, (1,))
    plan = build_page_crop_plan(states, margins=PageCropMargins(10, 20, 30, 40))
    mutation = service.crop_pages(document_path, plan)
    receipt = mutation.receipt

    invalid_values: tuple[object, ...] = (
        (1.0, 2.0, 3.0),
        (1.0, 2.0, 3.0, 4.0, 5.0),
        (True, 2.0, 3.0, 4.0),
        (1.0, 2.0, float("nan"), 4.0),
        (1.0, 2.0, float("inf"), 4.0),
    )
    for invalid_value in invalid_values:
        invalid_state = unsafe_page_crop_state(
            receipt.original_crop_states[0],
            direct_crop_box_present=True,
            direct_crop_box_value=invalid_value,  # type: ignore[arg-type]
        )
        with pytest.raises(ValueError, match="direct_crop_box_value is invalid"):
            replace(receipt, original_crop_states=(invalid_state,))

    with pytest.raises(ValueError, match="direct_crop_box_value must be None"):
        replace(
            receipt,
            original_crop_states=(
                replace(
                    receipt.original_crop_states[0],
                    direct_crop_box_present=False,
                    direct_crop_box_value=(1.0, 2.0, 3.0, 4.0),
                ),
            ),
        )


def test_crop_receipt_rejects_invalid_changed_indexes_and_path(tmp_path: Path) -> None:
    document_path = create_crop_source_pdf(tmp_path / "crop-receipt-indexes.pdf")
    service = PdfPageMutationService()
    states = service.read_crop_states(document_path, (0,))
    plan = build_page_crop_plan(states, margins=PageCropMargins(10, 20, 30, 40))
    receipt = service.crop_pages(document_path, plan).receipt

    with pytest.raises(ValueError, match="changed_page_indexes must not be empty"):
        replace(receipt, changed_page_indexes=())
    with pytest.raises(ValueError, match="changed_page_indexes must be unique"):
        replace(receipt, changed_page_indexes=(0, 0))
    with pytest.raises(ValueError, match="changed_page_indexes must stay within the page range"):
        replace(receipt, changed_page_indexes=(99,))
    with pytest.raises(ValueError, match="working_copy_path must point to a PDF"):
        replace(receipt, working_copy_path=document_path.with_suffix(".txt"))


def test_crop_receipt_rejects_semantic_snapshot_and_state_tampering(tmp_path: Path) -> None:
    document_path = create_crop_source_pdf(tmp_path / "crop-receipt-semantic.pdf")
    service = PdfPageMutationService()
    states = service.read_crop_states(document_path, (0,))
    plan = build_page_crop_plan(states, margins=PageCropMargins(10, 20, 30, 40))
    receipt = service.crop_pages(document_path, plan).receipt
    after_page = receipt.after_snapshot.pages[0]

    with pytest.raises(ValueError, match="original crop state page index"):
        replace(
            receipt,
            original_crop_states=(replace(receipt.original_crop_states[0], page_index=1),),
        )
    with pytest.raises(ValueError, match="target crop state page index"):
        replace(
            receipt,
            target_crop_states=(replace(receipt.target_crop_states[0], page_index=1),),
        )
    with pytest.raises(ValueError, match="target CropBox must match after snapshot"):
        replace(
            receipt,
            target_crop_states=(
                replace(
                    receipt.target_crop_states[0],
                    crop_box=(41.0, 80.0, 560.0, 780.0),
                ),
            ),
        )
    with pytest.raises(ValueError, match="MediaBox must stay unchanged"):
        replace(
            receipt,
            after_snapshot=replace(
                receipt.after_snapshot,
                pages=(
                    replace(
                        after_page,
                        boxes=replace(after_page.boxes, media_box=(11.0, 20.0, 610.0, 820.0)),
                    ),
                    *receipt.after_snapshot.pages[1:],
                ),
            ),
        )
    with pytest.raises(ValueError, match="page Resources must stay unchanged"):
        replace(
            receipt,
            after_snapshot=replace(
                receipt.after_snapshot,
                pages=(
                    replace(after_page, resources_fingerprint="tampered"),
                    *receipt.after_snapshot.pages[1:],
                ),
            ),
        )
    with pytest.raises(ValueError, match="page Contents must stay unchanged"):
        replace(
            receipt,
            after_snapshot=replace(
                receipt.after_snapshot,
                pages=(
                    replace(after_page, content_fingerprint="tampered"),
                    *receipt.after_snapshot.pages[1:],
                ),
            ),
        )
    with pytest.raises(ValueError, match="page rotation must stay unchanged"):
        replace(
            receipt,
            after_snapshot=replace(
                receipt.after_snapshot,
                pages=(
                    replace(after_page, effective_rotation=90),
                    *receipt.after_snapshot.pages[1:],
                ),
            ),
        )
    with pytest.raises(ValueError, match="page annotations must stay unchanged"):
        replace(
            receipt,
            after_snapshot=replace(
                receipt.after_snapshot,
                pages=(
                    replace(
                        after_page,
                        annotations=(
                            PdfAnnotationStructureSnapshot(
                                subtype="/Text",
                                rect=(10.0, 20.0, 30.0, 40.0),
                                has_appearance=False,
                                appearance_fingerprint=None,
                                parent_state=AnnotationParentState.ABSENT,
                                fingerprint="tampered-annotation",
                            ),
                        ),
                    ),
                    *receipt.after_snapshot.pages[1:],
                ),
            ),
        )
    with pytest.raises(ValueError, match="metadata must stay unchanged"):
        replace(
            receipt,
            after_snapshot=replace(
                receipt.after_snapshot,
                metadata_fingerprint="tampered",
            ),
        )
    with pytest.raises(ValueError, match="untouched pages must remain identical"):
        replace(
            receipt,
            after_snapshot=replace(
                receipt.after_snapshot,
                pages=(
                    receipt.after_snapshot.pages[0],
                    replace(receipt.after_snapshot.pages[1], content_fingerprint="tampered"),
                ),
            ),
        )


def test_crop_receipt_rejects_invalid_transition_mapping(tmp_path: Path) -> None:
    document_path = create_crop_source_pdf(tmp_path / "crop-receipt-transition.pdf")
    service = PdfPageMutationService()
    states = service.read_crop_states(document_path, (0,))
    plan = build_page_crop_plan(states, margins=PageCropMargins(10, 20, 30, 40))
    receipt = service.crop_pages(document_path, plan).receipt

    invalid_transition = PageIndexTransition(
        old_page_count=receipt.before_snapshot.page_count,
        new_page_count=receipt.before_snapshot.page_count,
        cache_old_to_new=(0, 1),
        current_page_old_to_new=(0, 1),
    )
    with pytest.raises(ValueError, match="execute_transition cache_old_to_new is invalid"):
        replace(receipt, execute_transition=invalid_transition)


def test_crop_receipt_undo_redo_reject_working_copy_ownership_mismatch(tmp_path: Path) -> None:
    service = PdfPageMutationService()
    path_a = create_crop_source_pdf(tmp_path / "crop-owner-a.pdf")
    path_b = create_crop_source_pdf(tmp_path / "crop-owner-b.pdf")
    states = service.read_crop_states(path_a, (0,))
    plan = build_page_crop_plan(states, margins=PageCropMargins(10, 20, 30, 40))
    mutation = service.crop_pages(path_a, plan)
    before_sha_b = path_b.read_bytes()
    before_candidates = mutation_candidate_paths(path_b)

    with pytest.raises(PdfPageMutationError, match="別の作業コピー用"):
        service.undo_page_crop(path_b, mutation.receipt)
    assert path_b.read_bytes() == before_sha_b
    assert mutation_candidate_paths(path_b) == before_candidates

    service.undo_page_crop(path_a, mutation.receipt)
    with pytest.raises(PdfPageMutationError, match="別の作業コピー用"):
        service.redo_page_crop(path_b, mutation.receipt)
    assert path_b.read_bytes() == before_sha_b
    assert mutation_candidate_paths(path_b) == before_candidates


def test_crop_receipt_reset_to_mediabox_keeps_direct_crop_materialized(tmp_path: Path) -> None:
    document_path = create_crop_source_pdf(tmp_path / "crop-receipt-reset-direct.pdf")
    service = PdfPageMutationService()
    states = service.read_crop_states(document_path, (1,))
    plan = build_page_crop_plan(
        states,
        margins=PageCropMargins(0, 0, 0, 0),
        reset_to_media_box=True,
    )

    mutation = service.crop_pages(document_path, plan)

    assert mutation.receipt.after_snapshot.pages[1].boxes.crop_box_direct_present is True
    assert mutation.receipt.after_snapshot.pages[1].boxes.crop_box_inherited is False
    assert mutation.receipt.after_snapshot.pages[1].boxes.crop_box_falls_back_to_media_box is False


def test_crop_receipt_rejects_document_level_structure_drift(tmp_path: Path) -> None:
    document_path = create_crop_source_pdf(tmp_path / "crop-receipt-doc-drift.pdf")
    service = PdfPageMutationService()
    states = service.read_crop_states(document_path, (0,))
    plan = build_page_crop_plan(states, margins=PageCropMargins(10, 20, 30, 40))
    receipt = service.crop_pages(document_path, plan).receipt

    with pytest.raises(ValueError, match="outlines must stay unchanged"):
        replace(
            receipt,
            after_snapshot=replace(
                receipt.after_snapshot,
                outlines=(PdfOutlineItemSnapshot("x", None, None, ()),),
            ),
        )
    with pytest.raises(ValueError, match="named destinations must stay unchanged"):
        replace(
            receipt,
            after_snapshot=replace(
                receipt.after_snapshot,
                named_destinations=(PdfNamedDestinationSnapshot("dest", 0),),
            ),
        )
    with pytest.raises(ValueError, match="attachments must stay unchanged"):
        replace(
            receipt,
            after_snapshot=replace(receipt.after_snapshot, attachments_fingerprint="tampered"),
        )
