from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfWriter

from pdf_regression_utils import extract_pdfium_text, file_sha256
from pdf_test_utils import create_blank_pdf, create_simple_text_pdf
from pdf_workbench.domain.mutation import PageIndexTransition
from pdf_workbench.services.pdf_page_mutation import (
    PageInsertionReceipt,
    PdfPageMutationError,
    PdfPageMutationService,
)


def create_outline_attachment_pdf(path: Path, *, title: str, attachment_name: str) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    writer.add_outline_item("First", 0)
    writer.add_named_destination("Later", 1)
    writer.add_attachment(attachment_name, title.encode("utf-8"))
    writer.add_metadata({"/Title": title})
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def _annotation_page_resources(pdf: pikepdf.Pdf) -> pikepdf.Dictionary:
    return pikepdf.Dictionary(
        {
            "/Font": pikepdf.Dictionary(
                {
                    "/F1": pdf.make_indirect(
                        pikepdf.Dictionary(
                            {
                                "/Type": pikepdf.Name("/Font"),
                                "/Subtype": pikepdf.Name("/Type1"),
                                "/BaseFont": pikepdf.Name("/Helvetica"),
                            }
                        )
                    )
                }
            )
        }
    )


def _base_annotation_dictionary(
    pdf: pikepdf.Pdf,
    *,
    subtype: str = "/Square",
    contents: str = "annot",
) -> pikepdf.Dictionary:
    return pikepdf.Dictionary(
        {
            "/Type": pikepdf.Name("/Annot"),
            "/Subtype": pikepdf.Name(subtype),
            "/Rect": pikepdf.Array([20, 20, 80, 80]),
            "/Contents": pikepdf.String(contents),
            "/C": pikepdf.Array([1, 0, 0]),
            "/F": 4,
            "/Border": pikepdf.Array([0, 0, 1]),
            "/AP": pikepdf.Dictionary({"/N": _annotation_appearance_stream(pdf)}),
        }
    )


def _annotation_appearance_stream(
    pdf: pikepdf.Pdf,
    *,
    color: tuple[float, float, float] = (1.0, 0.0, 0.0),
) -> pikepdf.Object:
    red, green, blue = color
    stream = pdf.make_stream(f"q {red} {green} {blue} rg 20 20 60 60 re f Q".encode("ascii"))
    stream["/Type"] = pikepdf.Name("/XObject")
    stream["/Subtype"] = pikepdf.Name("/Form")
    stream["/BBox"] = pikepdf.Array([20, 20, 80, 80])
    return stream


def create_supported_annotation_source_pdf(
    path: Path,
    *,
    subtype: str = "/Square",
    include_parent: bool,
    direct_annots: bool,
    indirect_annotation: bool,
    contents: str = "annot",
) -> Path:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page_contents = pdf.make_stream(b"BT /F1 24 Tf 48 96 Td (Annotated) Tj ET")
    resources = _annotation_page_resources(pdf)
    page.obj["/Contents"] = page_contents
    page.obj["/Resources"] = resources
    annotation = _base_annotation_dictionary(pdf, subtype=subtype, contents=contents)
    if include_parent:
        annotation["/P"] = page.obj
    annotation_value: pikepdf.Object | pikepdf.Dictionary = (
        pdf.make_indirect(annotation) if indirect_annotation else annotation
    )
    annots = pikepdf.Array([annotation_value])
    if direct_annots:
        page.obj["/Annots"] = annots
    else:
        page.obj["/Annots"] = pdf.make_indirect(annots)
    pdf.save(path)
    return path


def create_annotation_source_pdf_with_entries(
    pdf: pikepdf.Pdf,
    path: Path,
    entries: list[object],
    *,
    direct_annots: bool = True,
    page: pikepdf.Page | None = None,
) -> Path:
    resolved_page = page if page is not None else pdf.add_blank_page(page_size=(200, 200))
    resolved_page.obj["/Contents"] = pdf.make_stream(b"BT /F1 24 Tf 48 96 Td (Annotated) Tj ET")
    resolved_page.obj["/Resources"] = _annotation_page_resources(pdf)
    annots = pikepdf.Array(entries)
    resolved_page.obj["/Annots"] = annots if direct_annots else pdf.make_indirect(annots)
    pdf.save(path)
    return path


def create_supported_annotation_source_pdf_with_unresolved_parent(path: Path) -> Path:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    annotation = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Annot"),
                "/Subtype": pikepdf.Name("/Square"),
                "/Rect": pikepdf.Array([20, 20, 80, 80]),
                "/Contents": pikepdf.String("invalid-parent"),
                "/AP": pikepdf.Dictionary({"/N": _annotation_appearance_stream(pdf)}),
                "/P": pikepdf.Dictionary({"/Type": pikepdf.Name("/Page")}),
            }
        )
    )
    page.obj["/Annots"] = pikepdf.Array([annotation])
    pdf.save(path)
    return path


def create_mixed_annotation_source_pdf(path: Path) -> Path:
    pdf = pikepdf.Pdf.new()
    annotated_page = pdf.add_blank_page(page_size=(200, 200))
    plain_page = pdf.add_blank_page(page_size=(200, 200))
    for page, text in ((annotated_page, "Annotated"), (plain_page, "Plain")):
        page.obj["/Contents"] = pdf.make_stream(
            f"BT /F1 24 Tf 48 96 Td ({text}) Tj ET".encode("ascii")
        )
        page.obj["/Resources"] = pikepdf.Dictionary(
            {
                "/Font": pikepdf.Dictionary(
                    {
                        "/F1": pdf.make_indirect(
                            pikepdf.Dictionary(
                                {
                                    "/Type": pikepdf.Name("/Font"),
                                    "/Subtype": pikepdf.Name("/Type1"),
                                    "/BaseFont": pikepdf.Name("/Helvetica"),
                                }
                            )
                        )
                    }
                )
            }
        )
    annotation = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Annot"),
                "/Subtype": pikepdf.Name("/Text"),
                "/Rect": pikepdf.Array([20, 20, 80, 80]),
                "/Contents": pikepdf.String("mixed"),
                "/C": pikepdf.Array([1, 0, 0]),
                "/Border": pikepdf.Array([0, 0, 1]),
                "/AP": pikepdf.Dictionary({"/N": _annotation_appearance_stream(pdf)}),
                "/P": annotated_page.obj,
            }
        )
    )
    annotated_page.obj["/Annots"] = pdf.make_indirect(pikepdf.Array([annotation]))
    pdf.save(path)
    return path


def create_cross_page_parent_annotation_pdf(path: Path) -> Path:
    pdf = pikepdf.Pdf.new()
    first_page = pdf.add_blank_page(page_size=(200, 200))
    second_page = pdf.add_blank_page(page_size=(200, 200))
    annotation = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/Annot"),
                "/Subtype": pikepdf.Name("/Square"),
                "/Rect": pikepdf.Array([20, 20, 80, 80]),
                "/Contents": pikepdf.String("cross-page"),
                "/AP": pikepdf.Dictionary({"/N": _annotation_appearance_stream(pdf)}),
                "/P": second_page.obj,
            }
        )
    )
    first_page.obj["/Annots"] = pikepdf.Array([annotation])
    pdf.save(path)
    return path


def create_rejected_subtype_annotation_source_pdf(path: Path, *, subtype: str) -> Path:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    annotation = _base_annotation_dictionary(pdf, subtype=subtype, contents=subtype)
    annotation["/P"] = page.obj
    return create_annotation_source_pdf_with_entries(
        pdf,
        path,
        [pdf.make_indirect(annotation)],
        page=page,
    )


def create_malformed_annotation_source_pdf(path: Path, *, mode: str) -> Path:
    pdf = pikepdf.Pdf.new()
    if mode == "missing_subtype":
        annotation = _base_annotation_dictionary(pdf)
        del annotation["/Subtype"]
        entry: object = pdf.make_indirect(annotation)
    elif mode == "string_subtype":
        annotation = _base_annotation_dictionary(pdf)
        annotation["/Subtype"] = pikepdf.String("/Square")
        entry = pdf.make_indirect(annotation)
    elif mode == "non_dictionary_entry":
        entry = pikepdf.String("bad-entry")
    else:
        raise AssertionError(f"unsupported malformed mode: {mode}")
    return create_annotation_source_pdf_with_entries(pdf, path, [entry])


def create_active_content_annotation_source_pdf(
    path: Path,
    *,
    key: str,
    value: object,
) -> Path:
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    annotation = _base_annotation_dictionary(pdf, subtype="/Square", contents=key)
    annotation["/P"] = page.obj
    annotation[key] = value
    return create_annotation_source_pdf_with_entries(
        pdf,
        path,
        [pdf.make_indirect(annotation)],
        page=page,
    )


def create_target_with_existing_annotation(path: Path) -> Path:
    create_simple_text_pdf(path, ["Before", "Keep", "After"])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[1]
        annotation = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/Annot"),
                    "/Subtype": pikepdf.Name("/Text"),
                    "/Rect": pikepdf.Array([30, 30, 60, 60]),
                    "/Contents": pikepdf.String("target"),
                    "/C": pikepdf.Array([0, 0, 1]),
                    "/Border": pikepdf.Array([0, 0, 1]),
                    "/AP": pikepdf.Dictionary(
                        {"/N": _annotation_appearance_stream(pdf, color=(0.0, 0.0, 1.0))}
                    ),
                    "/P": page.obj,
                }
            )
        )
        page.obj["/Annots"] = pikepdf.Array([annotation])
        pdf.save(path)
    return path


def annotation_details(path: Path, *, page_index: int) -> tuple[dict[str, object], ...]:
    service = PdfPageMutationService()
    with pikepdf.open(path) as pdf:
        page = pdf.pages[page_index]
        annots_object = page.obj.get("/Annots", None)
        if annots_object is None:
            return ()
        annots = (
            annots_object
            if isinstance(annots_object, pikepdf.Array)
            else annots_object.get_object()
        )
        details: list[dict[str, object]] = []
        for annot_ref in annots:
            annot = annot_ref if isinstance(annot_ref, pikepdf.Object) else annot_ref.get_object()
            appearance = annot.get("/AP", None)
            appearance_fingerprint: str | None = None
            if appearance is not None:
                appearance_object = (
                    appearance
                    if isinstance(appearance, pikepdf.Object)
                    else appearance.get_object()
                )
                appearance_fingerprint = service._object_fingerprint(appearance_object)
            parent_object = annot.get("/P", None)
            parent = None
            if parent_object is not None:
                parent = (
                    parent_object
                    if isinstance(parent_object, pikepdf.Object)
                    else parent_object.get_object()
                )
            details.append(
                {
                    "subtype": str(annot.get("/Subtype", "")),
                    "rect": tuple(float(value) for value in annot.get("/Rect", ())),
                    "contents": str(annot.get("/Contents", "")),
                    "has_appearance": "/AP" in annot,
                    "appearance_fingerprint": appearance_fingerprint,
                    "parent_objgen": getattr(parent, "objgen", None),
                    "page_objgen": getattr(page.obj, "objgen", None),
                }
            )
        return tuple(details)


def assert_no_page_insertion_temp_files(target_path: Path) -> None:
    assert list(target_path.parent.glob(f".{target_path.stem}.insert-source.*.pdf")) == []
    assert list(target_path.parent.glob(f".{target_path.stem}.insert-undo.*.pdf")) == []
    assert list(target_path.parent.glob(f".{target_path.stem}.candidate.*.pdf")) == []


def create_inherited_crop_resources_pdf(path: Path) -> Path:
    create_simple_text_pdf(path, ["Inherited"])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        root_pages = pdf.Root.Pages
        root_pages["/MediaBox"] = pikepdf.Array([0, 0, 220, 220])
        root_pages["/CropBox"] = pikepdf.Array([10, 20, 180, 160])
        root_pages["/Resources"] = page.obj["/Resources"]
        del page.obj["/MediaBox"]
        del page.obj["/Resources"]
        pdf.save(path)
    return path


def clone_receipt(receipt: PageInsertionReceipt, **changes: object) -> PageInsertionReceipt:
    return replace(receipt, **changes)


@pytest.mark.parametrize(
    ("direct_annots", "indirect_annotation"),
    [
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ],
)
@pytest.mark.parametrize("include_parent", [False, True])
@pytest.mark.parametrize("subtype", ["/Square", "/Text", "/Highlight"])
def test_insert_pages_from_pdf_preserves_supported_annotations(
    tmp_path: Path,
    direct_annots: bool,
    indirect_annotation: bool,
    include_parent: bool,
    subtype: str,
) -> None:
    target_path = create_simple_text_pdf(tmp_path / "target-annot.pdf", ["A", "B"])
    source_path = create_supported_annotation_source_pdf(
        tmp_path / "source-annot.pdf",
        subtype=subtype,
        include_parent=include_parent,
        direct_annots=direct_annots,
        indirect_annotation=indirect_annotation,
        contents=f"{subtype}-contents",
    )
    service = PdfPageMutationService()
    source_sha_before = file_sha256(source_path)
    source_annotation_before = annotation_details(source_path, page_index=0)

    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)

    imported_annotation = annotation_details(target_path, page_index=1)
    assert len(imported_annotation) == 1
    assert imported_annotation[0]["subtype"] == subtype
    assert imported_annotation[0]["rect"] == source_annotation_before[0]["rect"]
    assert imported_annotation[0]["contents"] == source_annotation_before[0]["contents"]
    assert imported_annotation[0]["has_appearance"] is True
    assert (
        imported_annotation[0]["appearance_fingerprint"]
        == source_annotation_before[0]["appearance_fingerprint"]
    )
    if include_parent:
        assert imported_annotation[0]["parent_objgen"] == imported_annotation[0]["page_objgen"]
    else:
        assert imported_annotation[0]["parent_objgen"] is None
    assert file_sha256(source_path) == source_sha_before
    assert extract_pdfium_text(target_path) == "A Annotated B"

    service.undo_page_insertion(target_path, mutation.receipt)
    assert annotation_details(target_path, page_index=0) == ()
    assert extract_pdfium_text(target_path) == "A B"

    source_path.write_bytes(b"%PDF-1.4\nchanged")
    service.redo_page_insertion(target_path, mutation.receipt)
    redone_annotation = annotation_details(target_path, page_index=1)
    assert redone_annotation == imported_annotation
    assert extract_pdfium_text(target_path) == "A Annotated B"


def test_insert_pages_from_pdf_preserves_multiple_annotations_and_order(tmp_path: Path) -> None:
    target_path = create_simple_text_pdf(tmp_path / "target-multi-annot.pdf", ["A", "B"])
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page.obj["/Contents"] = pdf.make_stream(b"BT /F1 24 Tf 48 96 Td (Annotated) Tj ET")
    page.obj["/Resources"] = _annotation_page_resources(pdf)
    square = _base_annotation_dictionary(pdf, subtype="/Square", contents="first")
    square["/P"] = page.obj
    highlight = _base_annotation_dictionary(pdf, subtype="/Highlight", contents="second")
    source_path = create_annotation_source_pdf_with_entries(
        pdf,
        tmp_path / "source-multi-annot.pdf",
        [pdf.make_indirect(square), highlight],
        direct_annots=False,
        page=page,
    )
    service = PdfPageMutationService()

    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)

    imported_annotations = annotation_details(target_path, page_index=1)
    assert [annotation["subtype"] for annotation in imported_annotations] == [
        "/Square",
        "/Highlight",
    ]
    assert [annotation["contents"] for annotation in imported_annotations] == ["first", "second"]
    service.undo_page_insertion(target_path, mutation.receipt)
    service.redo_page_insertion(target_path, mutation.receipt)
    assert [
        annotation["subtype"]
        for annotation in annotation_details(target_path, page_index=1)
    ] == [
        "/Square",
        "/Highlight",
    ]


def test_insert_pages_from_pdf_rejects_cross_page_annotation_parent(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "target-cross-parent.pdf", 1)
    source_path = create_cross_page_parent_annotation_pdf(tmp_path / "source-cross-parent.pdf")
    service = PdfPageMutationService()
    target_sha_before = file_sha256(target_path)
    source_sha_before = file_sha256(source_path)

    with pytest.raises(PdfPageMutationError, match="他ページを参照する注釈の/P参照は未対応です"):
        service.insert_pages_from_pdf(target_path, source_path, (0,), 1)

    assert file_sha256(target_path) == target_sha_before
    assert file_sha256(source_path) == source_sha_before


def test_insert_pages_from_pdf_rejects_unresolved_annotation_parent(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "target-unresolved-parent.pdf", 1)
    source_path = create_supported_annotation_source_pdf_with_unresolved_parent(
        tmp_path / "source-unresolved-parent.pdf"
    )
    service = PdfPageMutationService()
    target_sha_before = file_sha256(target_path)
    source_sha_before = file_sha256(source_path)

    with pytest.raises(PdfPageMutationError, match="注釈の/P参照を解決できません"):
        service.insert_pages_from_pdf(target_path, source_path, (0,), 1)

    assert file_sha256(target_path) == target_sha_before
    assert file_sha256(source_path) == source_sha_before


@pytest.mark.parametrize(
    ("subtype", "message"),
    [
        ("/Widget", "挿入元PDFのWidget注釈は未対応です"),
        ("/Link", "挿入元PDFのLink注釈は未対応です"),
        ("/FileAttachment", "挿入元PDFのFileAttachment注釈は未対応です"),
        ("/Sound", "挿入元PDFのSound注釈は未対応です"),
        ("/Movie", "挿入元PDFのMovie注釈は未対応です"),
        ("/Screen", "挿入元PDFのScreen注釈は未対応です"),
        ("/RichMedia", "挿入元PDFのRichMedia注釈は未対応です"),
        ("/3D", "挿入元PDFの3D注釈は未対応です"),
        ("/Redact", "挿入元PDFのRedact注釈は未対応です"),
        ("/UnknownCustomAnnotation", "挿入元PDFのUnknownCustomAnnotation注釈は未対応です"),
    ],
)
def test_insert_pages_from_pdf_rejects_unsupported_annotation_subtypes(
    tmp_path: Path,
    subtype: str,
    message: str,
) -> None:
    target_path = create_blank_pdf(tmp_path / "target-reject-subtype.pdf", 1)
    source_path = create_rejected_subtype_annotation_source_pdf(
        tmp_path / f"{subtype.removeprefix('/')}.pdf",
        subtype=subtype,
    )
    service = PdfPageMutationService()
    target_sha_before = file_sha256(target_path)
    source_sha_before = file_sha256(source_path)

    with pytest.raises(PdfPageMutationError, match=message):
        service.insert_pages_from_pdf(target_path, source_path, (0,), 1)

    assert file_sha256(target_path) == target_sha_before
    assert file_sha256(source_path) == source_sha_before
    assert_no_page_insertion_temp_files(target_path)


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("missing_subtype", "挿入元PDFの注釈subtypeが不正です"),
        ("string_subtype", "挿入元PDFの注釈subtypeが不正です"),
        ("non_dictionary_entry", "挿入元PDFの注釈構造が不正です"),
    ],
)
def test_insert_pages_from_pdf_rejects_malformed_annotations(
    tmp_path: Path,
    mode: str,
    message: str,
) -> None:
    target_path = create_blank_pdf(tmp_path / "target-malformed-annot.pdf", 1)
    source_path = create_malformed_annotation_source_pdf(
        tmp_path / f"{mode}.pdf",
        mode=mode,
    )
    service = PdfPageMutationService()
    target_sha_before = file_sha256(target_path)
    source_sha_before = file_sha256(source_path)

    with pytest.raises(PdfPageMutationError, match=message):
        service.insert_pages_from_pdf(target_path, source_path, (0,), 1)

    assert file_sha256(target_path) == target_sha_before
    assert file_sha256(source_path) == source_sha_before
    assert_no_page_insertion_temp_files(target_path)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        (
            "/A",
            pikepdf.Dictionary({"/S": pikepdf.Name("/JavaScript"), "/JS": pikepdf.String("1")}),
            "挿入元PDFのannotation actionは未対応です",
        ),
        (
            "/A",
            pikepdf.Dictionary({"/S": pikepdf.Name("/Launch"), "/F": pikepdf.String("app")}),
            "挿入元PDFのannotation actionは未対応です",
        ),
        (
            "/A",
            pikepdf.Dictionary(
                {"/S": pikepdf.Name("/URI"), "/URI": pikepdf.String("https://example.com")}
            ),
            "挿入元PDFのannotation actionは未対応です",
        ),
        (
            "/A",
            pikepdf.Dictionary({"/S": pikepdf.Name("/GoTo"), "/D": pikepdf.String("dest")}),
            "挿入元PDFのannotation actionは未対応です",
        ),
        ("/A", pikepdf.String("broken"), "挿入元PDFのannotation actionは未対応です"),
        (
            "/AA",
            pikepdf.Dictionary({"/E": pikepdf.Dictionary()}),
            "挿入元PDFのannotation actionは未対応です",
        ),
        ("/Dest", pikepdf.String("dest"), "挿入元PDFのannotation actionは未対応です"),
        ("/FS", pikepdf.String("file"), "挿入元PDFのFileSpec参照付き注釈は未対応です"),
        (
            "/RichMediaContent",
            pikepdf.Dictionary(),
            "挿入元PDFのRichMedia注釈は未対応です",
        ),
        ("/3DD", pikepdf.Dictionary(), "挿入元PDFの3D注釈は未対応です"),
        ("/Sound", pikepdf.Dictionary(), "挿入元PDFのSound注釈は未対応です"),
        ("/Movie", pikepdf.Dictionary(), "挿入元PDFのMovie注釈は未対応です"),
    ],
)
def test_insert_pages_from_pdf_rejects_active_content_annotations(
    tmp_path: Path,
    key: str,
    value: object,
    message: str,
) -> None:
    target_path = create_blank_pdf(tmp_path / "target-active-content.pdf", 1)
    source_path = create_active_content_annotation_source_pdf(
        tmp_path / f"{key.removeprefix('/')}.pdf",
        key=key,
        value=value,
    )
    service = PdfPageMutationService()
    target_sha_before = file_sha256(target_path)
    source_sha_before = file_sha256(source_path)

    with pytest.raises(PdfPageMutationError, match=message):
        service.insert_pages_from_pdf(target_path, source_path, (0,), 1)

    assert file_sha256(target_path) == target_sha_before
    assert file_sha256(source_path) == source_sha_before
    assert_no_page_insertion_temp_files(target_path)


def test_insert_pages_from_pdf_preserves_target_annotations_across_undo_and_redo(
    tmp_path: Path,
) -> None:
    target_path = create_target_with_existing_annotation(tmp_path / "target-existing-annot.pdf")
    source_path = create_supported_annotation_source_pdf(
        tmp_path / "source-existing-annot.pdf",
        subtype="/Square",
        include_parent=True,
        direct_annots=False,
        indirect_annotation=True,
    )
    service = PdfPageMutationService()
    before_snapshot = service.snapshot_document_structure(target_path)
    before_target_annotation = annotation_details(target_path, page_index=1)

    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    after_snapshot = service.snapshot_document_structure(target_path)

    assert after_snapshot.pages[2] == before_snapshot.pages[1]
    imported_target_annotation = annotation_details(target_path, page_index=2)
    assert len(imported_target_annotation) == 1
    assert imported_target_annotation[0]["subtype"] == before_target_annotation[0]["subtype"]
    assert imported_target_annotation[0]["rect"] == before_target_annotation[0]["rect"]
    assert imported_target_annotation[0]["contents"] == before_target_annotation[0]["contents"]
    assert imported_target_annotation[0]["has_appearance"] is True
    assert (
        imported_target_annotation[0]["appearance_fingerprint"]
        == before_target_annotation[0]["appearance_fingerprint"]
    )
    assert (
        imported_target_annotation[0]["parent_objgen"]
        == imported_target_annotation[0]["page_objgen"]
    )

    service.undo_page_insertion(target_path, mutation.receipt)
    assert service.snapshot_document_structure(target_path) == before_snapshot

    source_path.unlink()
    service.redo_page_insertion(target_path, mutation.receipt)
    redone_snapshot = service.snapshot_document_structure(target_path)
    assert redone_snapshot.pages[2] == before_snapshot.pages[1]
    redone_target_annotation = annotation_details(target_path, page_index=2)
    assert redone_target_annotation == imported_target_annotation


def test_insert_pages_from_pdf_annotation_copy_failure_preserves_working_copy_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_path = create_blank_pdf(tmp_path / "target-annot-copy-fail.pdf", 1)
    source_path = create_supported_annotation_source_pdf(
        tmp_path / "source-annot-copy-fail.pdf",
        subtype="/Square",
        include_parent=True,
        direct_annots=False,
        indirect_annotation=True,
    )
    service = PdfPageMutationService()
    target_sha_before = file_sha256(target_path)
    source_sha_before = file_sha256(source_path)

    def fail_copy(*_args: object, **_kwargs: object) -> object:
        raise PdfPageMutationError("注釈オブジェクトのコピーに失敗しました")

    monkeypatch.setattr(service, "_copy_single_imported_annotation", fail_copy)

    with pytest.raises(PdfPageMutationError, match="注釈オブジェクトのコピーに失敗しました"):
        service.insert_pages_from_pdf(target_path, source_path, (0,), 1)

    assert file_sha256(target_path) == target_sha_before
    assert file_sha256(source_path) == source_sha_before
    assert list(target_path.parent.glob(".target-annot-copy-fail.insert-*.pdf")) == []


def test_insert_pages_from_pdf_preserves_mixed_annotation_pages_across_redo(
    tmp_path: Path,
) -> None:
    target_path = create_simple_text_pdf(tmp_path / "target-mixed-annot.pdf", ["A", "B"])
    source_path = create_mixed_annotation_source_pdf(tmp_path / "source-mixed-annot.pdf")
    service = PdfPageMutationService()
    source_sha_before = file_sha256(source_path)

    mutation = service.insert_pages_from_pdf(target_path, source_path, (0, 1), 1)

    assert extract_pdfium_text(target_path) == "A Annotated Plain B"
    assert len(annotation_details(target_path, page_index=1)) == 1
    assert annotation_details(target_path, page_index=2) == ()
    imported_annotation = annotation_details(target_path, page_index=1)

    service.undo_page_insertion(target_path, mutation.receipt)
    assert extract_pdfium_text(target_path) == "A B"

    source_path.unlink()
    service.redo_page_insertion(target_path, mutation.receipt)
    assert extract_pdfium_text(target_path) == "A Annotated Plain B"
    assert annotation_details(target_path, page_index=1) == imported_annotation
    assert annotation_details(target_path, page_index=2) == ()
    assert (
        file_sha256(mutation.receipt.source_snapshot_path)
        == mutation.receipt.source_snapshot_sha256
    )
    assert source_sha_before != file_sha256(target_path)


def test_insert_pages_from_pdf_executes_undoes_and_redoes_exact_order(tmp_path: Path) -> None:
    target_path = create_simple_text_pdf(tmp_path / "target.pdf", ["A", "B", "C", "D"])
    source_path = create_simple_text_pdf(tmp_path / "source.pdf", ["X", "Y", "Z"])
    service = PdfPageMutationService()
    target_source_sha = file_sha256(target_path)
    import_source_sha = file_sha256(source_path)

    mutation = service.insert_pages_from_pdf(target_path, source_path, (0, 2), 2)

    assert extract_pdfium_text(target_path) == "A B X Z C D"
    assert mutation.receipt.inserted_page_indexes == (2, 3)
    assert mutation.mutation_result.page_index_transition is not None
    assert mutation.mutation_result.page_index_transition.cache_old_to_new == (0, 1, 4, 5)
    assert file_sha256(source_path) == import_source_sha

    service.undo_page_insertion(target_path, mutation.receipt)
    assert extract_pdfium_text(target_path) == "A B C D"

    service.redo_page_insertion(target_path, mutation.receipt)
    assert extract_pdfium_text(target_path) == "A B X Z C D"
    assert file_sha256(source_path) == import_source_sha
    assert file_sha256(target_path) != target_source_sha


def test_insert_pages_from_pdf_preserves_target_document_metadata_and_attachments(
    tmp_path: Path,
) -> None:
    target_path = create_outline_attachment_pdf(
        tmp_path / "target-outline.pdf",
        title="Target",
        attachment_name="target.txt",
    )
    source_path = create_outline_attachment_pdf(
        tmp_path / "source-outline.pdf",
        title="Source",
        attachment_name="source.txt",
    )
    service = PdfPageMutationService()

    before_target = service.snapshot_document_structure(target_path)
    source_snapshot = service.snapshot_document_structure(source_path)

    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    after_target = service.snapshot_document_structure(target_path)

    assert after_target.metadata_fingerprint == before_target.metadata_fingerprint
    assert after_target.attachments_fingerprint == before_target.attachments_fingerprint
    assert tuple(item.destination_page_index for item in after_target.named_destinations) == (2,)
    assert after_target.outlines[0].title == before_target.outlines[0].title
    assert after_target.metadata_fingerprint != source_snapshot.metadata_fingerprint

    service.undo_page_insertion(target_path, mutation.receipt)
    assert service.snapshot_document_structure(target_path) == before_target


def test_page_insertion_redo_uses_frozen_source_snapshot_after_external_source_changes(
    tmp_path: Path,
) -> None:
    target_path = create_simple_text_pdf(tmp_path / "target-redo.pdf", ["A", "B"])
    source_path = create_simple_text_pdf(tmp_path / "source-redo.pdf", ["X", "Y"])
    service = PdfPageMutationService()

    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    service.undo_page_insertion(target_path, mutation.receipt)
    source_path.write_bytes(b"%PDF-1.4\nbroken")

    service.redo_page_insertion(target_path, mutation.receipt)

    assert extract_pdfium_text(target_path) == "A X B"


def test_page_insertion_rejects_tampered_source_snapshot_on_redo(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "target-tamper.pdf", 2)
    source_path = create_blank_pdf(tmp_path / "source-tamper.pdf", 1)
    service = PdfPageMutationService()

    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    service.undo_page_insertion(target_path, mutation.receipt)
    mutation.receipt.source_snapshot_path.write_bytes(b"%PDF-1.4\nbroken")

    with pytest.raises(PdfPageMutationError, match="整合性検証"):
        service.redo_page_insertion(target_path, mutation.receipt)


def test_discard_page_insertion_receipt_removes_both_snapshots(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "target-dispose.pdf", 2)
    source_path = create_blank_pdf(tmp_path / "source-dispose.pdf", 1)
    service = PdfPageMutationService()

    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)

    assert mutation.receipt.source_snapshot_path.exists()
    assert mutation.receipt.target_undo_snapshot_path.exists()

    service.discard_page_insertion_receipt(target_path, mutation.receipt)

    assert not mutation.receipt.source_snapshot_path.exists()
    assert not mutation.receipt.target_undo_snapshot_path.exists()


def test_insert_pages_from_pdf_failure_preserves_working_copy_sha(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "target-fail.pdf", 2)
    source_path = create_blank_pdf(tmp_path / "source-fail.pdf", 1)
    before_sha = file_sha256(target_path)
    service = PdfPageMutationService()

    def raise_before_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("replace blocked")

    original_replace = service._replace_atomically
    service._replace_atomically = raise_before_replace  # type: ignore[method-assign]
    try:
        with pytest.raises(PdfPageMutationError, match="更新に失敗"):
            service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    finally:
        service._replace_atomically = original_replace  # type: ignore[method-assign]

    assert file_sha256(target_path) == before_sha


def test_insert_pages_from_pdf_materializes_inherited_cropbox_and_resources(
    tmp_path: Path,
) -> None:
    target_path = create_blank_pdf(tmp_path / "target-inherited.pdf", 1)
    source_path = create_inherited_crop_resources_pdf(tmp_path / "source-inherited.pdf")
    service = PdfPageMutationService()

    source_snapshot = service.snapshot_document_structure(source_path)
    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    target_snapshot = service.snapshot_document_structure(target_path)
    imported_page = target_snapshot.pages[1]
    source_page = source_snapshot.pages[0]

    assert imported_page.boxes.media_box == source_page.boxes.media_box
    assert imported_page.boxes.crop_box == source_page.boxes.crop_box
    assert imported_page.boxes.crop_box_direct_present is True
    assert imported_page.boxes.crop_box_inherited is False
    assert imported_page.direct_resources_present is True
    assert imported_page.resources_fingerprint == source_page.resources_fingerprint
    assert imported_page == service._expected_imported_page_snapshot(source_page)
    assert mutation.receipt.source_selected_page_snapshots[0].resources_fingerprint == (
        source_page.resources_fingerprint
    )


def test_insert_pages_from_pdf_rejects_source_snapshot_sha_mismatch_and_cleans_up(
    tmp_path: Path,
) -> None:
    target_path = create_blank_pdf(tmp_path / "target-sha.pdf", 1)
    source_path = create_simple_text_pdf(tmp_path / "source-sha.pdf", ["Alpha"])
    service = PdfPageMutationService()
    before_target_sha = file_sha256(target_path)

    original_copy_named_snapshot = service._copy_named_snapshot  # type: ignore[attr-defined]

    def corrupting_copy(*args: object, **kwargs: object) -> None:
        original_copy_named_snapshot(*args, **kwargs)
        snapshot_path = args[1]
        assert isinstance(snapshot_path, Path)
        snapshot_path.write_bytes(b"%PDF-1.4\ncorrupted")

    service._copy_named_snapshot = corrupting_copy  # type: ignore[method-assign]
    try:
        with pytest.raises(PdfPageMutationError, match="整合性検証"):
            service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    finally:
        service._copy_named_snapshot = original_copy_named_snapshot  # type: ignore[method-assign]

    assert file_sha256(target_path) == before_target_sha
    assert list(target_path.parent.glob(".target-sha.insert-*.pdf")) == []


def test_page_insertion_receipt_rejects_tampered_execute_current_mapping(
    tmp_path: Path,
) -> None:
    target_path = create_simple_text_pdf(tmp_path / "target-receipt.pdf", ["A", "B"])
    source_path = create_simple_text_pdf(tmp_path / "source-receipt.pdf", ["X"])
    service = PdfPageMutationService()
    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    receipt = mutation.receipt

    with pytest.raises(ValueError, match="execute_transition current_page_old_to_new is invalid"):
        clone_receipt(
            receipt,
            execute_transition=PageIndexTransition(
                old_page_count=receipt.execute_transition.old_page_count,
                new_page_count=receipt.execute_transition.new_page_count,
                cache_old_to_new=receipt.execute_transition.cache_old_to_new,
                current_page_old_to_new=(1, 2),
            ),
        )


def test_page_insertion_receipt_rejects_tampered_undo_transition(tmp_path: Path) -> None:
    target_path = create_simple_text_pdf(tmp_path / "target-undo-receipt.pdf", ["A", "B"])
    source_path = create_simple_text_pdf(tmp_path / "source-undo-receipt.pdf", ["X"])
    service = PdfPageMutationService()
    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    receipt = mutation.receipt

    with pytest.raises(ValueError, match="undo_transition current_page_old_to_new is invalid"):
        clone_receipt(
            receipt,
            undo_transition=PageIndexTransition(
                old_page_count=receipt.undo_transition.old_page_count,
                new_page_count=receipt.undo_transition.new_page_count,
                cache_old_to_new=receipt.undo_transition.cache_old_to_new,
                current_page_old_to_new=(0, 0, 1),
            ),
        )


def test_page_insertion_receipt_rejects_identical_snapshot_paths(tmp_path: Path) -> None:
    target_path = create_simple_text_pdf(tmp_path / "target-identical-snapshots.pdf", ["A", "B"])
    source_path = create_simple_text_pdf(tmp_path / "source-identical-snapshots.pdf", ["X"])
    service = PdfPageMutationService()
    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    receipt = mutation.receipt

    with pytest.raises(ValueError, match="snapshot paths must differ"):
        clone_receipt(receipt, target_undo_snapshot_path=receipt.source_snapshot_path)


def test_validate_insert_source_snapshot_ownership_rejects_wrong_prefix(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "target-ownership.pdf", 1)
    source_path = create_blank_pdf(tmp_path / "source-ownership.pdf", 1)
    service = PdfPageMutationService()
    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 0)
    receipt = clone_receipt(
        mutation.receipt,
        source_snapshot_path=target_path.parent / ".wrong-prefix.insert-source.fake.pdf",
    )

    with pytest.raises(PdfPageMutationError, match="場所が不正"):
        service._validate_insert_source_snapshot_ownership(
            target_path,
            receipt,
            require_exists=False,
        )


def test_validate_insert_target_undo_snapshot_ownership_rejects_symlink(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "target-symlink.pdf", 1)
    source_path = create_blank_pdf(tmp_path / "source-symlink.pdf", 1)
    service = PdfPageMutationService()
    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 0)
    symlink_path = target_path.parent / f".{target_path.stem}.insert-undo.symlink.pdf"
    symlink_path.symlink_to(mutation.receipt.target_undo_snapshot_path)
    receipt = clone_receipt(mutation.receipt, target_undo_snapshot_path=symlink_path)

    with pytest.raises(PdfPageMutationError, match="場所が不正"):
        service._validate_insert_target_undo_snapshot_ownership(
            target_path,
            receipt,
            require_exists=False,
        )


def test_discard_page_insertion_receipt_attempts_both_deletions_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_path = create_blank_pdf(tmp_path / "target-discard-retry.pdf", 2)
    source_path = create_blank_pdf(tmp_path / "source-discard-retry.pdf", 1)
    service = PdfPageMutationService()
    mutation = service.insert_pages_from_pdf(target_path, source_path, (0,), 1)
    receipt = mutation.receipt
    calls: list[Path] = []
    original_unlink = Path.unlink
    failed_once = {receipt.source_snapshot_path}

    def flaky_unlink(path: Path, *args: object, **kwargs: object) -> None:
        calls.append(path)
        if path in failed_once:
            failed_once.remove(path)
            raise OSError("source busy")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with pytest.raises(PdfPageMutationError, match="削除に失敗"):
        service.discard_page_insertion_receipt(target_path, receipt)

    assert receipt.source_snapshot_path.exists() is True
    assert receipt.target_undo_snapshot_path.exists() is False
    assert calls == [receipt.source_snapshot_path, receipt.target_undo_snapshot_path]

    service.discard_page_insertion_receipt(target_path, receipt)

    assert receipt.source_snapshot_path.exists() is False
    assert receipt.target_undo_snapshot_path.exists() is False


def test_validate_insert_source_snapshot_renders_only_selected_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = create_blank_pdf(tmp_path / "source-selected-render.pdf", 1000)
    service = PdfPageMutationService()
    rendered: list[tuple[int, ...]] = []

    def fake_render_page_digests(
        _path: Path,
        page_indexes: list[int] | tuple[int, ...],
    ) -> dict[int, tuple[int, int, str]]:
        normalized = tuple(page_indexes)
        rendered.append(normalized)
        return {page_index: (1, 1, f"digest-{page_index}") for page_index in normalized}

    monkeypatch.setattr(service, "_render_page_digests", fake_render_page_digests)

    service._validate_insert_source_snapshot_path(
        source_path,
        expected_page_count=1000,
        render_page_indexes=(12, 600, 12),
    )

    assert rendered == [(12, 600)]


def test_validate_insert_target_undo_snapshot_renders_only_boundary_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_path = create_blank_pdf(tmp_path / "target-boundary-render.pdf", 1000)
    service = PdfPageMutationService()
    rendered: list[tuple[int, ...]] = []

    def fake_render_page_digests(
        _path: Path,
        page_indexes: list[int] | tuple[int, ...],
    ) -> dict[int, tuple[int, int, str]]:
        normalized = tuple(page_indexes)
        rendered.append(normalized)
        return {page_index: (1, 1, f"digest-{page_index}") for page_index in normalized}

    monkeypatch.setattr(service, "_render_page_digests", fake_render_page_digests)

    service._validate_insert_target_undo_snapshot_path(
        target_path,
        expected_page_count=1000,
        render_page_indexes=service._target_snapshot_validation_page_indexes(1000, 500),
    )

    assert rendered == [(0, 499, 500, 999)]
