from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject

import pdf_workbench.services.pdf_page_mutation as mutation_module
from pdf_regression_utils import compatibility_fixture_dir, extract_pdfium_text, file_sha256
from pdf_test_utils import create_blank_pdf, create_simple_text_pdf
from pdf_workbench.domain.page_reorder import (
    PageReorderNoOpError,
    PageReorderPlan,
    build_page_reorder_plan,
)
from pdf_workbench.services.pdf_page_mutation import (
    PageReorderReceipt,
    PdfNamedDestinationSnapshot,
    PdfOutlineItemSnapshot,
    PdfPageMutationError,
    PdfPageMutationService,
)


def create_outline_attachment_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    writer.add_outline_item("First", 0)
    writer.add_attachment("note.txt", b"hello world")
    writer.add_metadata({"/Title": "Reorder Demo"})
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


def create_acroform_pdf(path: Path) -> Path:
    create_blank_pdf(path, 2)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root["/AcroForm"] = pikepdf.Dictionary()
        pdf.save(path)
    return path


def create_page_labels_pdf(path: Path) -> Path:
    create_blank_pdf(path, 2)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root["/PageLabels"] = pikepdf.Dictionary(
            {"/Nums": pikepdf.Array([0, pikepdf.Dictionary({"/S": pikepdf.Name("/D")})])}
        )
        pdf.save(path)
    return path


def create_tagged_pdf(path: Path) -> Path:
    create_blank_pdf(path, 2)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root["/StructTreeRoot"] = pikepdf.Dictionary({"/Type": pikepdf.Name("/StructTreeRoot")})
        pdf.save(path)
    return path


def create_open_action_pdf(path: Path) -> Path:
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=200, height=200)
    writer.open_destination = writer.pages[0]
    with path.open("wb") as stream:
        writer.write(stream)
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
        first_page["/Annots"] = pikepdf.Array([link])
        pdf.save(path)
    return path


def create_threads_pdf(path: Path) -> Path:
    create_blank_pdf(path, 2)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root["/Threads"] = pikepdf.Array()
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


def _first_annotation(page: pikepdf.Page) -> pikepdf.Object:
    annots = page.obj.get("/Annots", None)
    if not isinstance(annots, pikepdf.Array):
        raise AssertionError("annotation array was not created")
    annot = annots[0]
    return annot if isinstance(annot, pikepdf.Object) else annot.get_object()


def create_annotation_with_parent_pdf(path: Path) -> Path:
    create_square_annotation_pdf(path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        second_page = pdf.pages[1].obj
        annot = _first_annotation(pdf.pages[1])
        annot["/P"] = second_page
        pdf.save(path)
    return path


def create_cross_page_parent_annotation_pdf(path: Path) -> Path:
    create_annotation_with_parent_pdf(path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        first_page = pdf.pages[0].obj
        annot = _first_annotation(pdf.pages[1])
        annot["/P"] = first_page
        pdf.save(path)
    return path


def create_unresolved_parent_annotation_pdf(path: Path) -> Path:
    create_annotation_with_parent_pdf(path)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        annot = _first_annotation(pdf.pages[1])
        annot["/P"] = pikepdf.Dictionary({"/Type": pikepdf.Name("/Page")})
        pdf.save(path)
    return path


def copy_compatibility_fixture(name: str, destination: Path) -> Path:
    shutil.copyfile(compatibility_fixture_dir() / name, destination)
    return destination


def page_count(path: Path) -> int:
    return len(PdfReader(str(path)).pages)


def reorder_candidates(path: Path) -> list[Path]:
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


def outline_summary(
    items: tuple[PdfOutlineItemSnapshot, ...],
) -> tuple[tuple[str, int | None, tuple[object, ...]], ...]:
    summary: list[tuple[str, int | None, tuple[object, ...]]] = []
    for item in items:
        summary.append((item.title, item.destination_page_index, outline_summary(item.children)))
    return tuple(summary)


def clone_reorder_receipt(
    receipt: PageReorderReceipt,
    **overrides: object,
) -> PageReorderReceipt:
    return PageReorderReceipt(
        original_page_count=receipt.original_page_count,
        source_page_indexes=overrides.get("source_page_indexes", receipt.source_page_indexes),
        insertion_slot=overrides.get("insertion_slot", receipt.insertion_slot),
        target_order=overrides.get("target_order", receipt.target_order),
        old_to_new=overrides.get("old_to_new", receipt.old_to_new),
        moved_page_indexes_after=overrides.get(
            "moved_page_indexes_after",
            receipt.moved_page_indexes_after,
        ),
        before_snapshot=overrides.get("before_snapshot", receipt.before_snapshot),
        after_snapshot=overrides.get("after_snapshot", receipt.after_snapshot),
    )


@pytest.mark.parametrize(
    ("page_count_value", "source_page_indexes", "insertion_slot", "expected_target_order"),
    [
        (5, (1,), 5, (0, 2, 3, 4, 1)),
        (5, (3,), 1, (0, 3, 1, 2, 4)),
        (5, (1, 2), 5, (0, 3, 4, 1, 2)),
        (5, (3, 4), 1, (0, 3, 4, 1, 2)),
        (6, (1, 3), 6, (0, 2, 4, 5, 1, 3)),
        (6, (3, 5), 1, (0, 3, 5, 1, 2, 4)),
        (5, (2, 4), 0, (2, 4, 0, 1, 3)),
        (5, (0, 2), 5, (1, 3, 4, 0, 2)),
    ],
)
def test_build_page_reorder_plan_computes_exact_target_order_and_inverse_mapping(
    page_count_value: int,
    source_page_indexes: tuple[int, ...],
    insertion_slot: int,
    expected_target_order: tuple[int, ...],
) -> None:
    plan = build_page_reorder_plan(page_count_value, source_page_indexes, insertion_slot)

    assert plan.source_page_indexes == tuple(sorted(source_page_indexes))
    assert plan.target_order == expected_target_order
    assert plan.new_to_old == expected_target_order
    assert tuple(plan.target_order[index] for index in plan.moved_page_indexes_after) == tuple(
        sorted(source_page_indexes)
    )
    assert tuple(plan.old_to_new[old_index] for old_index in range(page_count_value)) == tuple(
        expected_target_order.index(old_index) for old_index in range(page_count_value)
    )
    assert tuple(
        plan.new_to_old[plan.old_to_new[index]] for index in range(page_count_value)
    ) == tuple(range(page_count_value))


def test_build_page_reorder_plan_normalizes_duplicate_indexes_without_breaking_order() -> None:
    plan = build_page_reorder_plan(5, (3, 1, 3, 1), 5)

    assert plan.source_page_indexes == (1, 3)
    assert plan.target_order == (0, 2, 4, 1, 3)
    assert plan.moved_page_indexes_after == (3, 4)


@pytest.mark.parametrize(
    ("page_count_value", "source_page_indexes", "insertion_slot", "error_type", "message"),
    [
        (5, (), 1, ValueError, "must not be empty"),
        (5, (-1,), 1, ValueError, "non-negative"),
        (5, (5,), 1, ValueError, "page range"),
        (5, (1,), -1, ValueError, "0..page_count"),
        (5, (1,), 6, ValueError, "0..page_count"),
        (5, (True,), 1, TypeError, "integer"),
        (5, (1.5,), 1, TypeError, "integer"),
        (5, ("1",), 1, TypeError, "integer"),
        (True, (1,), 1, TypeError, "integer"),
        (5, (1,), True, TypeError, "integer"),
    ],
)
def test_build_page_reorder_plan_rejects_invalid_inputs(
    page_count_value: object,
    source_page_indexes: tuple[object, ...],
    insertion_slot: object,
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        build_page_reorder_plan(page_count_value, source_page_indexes, insertion_slot)


@pytest.mark.parametrize(
    ("page_count_value", "source_page_indexes", "insertion_slot"),
    [
        (5, (1,), 2),
        (5, (1, 2), 3),
        (5, (1, 2, 3), 4),
        (3, (0, 1, 2), 0),
    ],
)
def test_build_page_reorder_plan_rejects_identity_and_inside_block_drops(
    page_count_value: int,
    source_page_indexes: tuple[int, ...],
    insertion_slot: int,
) -> None:
    with pytest.raises(PageReorderNoOpError, match="no-op"):
        build_page_reorder_plan(page_count_value, source_page_indexes, insertion_slot)


def test_page_reorder_plan_rejects_invalid_permutations_and_moved_block_metadata() -> None:
    with pytest.raises(ValueError, match="target_order must be a valid permutation"):
        PageReorderPlan(
            page_count=3,
            source_page_indexes=(1,),
            insertion_slot=3,
            target_order=(0, 0, 1),
            old_to_new=(0, 2, 1),
            new_to_old=(0, 0, 1),
            moved_page_indexes_after=(2,),
        )

    with pytest.raises(ValueError, match="old_to_new does not match"):
        PageReorderPlan(
            page_count=3,
            source_page_indexes=(1,),
            insertion_slot=3,
            target_order=(0, 2, 1),
            old_to_new=(0, 1, 2),
            new_to_old=(0, 2, 1),
            moved_page_indexes_after=(2,),
        )

    with pytest.raises(ValueError, match="moved_page_indexes_after does not match"):
        PageReorderPlan(
            page_count=3,
            source_page_indexes=(1,),
            insertion_slot=3,
            target_order=(0, 2, 1),
            old_to_new=(0, 2, 1),
            new_to_old=(0, 2, 1),
            moved_page_indexes_after=(1,),
        )


def test_page_reorder_plan_direct_constructor_canonicalizes_source_indexes() -> None:
    plan = PageReorderPlan(
        page_count=5,
        source_page_indexes=(3, 1, 3, 1),
        insertion_slot=5,
        target_order=(0, 2, 4, 1, 3),
        old_to_new=(0, 3, 1, 4, 2),
        new_to_old=(0, 2, 4, 1, 3),
        moved_page_indexes_after=(3, 4),
    )

    assert plan.source_page_indexes == (1, 3)


def test_page_reorder_plan_builder_matches_direct_constructor_canonical_object() -> None:
    built = build_page_reorder_plan(5, (3, 1, 3, 1), 5)
    direct = PageReorderPlan(
        page_count=5,
        source_page_indexes=(3, 1, 3, 1),
        insertion_slot=5,
        target_order=(0, 2, 4, 1, 3),
        old_to_new=(0, 3, 1, 4, 2),
        new_to_old=(0, 2, 4, 1, 3),
        moved_page_indexes_after=(3, 4),
    )

    assert built == direct


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("target_order", (0, True, 1)),
        ("old_to_new", (0, True, 1)),
        ("target_order", (0, 1.0, 2)),
        ("old_to_new", (0, "1", 2)),
    ],
)
def test_page_reorder_plan_rejects_noncanonical_permutation_values(
    field_name: str,
    field_value: tuple[object, ...],
) -> None:
    kwargs: dict[str, object] = {
        "page_count": 3,
        "source_page_indexes": (1,),
        "insertion_slot": 3,
        "target_order": (0, 2, 1),
        "old_to_new": (0, 2, 1),
        "new_to_old": (0, 2, 1),
        "moved_page_indexes_after": (2,),
    }
    kwargs[field_name] = field_value

    with pytest.raises(TypeError, match="integer"):
        PageReorderPlan(**kwargs)


def test_page_reorder_receipt_direct_constructor_canonicalizes_source_indexes(
    tmp_path: Path,
) -> None:
    working_copy = create_simple_text_pdf(
        tmp_path / "receipt-canonical.pdf",
        ["A", "B", "C", "D", "E"],
    )
    service = PdfPageMutationService()
    before_snapshot = service._snapshot_document_structure(working_copy)
    mutation = service.reorder_pages(working_copy, (1, 3), 5)

    receipt = PageReorderReceipt(
        original_page_count=5,
        source_page_indexes=(3, 1, 3),
        insertion_slot=5,
        target_order=(0, 2, 4, 1, 3),
        old_to_new=(0, 3, 1, 4, 2),
        moved_page_indexes_after=(3, 4),
        before_snapshot=before_snapshot,
        after_snapshot=mutation.receipt.after_snapshot,
    )

    assert receipt.source_page_indexes == (1, 3)


def test_reorder_pages_executes_undoes_and_redoes_with_exact_permutation(tmp_path: Path) -> None:
    source_path = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C", "D", "E", "F"])
    working_copy = tmp_path / "working.pdf"
    shutil.copyfile(source_path, working_copy)
    service = PdfPageMutationService()
    source_sha_before = file_sha256(source_path)
    working_sha_before = file_sha256(working_copy)
    before_snapshot = service._snapshot_document_structure(working_copy)

    mutation = service.reorder_pages(working_copy, (1, 3), 6)

    assert mutation.receipt.target_order == (0, 2, 4, 5, 1, 3)
    assert mutation.receipt.old_to_new == (0, 4, 1, 5, 2, 3)
    assert mutation.receipt.moved_page_indexes_after == (4, 5)
    assert mutation.mutation_result.page_count == 6
    assert mutation.mutation_result.page_index_transition is not None
    assert mutation.mutation_result.page_index_transition.cache_old_to_new == (0, 4, 1, 5, 2, 3)
    assert mutation.mutation_result.page_index_transition.current_page_old_to_new == (
        0,
        4,
        1,
        5,
        2,
        3,
    )
    assert extract_pdfium_text(working_copy) == "A C E F B D"
    assert file_sha256(working_copy) != working_sha_before
    assert file_sha256(source_path) == source_sha_before
    assert reorder_candidates(working_copy) == []

    undo_result = service.undo_page_reordering(working_copy, mutation.receipt)

    assert undo_result.page_count == 6
    assert undo_result.page_index_transition is not None
    assert undo_result.page_index_transition.cache_old_to_new == mutation.receipt.target_order
    assert (
        undo_result.page_index_transition.current_page_old_to_new == mutation.receipt.target_order
    )
    assert extract_pdfium_text(working_copy) == "A B C D E F"
    assert service._snapshot_document_structure(working_copy) == before_snapshot
    assert file_sha256(working_copy) != working_sha_before

    service.validate_reordering_redo_precondition(
        working_copy,
        mutation.receipt,
    )
    redo_result = service.redo_page_reordering(working_copy, mutation.receipt)

    assert redo_result.page_count == 6
    assert extract_pdfium_text(working_copy) == "A C E F B D"
    assert file_sha256(source_path) == source_sha_before
    assert reorder_candidates(working_copy) == []


def test_reorder_pages_preserves_metadata_outlines_and_attachments(tmp_path: Path) -> None:
    working_copy = create_outline_attachment_pdf(tmp_path / "outline-attachment.pdf")
    service = PdfPageMutationService()
    before_snapshot = service._snapshot_document_structure(working_copy)

    mutation = service.reorder_pages(working_copy, (0,), 2)
    after_snapshot = service._snapshot_document_structure(working_copy)

    assert after_snapshot.metadata_fingerprint == before_snapshot.metadata_fingerprint
    assert after_snapshot.attachments_fingerprint == before_snapshot.attachments_fingerprint
    assert outline_summary(after_snapshot.outlines) == (("First", 1, ()),)

    service.undo_page_reordering(working_copy, mutation.receipt)
    assert service._snapshot_document_structure(working_copy) == before_snapshot


def test_reorder_pages_remaps_outline_and_named_destination_indexes(tmp_path: Path) -> None:
    working_copy = create_outline_mapping_pdf(tmp_path / "outline-map.pdf")
    service = PdfPageMutationService()
    before_snapshot = service._snapshot_document_structure(working_copy)

    mutation = service.reorder_pages(working_copy, (0,), 3)
    after_snapshot = service._snapshot_document_structure(working_copy)

    assert outline_summary(before_snapshot.outlines) == (
        ("A", 0, (("A child", 0, ()),)),
        ("B", 2, ()),
    )
    assert before_snapshot.named_destinations == (PdfNamedDestinationSnapshot("Later", 2),)
    assert outline_summary(after_snapshot.outlines) == (
        ("A", 2, (("A child", 2, ()),)),
        ("B", 1, ()),
    )
    assert after_snapshot.named_destinations == (PdfNamedDestinationSnapshot("Later", 1),)

    service.undo_page_reordering(working_copy, mutation.receipt)
    assert service._snapshot_document_structure(working_copy) == before_snapshot


@pytest.mark.parametrize(
    ("fixture_name", "source_page_indexes", "insertion_slot", "expected_target_order"),
    [
        ("rotations.pdf", (1, 3), 0, (1, 3, 0, 2)),
        ("page-boxes.pdf", (0,), 2, (1, 0)),
    ],
)
def test_reorder_pages_preserves_rotation_and_page_box_snapshots(
    tmp_path: Path,
    fixture_name: str,
    source_page_indexes: tuple[int, ...],
    insertion_slot: int,
    expected_target_order: tuple[int, ...],
) -> None:
    working_copy = copy_compatibility_fixture(fixture_name, tmp_path / fixture_name)
    service = PdfPageMutationService()
    before_snapshot = service._snapshot_document_structure(working_copy)

    mutation = service.reorder_pages(working_copy, source_page_indexes, insertion_slot)
    after_snapshot = service._snapshot_document_structure(working_copy)

    assert mutation.receipt.target_order == expected_target_order
    for new_page_index, original_page_index in enumerate(expected_target_order):
        assert after_snapshot.pages[new_page_index] == before_snapshot.pages[original_page_index]

    service.undo_page_reordering(working_copy, mutation.receipt)
    assert service._snapshot_document_structure(working_copy) == before_snapshot


def test_reorder_pages_preserves_annotation_parent_reference_for_moved_page(tmp_path: Path) -> None:
    working_copy = create_annotation_with_parent_pdf(tmp_path / "annot-parent.pdf")
    service = PdfPageMutationService()
    before_snapshot = service._snapshot_document_structure(working_copy)

    mutation = service.reorder_pages(working_copy, (1,), 0)

    with pikepdf.open(working_copy) as pdf:
        moved_page = pdf.pages[0].obj
        annot = _first_annotation(pdf.pages[0])
        parent = annot.get("/P", None)
        assert isinstance(parent, pikepdf.Object)
        assert parent.objgen == moved_page.objgen

    service.undo_page_reordering(working_copy, mutation.receipt)
    assert service._snapshot_document_structure(working_copy) == before_snapshot


@pytest.mark.parametrize(
    ("builder", "message"),
    [
        (create_acroform_pdf, "フォーム"),
        (create_widget_pdf, "Widget"),
        (create_page_labels_pdf, "PageLabels"),
        (create_tagged_pdf, "タグ付きPDF"),
        (create_open_action_pdf, "OpenAction"),
        (create_goto_annotation_pdf, "内部宛先注釈"),
        (create_threads_pdf, "Article Threads"),
        (create_cross_page_parent_annotation_pdf, "他ページを参照する注釈"),
        (create_unresolved_parent_annotation_pdf, "注釈の/P参照を解決できません"),
    ],
)
def test_reorder_pages_rejects_unsupported_structures_without_changing_working_copy(
    tmp_path: Path,
    builder: Callable[[Path], Path],
    message: str,
) -> None:
    working_copy = builder(tmp_path / f"unsupported-{abs(hash(message))}.pdf")
    before_sha = file_sha256(working_copy)

    with pytest.raises(PdfPageMutationError, match=message):
        PdfPageMutationService().reorder_pages(working_copy, (0,), 2)

    assert file_sha256(working_copy) == before_sha
    assert reorder_candidates(working_copy) == []


def test_reorder_pages_rejects_stale_execute_snapshot_without_changing_file(
    tmp_path: Path,
) -> None:
    working_copy = create_simple_text_pdf(tmp_path / "stale-before.pdf", ["A", "B", "C"])
    service = PdfPageMutationService()
    stale_snapshot = service._snapshot_document_structure(working_copy)
    with pikepdf.open(working_copy, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Rotate"] = 90
        pdf.save(working_copy)
    sha_before_call = file_sha256(working_copy)

    with pytest.raises(PdfPageMutationError, match="前提状態"):
        service.reorder_pages(
            working_copy,
            (1,),
            3,
            expected_before_snapshot=stale_snapshot,
        )

    assert file_sha256(working_copy) == sha_before_call
    assert reorder_candidates(working_copy) == []


def test_undo_page_reordering_rejects_stale_after_snapshot_without_changing_file(
    tmp_path: Path,
) -> None:
    working_copy = create_simple_text_pdf(tmp_path / "stale-after.pdf", ["A", "B", "C", "D"])
    service = PdfPageMutationService()
    mutation = service.reorder_pages(working_copy, (1,), 4)
    with pikepdf.open(working_copy, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Rotate"] = 90
        pdf.save(working_copy)
    sha_before_call = file_sha256(working_copy)

    with pytest.raises(PdfPageMutationError, match="元に戻せません"):
        service.undo_page_reordering(working_copy, mutation.receipt)

    assert file_sha256(working_copy) == sha_before_call
    assert reorder_candidates(working_copy) == []


def test_reorder_pages_render_validation_failure_preserves_working_copy_and_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_path = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C", "D"])
    working_copy = tmp_path / "working.pdf"
    shutil.copyfile(source_path, working_copy)
    service = PdfPageMutationService()
    source_sha_before = file_sha256(source_path)
    working_sha_before = file_sha256(working_copy)
    replace_calls: list[tuple[Path, Path]] = []

    def replace_spy(source: Path, destination: Path) -> None:
        replace_calls.append((source, destination))

    monkeypatch.setattr(service, "_replace_atomically", replace_spy)
    monkeypatch.setattr(
        service,
        "_render_pages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PdfPageMutationError("render failed")),
    )

    with pytest.raises(PdfPageMutationError, match="render failed"):
        service.reorder_pages(working_copy, (1,), 4)

    assert replace_calls == []
    assert file_sha256(working_copy) == working_sha_before
    assert file_sha256(source_path) == source_sha_before
    assert reorder_candidates(working_copy) == []


@pytest.mark.parametrize(
    ("failure_target", "expected_error"),
    [
        ("transition", "transition failed"),
        ("receipt", "receipt failed"),
        ("mutation_result", "mutation result failed"),
        ("prepared_result", "prepared result failed"),
    ],
)
def test_reorder_pages_prepared_result_failures_do_not_replace_working_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
    expected_error: str,
) -> None:
    source_path = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C", "D"])
    working_copy = tmp_path / "working.pdf"
    shutil.copyfile(source_path, working_copy)
    service = PdfPageMutationService()
    source_sha_before = file_sha256(source_path)
    working_sha_before = file_sha256(working_copy)
    replace_calls: list[tuple[Path, Path]] = []

    def replace_spy(source: Path, destination: Path) -> None:
        replace_calls.append((source, destination))

    monkeypatch.setattr(service, "_replace_atomically", replace_spy)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(expected_error)

    if failure_target == "transition":
        monkeypatch.setattr(service, "_build_reorder_execute_transition", fail)
    elif failure_target == "receipt":
        monkeypatch.setattr(mutation_module, "PageReorderReceipt", fail)
    elif failure_target == "mutation_result":
        monkeypatch.setattr(mutation_module, "WorkingCopyMutationResult", fail)
    else:
        monkeypatch.setattr(mutation_module, "PageReorderMutation", fail)

    with pytest.raises(PdfPageMutationError, match="作業コピーPDFの更新に失敗しました"):
        service.reorder_pages(working_copy, (1, 3), 4)

    assert replace_calls == []
    assert file_sha256(working_copy) == working_sha_before
    assert file_sha256(source_path) == source_sha_before
    assert reorder_candidates(working_copy) == []


@pytest.mark.parametrize(
    ("failure_target", "expected_error"),
    [
        ("transition", "undo transition failed"),
        ("mutation_result", "undo mutation result failed"),
    ],
)
def test_undo_page_reordering_prepared_result_failures_preserve_reordered_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
    expected_error: str,
) -> None:
    working_copy = create_simple_text_pdf(tmp_path / "undo-state.pdf", ["A", "B", "C", "D"])
    service = PdfPageMutationService()
    mutation = service.reorder_pages(working_copy, (1, 3), 4)
    reordered_sha = file_sha256(working_copy)
    replace_calls: list[tuple[Path, Path]] = []

    def replace_spy(source: Path, destination: Path) -> None:
        replace_calls.append((source, destination))

    monkeypatch.setattr(service, "_replace_atomically", replace_spy)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(expected_error)

    if failure_target == "transition":
        monkeypatch.setattr(service, "_build_reorder_undo_transition", fail)
    else:
        monkeypatch.setattr(mutation_module, "WorkingCopyMutationResult", fail)

    with pytest.raises(PdfPageMutationError, match="作業コピーPDFの更新に失敗しました"):
        service.undo_page_reordering(working_copy, mutation.receipt)

    assert replace_calls == []
    assert file_sha256(working_copy) == reordered_sha
    assert extract_pdfium_text(working_copy) == "A C B D"
    assert reorder_candidates(working_copy) == []


def test_reorder_pages_atomic_replace_failure_preserves_working_copy_and_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C", "D"])
    working_copy = tmp_path / "working.pdf"
    shutil.copyfile(source_path, working_copy)
    service = PdfPageMutationService()
    source_sha_before = file_sha256(source_path)
    working_sha_before = file_sha256(working_copy)

    def fail_replace(_candidate_path: Path, _destination_path: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(service, "_replace_atomically", fail_replace)

    with pytest.raises(PdfPageMutationError, match="作業コピーPDFの更新に失敗しました"):
        service.reorder_pages(working_copy, (1, 3), 4)

    assert file_sha256(working_copy) == working_sha_before
    assert file_sha256(source_path) == source_sha_before
    assert reorder_candidates(working_copy) == []


def test_validate_reordering_redo_precondition_rejects_tampered_original_state(
    tmp_path: Path,
) -> None:
    working_copy = create_simple_text_pdf(tmp_path / "redo-preflight.pdf", ["A", "B", "C", "D"])
    service = PdfPageMutationService()
    mutation = service.reorder_pages(working_copy, (1,), 4)
    service.undo_page_reordering(working_copy, mutation.receipt)
    with pikepdf.open(working_copy, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Rotate"] = 90
        pdf.save(working_copy)
    sha_before_call = file_sha256(working_copy)

    with pytest.raises(PdfPageMutationError, match="前提状態"):
        service.validate_reordering_redo_precondition(working_copy, mutation.receipt)

    assert file_sha256(working_copy) == sha_before_call


def test_validate_reordering_redo_precondition_rejects_resources_only_drift(
    tmp_path: Path,
) -> None:
    working_copy = create_simple_text_pdf(
        tmp_path / "redo-resource-drift.pdf", ["A", "B", "C", "D"]
    )
    service = PdfPageMutationService()
    mutation = service.reorder_pages(working_copy, (1,), 4)
    service.undo_page_reordering(working_copy, mutation.receipt)
    add_unused_font_resource(working_copy, 0)
    sha_before_call = file_sha256(working_copy)

    with pytest.raises(PdfPageMutationError, match="前提状態"):
        service.validate_reordering_redo_precondition(working_copy, mutation.receipt)

    assert file_sha256(working_copy) == sha_before_call


def test_page_reorder_receipt_rejects_tampered_after_snapshot_mapping(tmp_path: Path) -> None:
    working_copy = create_simple_text_pdf(tmp_path / "receipt.pdf", ["A", "B", "C", "D"])
    service = PdfPageMutationService()
    mutation = service.reorder_pages(working_copy, (1, 3), 4)

    with pytest.raises(ValueError, match="expected reordered page order"):
        clone_reorder_receipt(
            mutation.receipt,
            after_snapshot=mutation.receipt.before_snapshot,
        )


def test_page_reorder_receipt_rejects_resources_only_drift_in_after_snapshot(
    tmp_path: Path,
) -> None:
    working_copy = create_simple_text_pdf(tmp_path / "receipt-resources.pdf", ["A", "B", "C", "D"])
    service = PdfPageMutationService()
    mutation = service.reorder_pages(working_copy, (1, 3), 4)
    tampered_pages = list(mutation.receipt.after_snapshot.pages)
    moved_page_index = mutation.receipt.moved_page_indexes_after[0]
    tampered_pages[moved_page_index] = replace(
        tampered_pages[moved_page_index],
        resources_fingerprint="f" * 64,
    )
    tampered_after = replace(
        mutation.receipt.after_snapshot,
        pages=tuple(tampered_pages),
    )

    with pytest.raises(ValueError, match="expected reordered page order"):
        clone_reorder_receipt(
            mutation.receipt,
            after_snapshot=tampered_after,
        )
