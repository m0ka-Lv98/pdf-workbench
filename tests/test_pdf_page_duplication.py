from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject

import pdf_workbench.services.pdf_page_mutation as mutation_module
from pdf_regression_utils import compatibility_fixture_dir, extract_pdfium_text, file_sha256
from pdf_workbench.services.pdf_page_mutation import (
    PdfPageMutationError,
    PdfPageMutationService,
)


def create_text_fixture(path: Path, pages: list[str]) -> Path:
    objects: list[bytes] = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        f"2 0 obj << /Type /Pages /Count {len(pages)} /Kids [".encode("ascii"),
    ]
    page_object_numbers = [3 + index * 2 for index in range(len(pages))]
    content_object_numbers = [4 + index * 2 for index in range(len(pages))]
    objects[1] += b" ".join(f"{number} 0 R".encode("ascii") for number in page_object_numbers)
    objects[1] += b"] >> endobj\n"
    for page_number, content_number, text in zip(
        page_object_numbers,
        content_object_numbers,
        pages,
        strict=True,
    ):
        content = f"BT /F1 18 Tf 40 100 Td ({text}) Tj ET".encode("latin-1")
        objects.append(
            f"{page_number} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Resources << /Font << /F1 100 0 R >> >> /Contents {content_number} 0 R "
            f">> endobj\n".encode("ascii")
        )
        objects.append(
            f"{content_number} 0 obj << /Length {len(content)} >> stream\n".encode("ascii")
            + content
            + b"\nendstream\nendobj\n"
        )
    objects.append(b"100 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
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


def create_outline_attachment_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    writer.add_outline_item("First", 0)
    writer.add_attachment("note.txt", b"hello world")
    writer.add_metadata({"/Title": "Duplication Demo"})
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def create_widget_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    widget = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("field1"),
            NameObject("/Rect"): ArrayObject(
                [
                    FloatObject(20),
                    FloatObject(20),
                    FloatObject(120),
                    FloatObject(40),
                ]
            ),
        }
    )
    writer.add_annotation(page_number=0, annotation=widget)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


def copy_compatibility_fixture(name: str, destination: Path) -> Path:
    shutil.copyfile(compatibility_fixture_dir() / name, destination)
    return destination


def annotation_objects(values: object) -> list[pikepdf.Object]:
    if values is None:
        return []
    if not isinstance(values, pikepdf.Array):
        raise AssertionError("annotation array was not preserved")
    return [item if isinstance(item, pikepdf.Object) else item.get_object() for item in values]


def test_duplicate_pages_insert_copies_immediately_after_sources(tmp_path: Path) -> None:
    source_path = create_text_fixture(tmp_path / "source-order.pdf", ["A", "B", "C", "D", "E"])
    working_copy_path = tmp_path / "working-order.pdf"
    shutil.copyfile(source_path, working_copy_path)
    service = PdfPageMutationService()

    mutation = service.duplicate_pages(working_copy_path, (1, 3))

    assert mutation.receipt.source_page_indexes == (1, 3)
    assert mutation.receipt.original_page_indexes_after == (1, 4)
    assert mutation.receipt.duplicate_page_indexes == (2, 5)
    assert mutation.mutation_result.page_count == 7
    assert mutation.mutation_result.page_index_transition is not None
    assert mutation.mutation_result.page_index_transition.cache_old_to_new == (0, 1, 3, 4, 6)
    assert mutation.mutation_result.page_index_transition.current_page_old_to_new == (
        0,
        2,
        3,
        5,
        6,
    )
    assert page_count(working_copy_path) == 7
    assert extract_pdfium_text(working_copy_path) == "A B B C D D E"


def test_duplicate_pages_preserve_adjacent_and_all_page_order(tmp_path: Path) -> None:
    adjacent_path = create_text_fixture(tmp_path / "adjacent.pdf", ["A", "B", "C", "D"])
    all_pages_path = create_text_fixture(tmp_path / "all-pages.pdf", ["A", "B", "C"])
    service = PdfPageMutationService()

    adjacent_mutation = service.duplicate_pages(adjacent_path, (1, 2))
    assert adjacent_mutation.receipt.original_page_indexes_after == (1, 3)
    assert adjacent_mutation.receipt.duplicate_page_indexes == (2, 4)
    assert adjacent_mutation.mutation_result.page_index_transition is not None
    assert adjacent_mutation.mutation_result.page_index_transition.cache_old_to_new == (0, 1, 3, 5)
    assert extract_pdfium_text(adjacent_path) == "A B B C C D"

    all_pages_mutation = service.duplicate_pages(all_pages_path, (0, 1, 2))
    assert all_pages_mutation.receipt.original_page_indexes_after == (0, 2, 4)
    assert all_pages_mutation.receipt.duplicate_page_indexes == (1, 3, 5)
    assert all_pages_mutation.mutation_result.page_index_transition is not None
    assert all_pages_mutation.mutation_result.page_index_transition.cache_old_to_new == (0, 2, 4)
    assert extract_pdfium_text(all_pages_path) == "A A B B C C"


def test_duplicate_pages_preserves_source_and_undo_restores_original_state(tmp_path: Path) -> None:
    source_path = create_text_fixture(tmp_path / "source-undo.pdf", ["A", "B", "C", "D", "E"])
    working_copy_path = tmp_path / "working-undo.pdf"
    shutil.copyfile(source_path, working_copy_path)
    service = PdfPageMutationService()
    source_sha_before = file_sha256(source_path)
    working_copy_sha_before = file_sha256(working_copy_path)
    snapshot_before = service._snapshot_document_structure(working_copy_path)

    mutation = service.duplicate_pages(working_copy_path, (1, 3))

    assert file_sha256(source_path) == source_sha_before
    assert file_sha256(working_copy_path) != working_copy_sha_before

    undo_result = service.undo_page_duplication(working_copy_path, mutation.receipt)

    assert service._snapshot_document_structure(working_copy_path) == snapshot_before
    assert undo_result.page_count == 5
    assert undo_result.page_index_transition is not None
    assert undo_result.page_index_transition.cache_old_to_new == (0, 1, None, 2, 3, None, 4)
    assert undo_result.page_index_transition.current_page_old_to_new == (
        0,
        1,
        1,
        2,
        3,
        3,
        4,
    )
    assert extract_pdfium_text(working_copy_path) == "A B C D E"


def test_duplicate_pages_preserve_page_boxes_and_rotation(tmp_path: Path) -> None:
    source_path = copy_compatibility_fixture("page-boxes.pdf", tmp_path / "page-boxes.pdf")
    rotations_path = copy_compatibility_fixture("rotations.pdf", tmp_path / "rotations.pdf")
    service = PdfPageMutationService()

    page_boxes_before = service._snapshot_document_structure(source_path)
    page_box_mutation = service.duplicate_pages(source_path, (0,))
    page_boxes_after = service._snapshot_document_structure(source_path)
    assert page_boxes_after.pages[0] == page_boxes_before.pages[0]
    assert page_boxes_after.pages[1] == page_boxes_before.pages[0]
    service.undo_page_duplication(source_path, page_box_mutation.receipt)

    rotations_before = service._snapshot_document_structure(rotations_path)
    rotation_mutation = service.duplicate_pages(rotations_path, (1,))
    rotations_after = service._snapshot_document_structure(rotations_path)
    assert rotations_after.pages[1] == rotations_before.pages[1]
    assert rotations_after.pages[2] == rotations_before.pages[1]
    service.undo_page_duplication(rotations_path, rotation_mutation.receipt)


def test_duplicate_pages_create_independent_page_and_annotation_objects(tmp_path: Path) -> None:
    working_copy_path = copy_compatibility_fixture("annotations.pdf", tmp_path / "annotations.pdf")
    service = PdfPageMutationService()

    mutation = service.duplicate_pages(working_copy_path, (0,))

    assert mutation.receipt.duplicate_page_indexes == (1,)
    with pikepdf.open(working_copy_path) as pdf:
        original_page = pdf.pages[0].obj
        duplicate_page = pdf.pages[1].obj
        assert original_page.objgen != duplicate_page.objgen
        original_annots = original_page.get("/Annots", None)
        duplicate_annots = duplicate_page.get("/Annots", None)
        assert original_annots is not None
        assert duplicate_annots is not None
        original_items = annotation_objects(original_annots)
        duplicate_items = annotation_objects(duplicate_annots)
        assert len(original_items) == len(duplicate_items)
        for original_annot, duplicate_annot in zip(original_items, duplicate_items, strict=True):
            assert original_annot.objgen != duplicate_annot.objgen
            duplicate_parent = duplicate_annot.get("/P", None)
            assert duplicate_parent is not None
            assert isinstance(duplicate_parent, pikepdf.Object)
            assert duplicate_parent.objgen == duplicate_page.objgen


def test_duplicate_pages_preserve_metadata_outlines_and_attachments(tmp_path: Path) -> None:
    working_copy_path = create_outline_attachment_pdf(tmp_path / "outline-attachment.pdf")
    service = PdfPageMutationService()
    snapshot_before = service._snapshot_document_structure(working_copy_path)

    mutation = service.duplicate_pages(working_copy_path, (0,))
    snapshot_after = service._snapshot_document_structure(working_copy_path)

    assert snapshot_after.metadata_fingerprint == snapshot_before.metadata_fingerprint
    assert snapshot_after.outlines_fingerprint == snapshot_before.outlines_fingerprint
    assert snapshot_after.attachments_fingerprint == snapshot_before.attachments_fingerprint

    service.undo_page_duplication(working_copy_path, mutation.receipt)
    assert service._snapshot_document_structure(working_copy_path) == snapshot_before


def test_duplicate_pages_reject_widget_annotations_without_changing_working_copy(
    tmp_path: Path,
) -> None:
    working_copy_path = create_widget_pdf(tmp_path / "widget.pdf")
    service = PdfPageMutationService()
    sha_before = file_sha256(working_copy_path)

    with pytest.raises(PdfPageMutationError, match="未対応"):
        service.duplicate_pages(working_copy_path, (0,))

    assert file_sha256(working_copy_path) == sha_before


def test_duplicate_pages_candidate_stays_in_working_copy_directory(tmp_path: Path) -> None:
    working_copy_path = create_text_fixture(tmp_path / "candidate-dir.pdf", ["A", "B"])
    service = PdfPageMutationService()
    captured: list[Path] = []
    original = service._create_candidate_path

    def capture_candidate(target_path: Path) -> Path:
        candidate_path = original(target_path)
        captured.append(candidate_path)
        return candidate_path

    service._create_candidate_path = capture_candidate  # type: ignore[method-assign]

    service.duplicate_pages(working_copy_path, (0,))

    assert captured
    assert captured[0].parent == working_copy_path.parent


def test_duplicate_pages_cleanup_failure_does_not_mask_primary_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    working_copy_path = create_text_fixture(tmp_path / "cleanup-failure.pdf", ["A", "B"])
    service = PdfPageMutationService()

    monkeypatch.setattr(
        service,
        "_validate_page_duplication_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfPageMutationError("primary failure")),
    )
    original_unlink = Path.unlink

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    try:
        with pytest.raises(PdfPageMutationError, match="primary failure"):
            service.duplicate_pages(working_copy_path, (0,))
    finally:
        monkeypatch.setattr(Path, "unlink", original_unlink)


def test_duplicate_pages_tolerate_parent_directory_fsync_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    working_copy_path = create_text_fixture(tmp_path / "parent-fsync.pdf", ["A", "B"])
    service = PdfPageMutationService()
    original_fsync = mutation_module.os.fsync
    call_count = {"value": 0}

    def flaky_fsync(fd: int) -> None:
        call_count["value"] += 1
        if call_count["value"] >= 2:
            raise OSError("directory fsync failed")
        original_fsync(fd)

    monkeypatch.setattr(mutation_module.os, "fsync", flaky_fsync)

    mutation = service.duplicate_pages(working_copy_path, (0,))

    assert mutation.receipt.duplicate_page_indexes == (1,)
    assert page_count(working_copy_path) == 3


def test_undo_page_duplication_rejects_changed_current_state(tmp_path: Path) -> None:
    working_copy_path = create_text_fixture(tmp_path / "undo-mismatch.pdf", ["A", "B", "C"])
    service = PdfPageMutationService()
    mutation = service.duplicate_pages(working_copy_path, (1,))
    changed_sha_before = file_sha256(working_copy_path)

    with pikepdf.open(working_copy_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Rotate"] = 90
        pdf.save(working_copy_path)

    changed_sha_after = file_sha256(working_copy_path)
    assert changed_sha_after != changed_sha_before

    with pytest.raises(PdfPageMutationError, match="元に戻せません"):
        service.undo_page_duplication(working_copy_path, mutation.receipt)

    assert file_sha256(working_copy_path) == changed_sha_after
