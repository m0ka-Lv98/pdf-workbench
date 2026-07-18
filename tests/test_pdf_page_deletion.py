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
    PageDeletionReceipt,
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


def delete_undo_snapshots(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.stem}.delete-undo.*.pdf"))


def delete_candidates(path: Path) -> list[Path]:
    return list(path.parent.glob(f".{path.stem}.mutation.*.tmp.pdf"))


def add_unused_font_resource(path: Path, page_index: int) -> None:
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        font_dict = pdf.pages[page_index].obj["/Resources"]["/Font"]
        font_dict["/F_unused"] = pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Font"),
                "/Subtype": pikepdf.Name("/Type1"),
                "/BaseFont": pikepdf.Name("/Courier"),
            }
        )
        pdf.save(path)


def clone_delete_receipt(
    receipt: PageDeletionReceipt,
    **overrides: object,
) -> PageDeletionReceipt:
    return PageDeletionReceipt(
        working_copy_path=overrides.get("working_copy_path", receipt.working_copy_path),
        original_page_count=receipt.original_page_count,
        original_current_page_index=overrides.get(
            "original_current_page_index",
            receipt.original_current_page_index,
        ),
        deleted_page_indexes=overrides.get("deleted_page_indexes", receipt.deleted_page_indexes),
        survivor_original_indexes=overrides.get(
            "survivor_original_indexes",
            receipt.survivor_original_indexes,
        ),
        before_snapshot=overrides.get("before_snapshot", receipt.before_snapshot),
        after_snapshot=overrides.get("after_snapshot", receipt.after_snapshot),
        undo_snapshot_path=overrides.get("undo_snapshot_path", receipt.undo_snapshot_path),
        undo_snapshot_sha256=overrides.get("undo_snapshot_sha256", receipt.undo_snapshot_sha256),
    )


def test_delete_pages_removes_selected_pages_in_order_and_builds_transition(tmp_path: Path) -> None:
    working_copy = create_text_fixture(tmp_path / "delete-order.pdf", ["A", "B", "C", "D", "E"])
    service = PdfPageMutationService()

    mutation = service.delete_pages(working_copy, (1, 3), current_page_index=0)

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

    mutation = PdfPageMutationService().delete_pages(
        working_copy,
        deleted_indexes,
        current_page_index=0,
    )

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

    mutation = service.delete_pages(working_copy, (1, 3), current_page_index=0)
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

    mutation = service.delete_pages(working_copy, (1,), current_page_index=1)

    snapshot_path = mutation.receipt.undo_snapshot_path
    assert snapshot_path.parent == working_copy.parent
    assert snapshot_path.is_absolute()
    assert snapshot_path.exists()
    assert mutation.receipt.undo_snapshot_sha256 == file_sha256(snapshot_path)

    service.undo_page_deletion(working_copy, mutation.receipt)
    assert snapshot_path.exists()

    service.discard_page_deletion_receipt(working_copy, mutation.receipt)
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

    mutation = service.delete_pages(working_copy, (1,), current_page_index=1)

    assert called is True
    service.discard_page_deletion_receipt(working_copy, mutation.receipt)


def test_delete_pages_falls_back_to_copy_when_hard_link_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    working_copy = create_text_fixture(tmp_path / "delete-copy-fallback.pdf", ["A", "B", "C"])
    service = PdfPageMutationService()

    def fail_link(_source: Path | str, _target: Path | str) -> None:
        raise OSError("link unavailable")

    monkeypatch.setattr(mutation_module.os, "link", fail_link)

    mutation = service.delete_pages(working_copy, (1,), current_page_index=1)

    assert mutation.receipt.undo_snapshot_path.exists()
    assert extract_pdfium_text(working_copy) == "A C"


def test_delete_pages_rejects_all_pages(tmp_path: Path) -> None:
    working_copy = create_blank_pdf(tmp_path / "delete-all-pages.pdf", 2)

    with pytest.raises(PdfPageMutationError, match="少なくとも1ページは残す必要があります"):
        PdfPageMutationService().delete_pages(working_copy, (0, 1), current_page_index=0)

    assert page_count(working_copy) == 2


def test_delete_pages_preserves_rotations_and_page_boxes_for_survivors(tmp_path: Path) -> None:
    rotations_path = copy_compatibility_fixture("rotations.pdf", tmp_path / "rotations.pdf")
    page_boxes_path = copy_compatibility_fixture("page-boxes.pdf", tmp_path / "page-boxes.pdf")
    service = PdfPageMutationService()

    rotations_before = service._snapshot_document_structure(rotations_path)
    page_boxes_before = service._snapshot_document_structure(page_boxes_path)

    rotations_mutation = service.delete_pages(rotations_path, (1,), current_page_index=0)
    page_boxes_mutation = service.delete_pages(page_boxes_path, (0,), current_page_index=0)

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

    annotations_mutation = service.delete_pages(annotations_path, (0,), current_page_index=0)
    outline_mutation = service.delete_pages(outline_path, (1,), current_page_index=0)

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
        PdfPageMutationService().delete_pages(outline_path, (2,), current_page_index=0)

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
        PdfPageMutationService().delete_pages(path, (0,), current_page_index=0)

    assert file_sha256(path) == before_sha


def test_delete_pages_receipt_tracks_working_copy_identity_and_current_page(
    tmp_path: Path,
) -> None:
    working_copy = create_text_fixture(tmp_path / "delete-receipt.pdf", ["A", "B", "C"])

    mutation = PdfPageMutationService().delete_pages(
        working_copy,
        (1,),
        current_page_index=2,
    )

    assert mutation.receipt.working_copy_path == working_copy.resolve()
    assert mutation.receipt.original_current_page_index == 2


@pytest.mark.parametrize(
    ("failure_target", "expected_error"),
    [
        ("receipt", "receipt failed"),
        ("transition", "transition failed"),
        ("mutation_result", "mutation result failed"),
        ("prepared_result", "prepared result failed"),
    ],
)
def test_delete_pages_prepared_result_failures_do_not_replace_working_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
    expected_error: str,
) -> None:
    source_path = create_text_fixture(tmp_path / "source.pdf", ["A", "B", "C", "D"])
    working_copy = tmp_path / "working.pdf"
    shutil.copyfile(source_path, working_copy)
    service = PdfPageMutationService()
    working_sha_before = file_sha256(working_copy)
    source_sha_before = file_sha256(source_path)
    replace_calls: list[tuple[Path, Path]] = []

    def replace_spy(source: Path | str, destination: Path | str) -> None:
        replace_calls.append((Path(source), Path(destination)))

    monkeypatch.setattr(mutation_module.os, "replace", replace_spy)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(expected_error)

    if failure_target == "receipt":
        monkeypatch.setattr(mutation_module, "PageDeletionReceipt", fail)
    elif failure_target == "transition":
        monkeypatch.setattr(service, "_build_delete_execute_transition", fail)
    elif failure_target == "mutation_result":
        monkeypatch.setattr(mutation_module, "WorkingCopyMutationResult", fail)
    else:
        monkeypatch.setattr(mutation_module, "PageDeletionMutation", fail)

    with pytest.raises(PdfPageMutationError, match="作業コピーPDFの更新に失敗しました"):
        service.delete_pages(working_copy, (1, 3), current_page_index=2)

    assert replace_calls == []
    assert file_sha256(working_copy) == working_sha_before
    assert page_count(working_copy) == 4
    assert file_sha256(source_path) == source_sha_before
    assert delete_undo_snapshots(working_copy) == []
    assert delete_candidates(working_copy) == []


@pytest.mark.parametrize(
    ("current_page_index", "expected_exception", "expected_message"),
    [
        (-1, ValueError, "current_page_index must stay within the page range"),
        (3, ValueError, "current_page_index must stay within the page range"),
        (4, ValueError, "current_page_index must stay within the page range"),
        (True, TypeError, "current_page_index must be an integer"),
        (1.5, TypeError, "current_page_index must be an integer"),
        ("1", TypeError, "current_page_index must be an integer"),
    ],
)
def test_delete_pages_validates_current_page_index_before_creating_temporary_files(
    tmp_path: Path,
    current_page_index: object,
    expected_exception: type[Exception],
    expected_message: str,
) -> None:
    working_copy = create_blank_pdf(tmp_path / "current-page-validation.pdf", 3)
    service = PdfPageMutationService()
    working_sha_before = file_sha256(working_copy)

    with pytest.raises(expected_exception, match=expected_message):
        service.delete_pages(
            working_copy,
            (1,),
            current_page_index=current_page_index,
        )

    assert file_sha256(working_copy) == working_sha_before
    assert page_count(working_copy) == 3
    assert delete_undo_snapshots(working_copy) == []
    assert delete_candidates(working_copy) == []


@pytest.mark.parametrize(
    ("failure_target", "expected_error"),
    [
        ("transition", "undo transition failed"),
        ("mutation_result", "undo mutation result failed"),
    ],
)
def test_undo_page_deletion_prepared_result_failures_preserve_deleted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
    expected_error: str,
) -> None:
    working_copy = create_text_fixture(tmp_path / "undo-atomicity.pdf", ["A", "B", "C", "D"])
    service = PdfPageMutationService()
    mutation = service.delete_pages(working_copy, (1, 3), current_page_index=2)
    deleted_state_sha = file_sha256(working_copy)
    snapshot_path = mutation.receipt.undo_snapshot_path
    replace_calls: list[tuple[Path, Path]] = []

    def replace_spy(source: Path | str, destination: Path | str) -> None:
        replace_calls.append((Path(source), Path(destination)))

    monkeypatch.setattr(mutation_module.os, "replace", replace_spy)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(expected_error)

    if failure_target == "transition":
        monkeypatch.setattr(service, "_build_delete_undo_transition", fail)
    else:
        monkeypatch.setattr(mutation_module, "WorkingCopyMutationResult", fail)

    with pytest.raises(PdfPageMutationError, match="作業コピーPDFの更新に失敗しました"):
        service.undo_page_deletion(working_copy, mutation.receipt)

    assert replace_calls == []
    assert file_sha256(working_copy) == deleted_state_sha
    assert extract_pdfium_text(working_copy) == "A C"
    assert snapshot_path.exists()
    assert delete_candidates(working_copy) == []


@pytest.mark.parametrize(
    ("failure_target", "expected_error"),
    [
        ("transition", "redo transition failed"),
        ("mutation_result", "redo mutation result failed"),
    ],
)
def test_redo_page_deletion_prepared_result_failures_preserve_original_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
    expected_error: str,
) -> None:
    working_copy = create_text_fixture(tmp_path / "redo-atomicity.pdf", ["A", "B", "C", "D"])
    service = PdfPageMutationService()
    mutation = service.delete_pages(working_copy, (1, 3), current_page_index=2)
    service.undo_page_deletion(working_copy, mutation.receipt)
    original_state_sha = file_sha256(working_copy)
    snapshot_path = mutation.receipt.undo_snapshot_path
    replace_calls: list[tuple[Path, Path]] = []

    def replace_spy(source: Path | str, destination: Path | str) -> None:
        replace_calls.append((Path(source), Path(destination)))

    monkeypatch.setattr(mutation_module.os, "replace", replace_spy)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(expected_error)

    if failure_target == "transition":
        monkeypatch.setattr(service, "_build_delete_execute_transition", fail)
    else:
        monkeypatch.setattr(mutation_module, "WorkingCopyMutationResult", fail)

    with pytest.raises(PdfPageMutationError, match="作業コピーPDFの更新に失敗しました"):
        service.redo_page_deletion(working_copy, mutation.receipt)

    assert replace_calls == []
    assert file_sha256(working_copy) == original_state_sha
    assert extract_pdfium_text(working_copy) == "A B C D"
    assert snapshot_path.exists()
    assert delete_candidates(working_copy) == []


def test_page_deletion_receipt_rejects_working_copy_as_snapshot_path(tmp_path: Path) -> None:
    working_copy = create_blank_pdf(tmp_path / "receipt-self.pdf", 3)
    mutation = PdfPageMutationService().delete_pages(working_copy, (1,), current_page_index=1)

    with pytest.raises(ValueError, match="must differ"):
        clone_delete_receipt(mutation.receipt, undo_snapshot_path=working_copy.resolve())


def test_redo_page_deletion_rejects_resources_only_drift_without_changing_file(
    tmp_path: Path,
) -> None:
    working_copy = create_text_fixture(tmp_path / "redo-resource-drift.pdf", ["A", "B", "C", "D"])
    service = PdfPageMutationService()
    mutation = service.delete_pages(working_copy, (1,), current_page_index=2)
    service.undo_page_deletion(working_copy, mutation.receipt)
    add_unused_font_resource(working_copy, 2)
    sha_before_call = file_sha256(working_copy)

    with pytest.raises(PdfPageMutationError, match="前提状態"):
        service.redo_page_deletion(working_copy, mutation.receipt)

    assert file_sha256(working_copy) == sha_before_call


@pytest.mark.parametrize(
    "case_name",
    ["outside", "wrong-prefix", "subdirectory", "directory", "symlink"],
)
def test_discard_page_deletion_receipt_rejects_malicious_snapshot_paths(
    tmp_path: Path,
    case_name: str,
) -> None:
    source_path = create_text_fixture(tmp_path / "source.pdf", ["A", "B", "C"])
    working_copy = tmp_path / "working.pdf"
    shutil.copyfile(source_path, working_copy)
    service = PdfPageMutationService()
    mutation = service.delete_pages(working_copy, (1,), current_page_index=1)
    valid_snapshot_path = mutation.receipt.undo_snapshot_path
    working_sha_before = file_sha256(working_copy)
    source_sha_before = file_sha256(source_path)
    path_to_preserve: Path
    preserved_file_sha: str | None = None

    if case_name == "outside":
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        path_to_preserve = create_blank_pdf(outside_dir / "outside.pdf", 1)
        preserved_file_sha = file_sha256(path_to_preserve)
    elif case_name == "wrong-prefix":
        path_to_preserve = working_copy.parent / f".{working_copy.stem}.other.pdf"
        shutil.copyfile(valid_snapshot_path, path_to_preserve)
        preserved_file_sha = file_sha256(path_to_preserve)
    elif case_name == "subdirectory":
        subdirectory = working_copy.parent / "subdir"
        subdirectory.mkdir()
        path_to_preserve = subdirectory / f".{working_copy.stem}.delete-undo.subdir.pdf"
        shutil.copyfile(valid_snapshot_path, path_to_preserve)
        preserved_file_sha = file_sha256(path_to_preserve)
    elif case_name == "directory":
        path_to_preserve = working_copy.parent / f".{working_copy.stem}.delete-undo.dir.pdf"
        path_to_preserve.mkdir()
    else:
        symlink_target = create_blank_pdf(tmp_path / "symlink-target.pdf", 1)
        path_to_preserve = working_copy.parent / f".{working_copy.stem}.delete-undo.symlink.pdf"
        path_to_preserve.symlink_to(symlink_target)
        preserved_file_sha = file_sha256(symlink_target)

    malicious_receipt = clone_delete_receipt(
        mutation.receipt,
        undo_snapshot_path=path_to_preserve,
    )

    with pytest.raises(PdfPageMutationError, match="削除前スナップショット"):
        service.discard_page_deletion_receipt(working_copy, malicious_receipt)

    assert file_sha256(working_copy) == working_sha_before
    assert file_sha256(source_path) == source_sha_before
    assert valid_snapshot_path.exists()
    if preserved_file_sha is not None and path_to_preserve.is_symlink():
        assert file_sha256(path_to_preserve.resolve()) == preserved_file_sha
    elif preserved_file_sha is not None:
        assert file_sha256(path_to_preserve) == preserved_file_sha
    else:
        assert path_to_preserve.exists()


def test_discard_page_deletion_receipt_rejects_mismatched_working_copy_identity(
    tmp_path: Path,
) -> None:
    first_working_copy = create_blank_pdf(tmp_path / "first.pdf", 3)
    second_working_copy = create_blank_pdf(tmp_path / "second.pdf", 3)
    service = PdfPageMutationService()
    first_mutation = service.delete_pages(first_working_copy, (1,), current_page_index=1)
    second_mutation = service.delete_pages(second_working_copy, (1,), current_page_index=1)

    with pytest.raises(PdfPageMutationError, match="所有者"):
        service.discard_page_deletion_receipt(first_working_copy, second_mutation.receipt)

    assert first_mutation.receipt.undo_snapshot_path.exists()
    assert second_mutation.receipt.undo_snapshot_path.exists()


def test_discard_page_deletion_receipt_is_idempotent_for_missing_owned_snapshot(
    tmp_path: Path,
) -> None:
    working_copy = create_blank_pdf(tmp_path / "discard-missing.pdf", 3)
    service = PdfPageMutationService()
    mutation = service.delete_pages(working_copy, (1,), current_page_index=1)
    missing_snapshot_path = working_copy.parent / f".{working_copy.stem}.delete-undo.missing.pdf"
    missing_receipt = clone_delete_receipt(
        mutation.receipt,
        undo_snapshot_path=missing_snapshot_path,
    )

    service.discard_page_deletion_receipt(working_copy, missing_receipt)

    assert not missing_snapshot_path.exists()
    assert mutation.receipt.undo_snapshot_path.exists()
