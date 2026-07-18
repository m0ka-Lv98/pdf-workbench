from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject

import pdf_workbench.services.pdf_page_mutation as mutation_module
from pdf_regression_utils import (
    compatibility_fixture_dir,
    extract_pdfium_text,
    file_sha256,
    render_pdf_pages,
)
from pdf_workbench.services.pdf_page_mutation import (
    AnnotationParentState,
    PageDuplicationReceipt,
    PdfAnnotationStructureSnapshot,
    PdfNamedDestinationSnapshot,
    PdfOutlineItemSnapshot,
    PdfPageMutationError,
    PdfPageMutationService,
)


def create_text_fixture(path: Path, pages: list[str]) -> Path:
    objects: dict[int, bytes] = {
        1: b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        2: f"2 0 obj << /Type /Pages /Count {len(pages)} /Kids [".encode("ascii"),
    }
    page_object_numbers = [3 + index * 2 for index in range(len(pages))]
    content_object_numbers = [4 + index * 2 for index in range(len(pages))]
    objects[2] += b" ".join(f"{number} 0 R".encode("ascii") for number in page_object_numbers)
    objects[2] += b"] >> endobj\n"
    for page_number, content_number, text in zip(
        page_object_numbers,
        content_object_numbers,
        pages,
        strict=True,
    ):
        content = f"BT /F1 18 Tf 40 100 Td ({text}) Tj ET".encode("latin-1")
        objects[page_number] = (
            f"{page_number} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Resources << /Font << /F1 100 0 R >> >> /Contents {content_number} 0 R "
            f">> endobj\n".encode("ascii")
        )
        objects[content_number] = (
            f"{content_number} 0 obj << /Length {len(content)} >> stream\n".encode("ascii")
            + content
            + b"\nendstream\nendobj\n"
        )
    objects[100] = b"100 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    max_object_number = max(objects)
    for object_number in range(1, max_object_number + 1):
        offsets.append(len(pdf))
        pdf.extend(
            objects.get(
                object_number,
                f"{object_number} 0 obj << >> endobj\n".encode("ascii"),
            )
        )
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


def create_outline_mapping_pdf(path: Path) -> Path:
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=200, height=200)
    parent = writer.add_outline_item("A", 0)
    writer.add_outline_item("A child", 0, parent=parent)
    writer.add_outline_item("B", 2)
    writer.add_named_destination("Later", 2)
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


def create_manual_annotation_pdf(
    path: Path,
    *,
    annotation_object_number: int,
    reverse_annotation_key_order: bool = False,
    rect: tuple[int, int, int, int] = (20, 20, 40, 40),
) -> Path:
    annots_object_number = annotation_object_number - 1
    if annots_object_number <= 3:
        raise ValueError("annotation_object_number must be greater than 4")
    objects: dict[int, bytes] = {
        1: b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        2: b"2 0 obj << /Type /Pages /Count 1 /Kids [3 0 R] >> endobj\n",
        3: (
            f"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Annots {annots_object_number} 0 R >> endobj\n".encode("ascii")
        ),
        annots_object_number: (
            f"{annots_object_number} 0 obj [ {annotation_object_number} 0 R ] endobj\n".encode(
                "ascii"
            )
        ),
    }
    annotation_items = [
        b"/Type /Annot",
        b"/Subtype /Square",
        f"/Rect [{rect[0]} {rect[1]} {rect[2]} {rect[3]}]".encode("ascii"),
    ]
    if reverse_annotation_key_order:
        annotation_items = list(reversed(annotation_items))
    objects[annotation_object_number] = (
        f"{annotation_object_number} 0 obj << ".encode("ascii")
        + b" ".join(annotation_items)
        + b" >> endobj\n"
    )
    max_object_number = annotation_object_number
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_number in range(1, max_object_number + 1):
        offsets.append(len(pdf))
        pdf.extend(
            objects.get(
                object_number,
                f"{object_number} 0 obj << >> endobj\n".encode("ascii"),
            )
        )
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


def add_normal_appearance_to_first_annotation(path: Path) -> None:
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        first_page = pdf.pages[0].obj
        annot = annotation_objects(first_page.get("/Annots", None))[0]
        appearance_stream = pdf.make_stream(b"q 0 0 20 20 re S Q")
        appearance_stream["/Type"] = pikepdf.Name("/XObject")
        appearance_stream["/Subtype"] = pikepdf.Name("/Form")
        appearance_stream["/BBox"] = pikepdf.Array([0, 0, 20, 20])
        annot["/AP"] = pikepdf.Dictionary({"/N": appearance_stream})
        pdf.save(path)


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


def outline_summary(
    items: tuple[PdfOutlineItemSnapshot, ...],
) -> tuple[tuple[str, int | None, tuple[object, ...]], ...]:
    return tuple(
        (
            item.title,
            item.destination_page_index,
            outline_summary(item.children),
        )
        for item in items
    )


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
    add_normal_appearance_to_first_annotation(working_copy_path)
    service = PdfPageMutationService()
    before_snapshot = service._snapshot_document_structure(working_copy_path)

    mutation = service.duplicate_pages(working_copy_path, (0,))
    after_snapshot = service._snapshot_document_structure(working_copy_path)

    assert mutation.receipt.duplicate_page_indexes == (1,)
    assert before_snapshot.pages[0].annotations
    assert after_snapshot.pages[1].annotations
    assert after_snapshot.pages[1].annotations == tuple(
        PdfAnnotationStructureSnapshot(
            subtype=annotation.subtype,
            rect=annotation.rect,
            has_appearance=annotation.has_appearance,
            appearance_fingerprint=annotation.appearance_fingerprint,
            parent_state=AnnotationParentState.POINTS_TO_OWN_PAGE,
            fingerprint=annotation.fingerprint,
        )
        for annotation in before_snapshot.pages[0].annotations
    )
    with pikepdf.open(working_copy_path) as pdf:
        original_page = pdf.pages[0].obj
        duplicate_page = pdf.pages[1].obj
        assert original_page.objgen != duplicate_page.objgen
        original_annots = original_page.get("/Annots", None)
        duplicate_annots = duplicate_page.get("/Annots", None)
        assert original_annots is not None
        assert duplicate_annots is not None
        assert getattr(duplicate_annots, "objgen", None) == (0, 0)
        original_items = annotation_objects(original_annots)
        duplicate_items = annotation_objects(duplicate_annots)
        assert len(original_items) == len(duplicate_items)
        assert before_snapshot.pages[0].annotations[0].appearance_fingerprint is not None
        for original_annot, duplicate_annot in zip(original_items, duplicate_items, strict=True):
            assert original_annot.objgen != duplicate_annot.objgen
            original_rect_before = tuple(float(value) for value in original_annot["/Rect"])
            duplicate_rect_before = tuple(float(value) for value in duplicate_annot["/Rect"])
            duplicate_annot[NameObject("/Rect")] = pikepdf.Array([80, 80, 120, 120])
            assert tuple(float(value) for value in original_annot["/Rect"]) == original_rect_before
            original_annot[NameObject("/Rect")] = pikepdf.Array([20, 20, 50, 50])
            assert tuple(float(value) for value in duplicate_annot["/Rect"]) == (
                80.0,
                80.0,
                120.0,
                120.0,
            )
            assert duplicate_rect_before != tuple(
                float(value) for value in duplicate_annot["/Rect"]
            )
            assert tuple(float(value) for value in original_annot["/Rect"]) == (
                20.0,
                20.0,
                50.0,
                50.0,
            )
            duplicate_parent = duplicate_annot.get("/P", None)
            assert duplicate_parent is not None
            assert isinstance(duplicate_parent, pikepdf.Object)
            assert duplicate_parent.objgen == duplicate_page.objgen
            assert service._appearance_fingerprint(original_annot) == (
                service._appearance_fingerprint(duplicate_annot)
            )
            assert str(original_annot.get("/Subtype", "")) == str(
                duplicate_annot.get("/Subtype", "")
            )
        original_count = len(original_items)
        duplicate_count = len(duplicate_items)
        del original_annots[-1]
        assert len(annotation_objects(original_page.get("/Annots", None))) == original_count - 1
        assert len(annotation_objects(duplicate_page.get("/Annots", None))) == duplicate_count
        del duplicate_annots[-1]
        assert len(annotation_objects(original_page.get("/Annots", None))) == original_count - 1
        assert len(annotation_objects(duplicate_page.get("/Annots", None))) == duplicate_count - 1


def test_duplicate_pages_annotation_round_trip_preserves_rendering_and_source_fixture(
    tmp_path: Path,
) -> None:
    source_path = compatibility_fixture_dir() / "annotations.pdf"
    working_copy_path = copy_compatibility_fixture(
        "annotations.pdf",
        tmp_path / "annotations-round-trip.pdf",
    )
    add_normal_appearance_to_first_annotation(working_copy_path)
    service = PdfPageMutationService()
    source_sha_before = file_sha256(source_path)
    before_snapshot = service._snapshot_document_structure(working_copy_path)

    mutation = service.duplicate_pages(working_copy_path, (0,))
    execute_snapshot = service._snapshot_document_structure(working_copy_path)
    execute_images = render_pdf_pages(working_copy_path, scale=0.4)

    assert len(execute_images) == 2
    assert all(image.width > 0 and image.height > 0 for image in execute_images)
    assert execute_snapshot.pages[1].annotations
    assert execute_snapshot.pages[1].annotations[0].appearance_fingerprint == (
        before_snapshot.pages[0].annotations[0].appearance_fingerprint
    )
    assert file_sha256(source_path) == source_sha_before

    undo_result = service.undo_page_duplication(working_copy_path, mutation.receipt)
    undo_images = render_pdf_pages(working_copy_path, scale=0.4)

    assert undo_result.page_count == 1
    assert service._snapshot_document_structure(working_copy_path) == before_snapshot
    assert len(undo_images) == 1
    assert all(image.width > 0 and image.height > 0 for image in undo_images)
    assert file_sha256(source_path) == source_sha_before

    service.validate_duplication_redo_precondition(working_copy_path, mutation.receipt)
    redo_mutation = service.duplicate_pages(
        working_copy_path,
        (0,),
        expected_before_snapshot=mutation.receipt.before_snapshot,
    )
    redo_snapshot = service._snapshot_document_structure(working_copy_path)
    redo_images = render_pdf_pages(working_copy_path, scale=0.4)

    assert redo_mutation.receipt.duplicate_page_indexes == (1,)
    assert redo_snapshot == execute_snapshot
    assert len(redo_images) == 2
    assert all(image.width > 0 and image.height > 0 for image in redo_images)
    assert [image.tobytes() for image in redo_images] == [
        image.tobytes() for image in execute_images
    ]
    with pikepdf.open(working_copy_path) as pdf:
        assert len(pdf.pages) == 2
    assert file_sha256(source_path) == source_sha_before


def test_duplicate_pages_preserve_metadata_outlines_and_attachments(tmp_path: Path) -> None:
    working_copy_path = create_outline_attachment_pdf(tmp_path / "outline-attachment.pdf")
    service = PdfPageMutationService()
    snapshot_before = service._snapshot_document_structure(working_copy_path)

    mutation = service.duplicate_pages(working_copy_path, (0,))
    snapshot_after = service._snapshot_document_structure(working_copy_path)

    assert snapshot_after.metadata_fingerprint == snapshot_before.metadata_fingerprint
    assert snapshot_after.outlines == snapshot_before.outlines
    assert snapshot_after.named_destinations == snapshot_before.named_destinations
    assert snapshot_after.attachments_fingerprint == snapshot_before.attachments_fingerprint

    service.undo_page_duplication(working_copy_path, mutation.receipt)
    assert service._snapshot_document_structure(working_copy_path) == snapshot_before


def test_duplicate_pages_map_later_outline_destinations_and_named_destinations(
    tmp_path: Path,
) -> None:
    working_copy_path = create_outline_mapping_pdf(tmp_path / "outline-mapping.pdf")
    service = PdfPageMutationService()
    before_snapshot = service._snapshot_document_structure(working_copy_path)

    assert outline_summary(before_snapshot.outlines) == (
        ("A", 0, (("A child", 0, ()),)),
        ("B", 2, ()),
    )
    assert before_snapshot.named_destinations == (PdfNamedDestinationSnapshot("Later", 2),)

    mutation = service.duplicate_pages(working_copy_path, (0,))
    after_snapshot = service._snapshot_document_structure(working_copy_path)

    assert outline_summary(after_snapshot.outlines) == (
        ("A", 0, (("A child", 0, ()),)),
        ("B", 3, ()),
    )
    assert after_snapshot.named_destinations == (PdfNamedDestinationSnapshot("Later", 3),)
    assert mutation.receipt.duplicate_page_indexes == (1,)

    service.undo_page_duplication(working_copy_path, mutation.receipt)
    assert service._snapshot_document_structure(working_copy_path) == before_snapshot

    redo_mutation = service.duplicate_pages(working_copy_path, (0,))
    redo_snapshot = service._snapshot_document_structure(working_copy_path)
    assert redo_mutation.receipt.duplicate_page_indexes == mutation.receipt.duplicate_page_indexes
    assert outline_summary(redo_snapshot.outlines) == (
        ("A", 0, (("A child", 0, ()),)),
        ("B", 3, ()),
    )
    assert redo_snapshot.named_destinations == (PdfNamedDestinationSnapshot("Later", 3),)


def test_duplicate_pages_reject_widget_annotations_without_changing_working_copy(
    tmp_path: Path,
) -> None:
    working_copy_path = create_widget_pdf(tmp_path / "widget.pdf")
    service = PdfPageMutationService()
    sha_before = file_sha256(working_copy_path)

    with pytest.raises(PdfPageMutationError, match="未対応"):
        service.duplicate_pages(working_copy_path, (0,))

    assert file_sha256(working_copy_path) == sha_before


def test_duplicate_pages_reject_outline_parse_failure_without_changing_working_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    working_copy_path = create_outline_attachment_pdf(tmp_path / "outline-fail-closed.pdf")
    service = PdfPageMutationService()
    sha_before = file_sha256(working_copy_path)

    def raise_outline(_self: PdfReader) -> object:
        raise RuntimeError("broken outline")

    monkeypatch.setattr(mutation_module.PdfReader, "outline", property(raise_outline))

    with pytest.raises(PdfPageMutationError, match="アウトライン"):
        service.duplicate_pages(working_copy_path, (0,))

    assert file_sha256(working_copy_path) == sha_before


def test_duplicate_pages_reject_metadata_parse_failure_without_changing_working_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    working_copy_path = create_outline_attachment_pdf(tmp_path / "metadata-fail-closed.pdf")
    service = PdfPageMutationService()
    sha_before = file_sha256(working_copy_path)

    def raise_metadata(_self: PdfReader) -> object:
        raise RuntimeError("broken metadata")

    monkeypatch.setattr(mutation_module.PdfReader, "metadata", property(raise_metadata))

    with pytest.raises(PdfPageMutationError, match="メタデータ"):
        service.duplicate_pages(working_copy_path, (0,))

    assert file_sha256(working_copy_path) == sha_before


def test_duplicate_pages_reject_attachment_parse_failure_without_changing_working_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    working_copy_path = create_outline_attachment_pdf(tmp_path / "attachment-fail-closed.pdf")
    service = PdfPageMutationService()
    sha_before = file_sha256(working_copy_path)

    def raise_attachments(_self: PdfReader) -> object:
        raise RuntimeError("broken attachments")

    monkeypatch.setattr(mutation_module.PdfReader, "attachments", property(raise_attachments))

    with pytest.raises(PdfPageMutationError, match="添付ファイル"):
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


def test_duplicate_pages_close_source_streams_before_replace_for_execute_and_undo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    working_copy_path = create_text_fixture(tmp_path / "stream-lifetime.pdf", ["A", "B", "C"])
    resolved_working_copy_path = working_copy_path.resolve()
    service = PdfPageMutationService()
    tracked_streams: list[object] = []
    original_path_open = Path.open
    original_replace = service._replace_atomically

    def tracking_open(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        stream = original_path_open(self, mode, *args, **kwargs)
        if self.expanduser().resolve() == resolved_working_copy_path and mode == "rb":
            tracked_streams.append(stream)
        return stream

    def assert_closed_then_replace(source_path: Path, destination_path: Path) -> None:
        assert tracked_streams
        assert all(getattr(stream, "closed", False) for stream in tracked_streams)
        original_replace(source_path, destination_path)

    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(service, "_replace_atomically", assert_closed_then_replace)

    mutation = service.duplicate_pages(working_copy_path, (1,))
    assert tracked_streams
    assert all(getattr(stream, "closed", False) for stream in tracked_streams)

    tracked_streams.clear()
    service.undo_page_duplication(working_copy_path, mutation.receipt)
    assert tracked_streams
    assert all(getattr(stream, "closed", False) for stream in tracked_streams)


def test_duplicate_pages_preserve_annotation_parent_semantics(
    tmp_path: Path,
) -> None:
    working_copy_path = copy_compatibility_fixture(
        "annotations.pdf",
        tmp_path / "annots-parent.pdf",
    )
    service = PdfPageMutationService()
    before_snapshot = service._snapshot_document_structure(working_copy_path)

    assert before_snapshot.pages[0].annotations
    assert all(
        annotation.parent_state is AnnotationParentState.ABSENT
        for annotation in before_snapshot.pages[0].annotations
    )

    mutation = service.duplicate_pages(working_copy_path, (0,))

    with pikepdf.open(working_copy_path) as pdf:
        original_page = pdf.pages[0].obj
        duplicate_page = pdf.pages[1].obj
        original_items = annotation_objects(original_page.get("/Annots", None))
        duplicate_items = annotation_objects(duplicate_page.get("/Annots", None))
        for original_annot, duplicate_annot in zip(original_items, duplicate_items, strict=True):
            original_parent = original_annot.get("/P", None)
            duplicate_parent = duplicate_annot.get("/P", None)
            assert original_parent is None
            assert isinstance(duplicate_parent, pikepdf.Object)
            assert duplicate_parent.objgen == duplicate_page.objgen

    service.undo_page_duplication(working_copy_path, mutation.receipt)
    restored_snapshot = service._snapshot_document_structure(working_copy_path)
    assert restored_snapshot == before_snapshot


def test_duplicate_pages_reject_tampered_annotation_parent_candidate_without_changing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    working_copy_path = copy_compatibility_fixture(
        "annotations.pdf",
        tmp_path / "annots-tamper.pdf",
    )
    service = PdfPageMutationService()
    sha_before = file_sha256(working_copy_path)
    original_write_candidate = service._write_candidate

    def tampering_write_candidate(writer: PdfWriter, candidate_path: Path) -> None:
        original_write_candidate(writer, candidate_path)
        with pikepdf.open(candidate_path, allow_overwriting_input=True) as pdf:
            duplicate_page = pdf.pages[1].obj
            original_page = pdf.pages[0].obj
            duplicate_annot = annotation_objects(duplicate_page.get("/Annots", None))[0]
            duplicate_annot[NameObject("/P")] = original_page
            pdf.save(candidate_path)

    monkeypatch.setattr(service, "_write_candidate", tampering_write_candidate)

    with pytest.raises(PdfPageMutationError, match=r"/P|構造検証|検証"):
        service.duplicate_pages(working_copy_path, (0,))

    assert file_sha256(working_copy_path) == sha_before


def test_duplicate_pages_page_boxes_are_independent_after_mutation(tmp_path: Path) -> None:
    working_copy_path = copy_compatibility_fixture("page-boxes.pdf", tmp_path / "box-share.pdf")
    service = PdfPageMutationService()
    service.duplicate_pages(working_copy_path, (0,))

    with pikepdf.open(working_copy_path, allow_overwriting_input=True) as pdf:
        original_page = pdf.pages[0]
        duplicate_page = pdf.pages[1]
        original_crop_before = tuple(float(value) for value in original_page.obj["/CropBox"])
        duplicate_crop_before = tuple(float(value) for value in duplicate_page.obj["/CropBox"])
        original_media_before = tuple(float(value) for value in original_page.obj["/MediaBox"])
        duplicate_media_before = tuple(float(value) for value in duplicate_page.obj["/MediaBox"])
        duplicate_page.obj["/CropBox"] = pikepdf.Array([40, 80, 560, 700])
        original_page.obj["/MediaBox"] = pikepdf.Array([10, 10, 600, 760])
        pdf.save(working_copy_path)

    with pikepdf.open(working_copy_path) as pdf:
        original_page = pdf.pages[0]
        duplicate_page = pdf.pages[1]
        assert (
            tuple(float(value) for value in original_page.obj["/CropBox"]) == original_crop_before
        )
        assert (
            tuple(float(value) for value in duplicate_page.obj["/CropBox"]) != duplicate_crop_before
        )
        assert (
            tuple(float(value) for value in duplicate_page.obj["/MediaBox"])
            == duplicate_media_before
        )
        assert (
            tuple(float(value) for value in original_page.obj["/MediaBox"]) != original_media_before
        )


def test_page_duplication_receipt_rejects_malformed_indexes(tmp_path: Path) -> None:
    service = PdfPageMutationService()
    working_copy_path = create_text_fixture(tmp_path / "receipt-source.pdf", ["A", "B", "C"])
    before_snapshot = service._snapshot_document_structure(working_copy_path)

    with pytest.raises(ValueError, match="sorted"):
        PageDuplicationReceipt(
            original_page_count=3,
            source_page_indexes=(1, 0),
            original_page_indexes_after=(1, 0),
            duplicate_page_indexes=(2, 1),
            before_snapshot=before_snapshot,
        )
    with pytest.raises(ValueError, match="expected mapping"):
        PageDuplicationReceipt(
            original_page_count=3,
            source_page_indexes=(0, 2),
            original_page_indexes_after=(1, 3),
            duplicate_page_indexes=(2, 4),
            before_snapshot=before_snapshot,
        )


def test_duplicate_pages_service_normalizes_unsorted_unique_input_and_rejects_invalid_types(
    tmp_path: Path,
) -> None:
    working_copy_path = create_text_fixture(
        tmp_path / "normalize-service.pdf",
        ["A", "B", "C", "D"],
    )
    service = PdfPageMutationService()

    mutation = service.duplicate_pages(working_copy_path, (3, 1, 1))
    assert mutation.receipt.source_page_indexes == (1, 3)
    assert mutation.receipt.duplicate_page_indexes == (2, 5)

    with pytest.raises(TypeError, match="integers"):
        service.duplicate_pages(working_copy_path, (True,))  # type: ignore[arg-type]


def test_object_fingerprint_ignores_object_numbers_and_dictionary_order(tmp_path: Path) -> None:
    first_path = create_manual_annotation_pdf(
        tmp_path / "fingerprint-a.pdf",
        annotation_object_number=6,
    )
    second_path = create_manual_annotation_pdf(
        tmp_path / "fingerprint-b.pdf",
        annotation_object_number=16,
        reverse_annotation_key_order=True,
    )
    changed_path = create_manual_annotation_pdf(
        tmp_path / "fingerprint-c.pdf",
        annotation_object_number=26,
        rect=(20, 20, 60, 60),
    )
    service = PdfPageMutationService()

    first_snapshot = service._snapshot_document_structure(first_path)
    second_snapshot = service._snapshot_document_structure(second_path)
    changed_snapshot = service._snapshot_document_structure(changed_path)

    assert first_snapshot.pages[0].annotations[0].fingerprint == (
        second_snapshot.pages[0].annotations[0].fingerprint
    )
    assert first_snapshot.pages[0].annotations[0].fingerprint != (
        changed_snapshot.pages[0].annotations[0].fingerprint
    )


def test_object_fingerprint_handles_cycles_without_recursion() -> None:
    service = PdfPageMutationService()
    cyclic = pikepdf.Dictionary()
    cyclic["/Self"] = cyclic

    fingerprint = service._object_fingerprint(cyclic)

    assert isinstance(fingerprint, str)
    assert len(fingerprint) == 64


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
