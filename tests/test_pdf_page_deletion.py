from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject

import pdf_workbench.services.pdf_page_mutation as mutation_module
from pdf_regression_utils import compatibility_fixture_dir, extract_pdfium_text, file_sha256
from pdf_test_utils import create_blank_pdf
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


def create_open_action_pdf(path: Path) -> Path:
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=200, height=200)
    writer.open_destination = writer.pages[0]
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def create_widget_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
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


def create_page_labels_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root["/PageLabels"] = pikepdf.Dictionary(
            {"/Nums": pikepdf.Array([0, pikepdf.Dictionary({"/S": pikepdf.Name("/D")})])}
        )
        pdf.save(path)
    return path


def create_tagged_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary({"/Type": pikepdf.Name("/StructTreeRoot")})
        pdf.save(path)
    return path


def create_goto_annotation_pdf(path: Path) -> Path:
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        target_page = pdf.pages[1].obj
        link = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/Annot"),
                    "/Subtype": pikepdf.Name("/Link"),
                    "/Rect": pikepdf.Array([10, 10, 60, 30]),
                    "/Dest": pikepdf.Array([target_page, pikepdf.Name("/Fit")]),
                }
            )
        )
        first_page = pdf.pages[0].obj
        annots = first_page.get("/Annots", None)
        if annots is None:
            first_page["/Annots"] = pikepdf.Array([link])
        else:
            annots_array = annots if isinstance(annots, pikepdf.Array) else annots.get_object()
            annots_array.append(link)
        pdf.save(path)
    return path


def create_square_annotation_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Square"),
            NameObject("/Rect"): ArrayObject(
                [FloatObject(20), FloatObject(20), FloatObject(80), FloatObject(80)]
            ),
        }
    )
    writer.add_annotation(page_number=1, annotation=annotation)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def copy_compatibility_fixture(name: str, destination: Path) -> Path:
    shutil.copyfile(compatibility_fixture_dir() / name, destination)
    return destination


def page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


def test_delete_pages_removes_selected_pages_in_order_and_builds_transition(tmp_path: Path) -> None:
    working_copy = create_text_fixture(tmp_path / "delete-order.pdf", ["A", "B", "C", "D", "E"])
    service = PdfPageMutationService()

    mutation = service.delete_pages(working_copy, (1, 3))

    assert mutation.receipt.deleted_page_indexes == (1, 3)
    assert mutation.receipt.survivor_original_indexes == (0, 2, 4)
    assert mutation.mutation_result.page_count == 3
    assert mutation.mutation_result.page_index_transition is not None
    assert mutation.mutation_result.page_index_transition.cache_old_to_new == (
        0,
        None,
        1,
        None,
        2,
    )
    assert mutation.mutation_result.page_index_transition.current_page_old_to_new == (
        0,
        1,
        1,
        2,
        2,
    )
    assert extract_pdfium_text(working_copy) == "A C E"


@pytest.mark.parametrize(
    ("deleted_indexes", "expected_text", "expected_mapping"),
    [
        ((1, 2), "A D", (0, None, None, 1)),
        ((0,), "B C D E", (None, 0, 1, 2, 3)),
        ((4,), "A B C D", (0, 1, 2, 3, None)),
        ((0, 1, 2), "D", (None, None, None, 0)),
    ],
)
def test_delete_pages_supports_adjacent_edge_and_all_but_one_cases(
    tmp_path: Path,
    deleted_indexes: tuple[int, ...],
    expected_text: str,
    expected_mapping: tuple[int | None, ...],
) -> None:
    base_pages = ["A", "B", "C", "D", "E"] if len(expected_mapping) == 5 else ["A", "B", "C", "D"]
    working_copy = create_text_fixture(tmp_path / f"{deleted_indexes}.pdf", base_pages)

    mutation = PdfPageMutationService().delete_pages(working_copy, deleted_indexes)

    assert mutation.mutation_result.page_index_transition is not None
    assert mutation.mutation_result.page_index_transition.cache_old_to_new == expected_mapping
    assert extract_pdfium_text(working_copy) == expected_text


def test_delete_pages_preserves_source_and_undo_redo_round_trip(tmp_path: Path) -> None:
    source_path = create_text_fixture(tmp_path / "source.pdf", ["A", "B", "C", "D", "E"])
    working_copy = tmp_path / "working.pdf"
    shutil.copyfile(source_path, working_copy)
    service = PdfPageMutationService()
    source_sha_before = file_sha256(source_path)
    working_sha_before = file_sha256(working_copy)

    mutation = service.delete_pages(working_copy, (1, 3))
    working_sha_after = file_sha256(working_copy)
    assert working_sha_after != working_sha_before
    assert extract_pdfium_text(working_copy) == "A C E"
    assert file_sha256(source_path) == source_sha_before

    undo_result = service.undo_page_deletion(working_copy, mutation.receipt)
    assert undo_result.page_count == 5
    assert file_sha256(working_copy) == working_sha_before

    redo_result = service.redo_page_deletion(working_copy, mutation.receipt)
    assert redo_result.page_count == 3
    assert extract_pdfium_text(working_copy) == "A C E"
    assert file_sha256(source_path) == source_sha_before


def test_delete_pages_creates_disk_backed_undo_snapshot_and_discard_removes_it(
    tmp_path: Path,
) -> None:
    working_copy = create_blank_pdf(tmp_path / "delete-snapshot.pdf", 3)
    service = PdfPageMutationService()

    mutation = service.delete_pages(working_copy, (1,))

    snapshot_path = mutation.receipt.undo_snapshot_path
    assert snapshot_path.parent == working_copy.parent
    assert snapshot_path.is_absolute()
    assert snapshot_path.exists()
    assert mutation.receipt.undo_snapshot_sha256 == file_sha256(snapshot_path)

    service.undo_page_deletion(working_copy, mutation.receipt)
    assert snapshot_path.exists()

    service.discard_page_deletion_receipt(mutation.receipt)
    assert not snapshot_path.exists()


def test_delete_pages_attempts_hard_link_before_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    working_copy = create_blank_pdf(tmp_path / "delete-link.pdf", 3)
    service = PdfPageMutationService()
    called = False
    real_link = mutation_module.os.link

    def tracking_link(
        source: Path | str, target: Path | str, *args: object, **kwargs: object
    ) -> None:
        nonlocal called
        called = True
        real_link(source, target, *args, **kwargs)

    monkeypatch.setattr(mutation_module.os, "link", tracking_link)

    mutation = service.delete_pages(working_copy, (1,))

    assert called is True
    service.discard_page_deletion_receipt(mutation.receipt)


def test_delete_pages_falls_back_to_copy_when_hard_link_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_copy = create_text_fixture(tmp_path / "delete-copy-fallback.pdf", ["A", "B", "C"])
    service = PdfPageMutationService()

    def fail_link(_source: Path | str, _target: Path | str) -> None:
        raise OSError("link unavailable")

    monkeypatch.setattr(mutation_module.os, "link", fail_link)

    mutation = service.delete_pages(working_copy, (1,))

    assert mutation.receipt.undo_snapshot_path.exists()
    assert extract_pdfium_text(working_copy) == "A C"


def test_delete_pages_rejects_all_pages(tmp_path: Path) -> None:
    working_copy = create_blank_pdf(tmp_path / "delete-all-pages.pdf", 2)

    with pytest.raises(PdfPageMutationError, match="少なくとも1ページは残す必要があります"):
        PdfPageMutationService().delete_pages(working_copy, (0, 1))

    assert page_count(working_copy) == 2


def test_delete_pages_preserves_rotations_and_page_boxes_for_survivors(tmp_path: Path) -> None:
    rotations_path = copy_compatibility_fixture("rotations.pdf", tmp_path / "rotations.pdf")
    page_boxes_path = copy_compatibility_fixture("page-boxes.pdf", tmp_path / "page-boxes.pdf")
    service = PdfPageMutationService()

    rotations_before = service._snapshot_document_structure(rotations_path)
    page_boxes_before = service._snapshot_document_structure(page_boxes_path)

    rotations_mutation = service.delete_pages(rotations_path, (1,))
    page_boxes_mutation = service.delete_pages(page_boxes_path, (0,))

    rotations_after = service._snapshot_document_structure(rotations_path)
    page_boxes_after = service._snapshot_document_structure(page_boxes_path)

    assert rotations_after.pages[0] == rotations_before.pages[0]
    assert rotations_after.pages[1] == rotations_before.pages[2]
    assert page_boxes_after.pages[0] == page_boxes_before.pages[1]
    service.undo_page_deletion(rotations_path, rotations_mutation.receipt)
    service.undo_page_deletion(page_boxes_path, page_boxes_mutation.receipt)
    assert service._snapshot_document_structure(rotations_path) == rotations_before
    assert service._snapshot_document_structure(page_boxes_path) == page_boxes_before


def test_delete_pages_preserves_annotations_and_maps_surviving_outline_targets(
    tmp_path: Path,
) -> None:
    annotations_path = create_square_annotation_pdf(tmp_path / "annotations.pdf")
    outline_path = create_outline_mapping_pdf(tmp_path / "outline.pdf")
    service = PdfPageMutationService()

    annotations_before = service._snapshot_document_structure(annotations_path)
    outline_before = service._snapshot_document_structure(outline_path)

    annotations_mutation = service.delete_pages(annotations_path, (0,))
    outline_mutation = service.delete_pages(outline_path, (1,))

    annotations_after = service._snapshot_document_structure(annotations_path)
    outline_after = service._snapshot_document_structure(outline_path)

    assert annotations_after.pages[0] == annotations_before.pages[1]
    assert tuple(item.destination_page_index for item in outline_after.outlines) == (0, 1)
    assert outline_after.outlines[0].children[0].destination_page_index == 0
    assert tuple(item.destination_page_index for item in outline_after.named_destinations) == (1,)
    service.undo_page_deletion(annotations_path, annotations_mutation.receipt)
    service.undo_page_deletion(outline_path, outline_mutation.receipt)
    assert service._snapshot_document_structure(outline_path) == outline_before


def test_delete_pages_rejects_deleted_outline_destination(tmp_path: Path) -> None:
    outline_path = create_outline_mapping_pdf(tmp_path / "outline-deleted-target.pdf")
    before_sha = file_sha256(outline_path)

    with pytest.raises(PdfPageMutationError):
        PdfPageMutationService().delete_pages(outline_path, (2,))

    assert file_sha256(outline_path) == before_sha


@pytest.mark.parametrize(
    ("builder", "message"),
    [
        (create_open_action_pdf, "OpenAction"),
        (create_widget_pdf, "Widget"),
        (create_page_labels_pdf, "PageLabels"),
        (create_tagged_pdf, "タグ付きPDF"),
        (create_goto_annotation_pdf, "内部宛先注釈"),
    ],
)
def test_delete_pages_rejects_unsupported_structures(
    tmp_path: Path,
    builder: callable,
    message: str,
) -> None:
    path = builder(tmp_path / f"{message}.pdf")
    before_sha = file_sha256(path)

    with pytest.raises(PdfPageMutationError, match=message):
        PdfPageMutationService().delete_pages(path, (0,))

    assert file_sha256(path) == before_sha
