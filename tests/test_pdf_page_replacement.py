from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pikepdf
import pytest
from pikepdf import Name

from pdf_regression_utils import extract_pdfium_text, file_sha256
from pdf_test_utils import create_blank_pdf
from pdf_workbench.domain.mutation import PageIndexTransition
from pdf_workbench.services.pdf_page_mutation import (
    PageReplacementReceipt,
    PdfPageMutationError,
    PdfPageMutationService,
    SourcePdfRevision,
)
from test_pdf_page_insertion import (
    annotation_details,
    create_outline_attachment_pdf,
    create_supported_annotation_source_pdf,
    create_target_with_existing_annotation,
)


def assert_no_page_replacement_temp_files(target_path: Path) -> None:
    assert list(target_path.parent.glob(f".{target_path.stem}.replace-source.*.pdf")) == []
    assert list(target_path.parent.glob(f".{target_path.stem}.replace-undo.*.pdf")) == []
    assert list(target_path.parent.glob(f".{target_path.stem}.mutation.*.tmp.pdf")) == []


def create_resource_collision_pdf(
    path: Path,
    *,
    text: str,
    font_name: str,
    image_rgb: tuple[int, int, int],
    form_text: str,
) -> Path:
    create_replacement_text_pdf(path, [text])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        resources = page.obj["/Resources"]
        font = resources["/Font"]["/F1"]
        font["/BaseFont"] = Name(f"/{font_name}")

        image_stream = pikepdf.Stream(
            pdf,
            bytes(image_rgb),
            {
                "/Type": Name("/XObject"),
                "/Subtype": Name("/Image"),
                "/Width": 1,
                "/Height": 1,
                "/ColorSpace": Name("/DeviceRGB"),
                "/BitsPerComponent": 8,
            },
        )
        form_stream = pikepdf.Stream(
            pdf,
            f"BT /F1 12 Tf 0 18 Td ({form_text}) Tj ET".encode("latin-1"),
            {
                "/Type": Name("/XObject"),
                "/Subtype": Name("/Form"),
                "/BBox": pikepdf.Array([0, 0, 60, 24]),
                "/Resources": pikepdf.Dictionary(
                    {"/Font": pikepdf.Dictionary({"/F1": resources["/Font"]["/F1"]})}
                ),
            },
        )
        resources["/XObject"] = pikepdf.Dictionary(
            {
                "/Im1": pdf.make_indirect(image_stream),
                "/X1": pdf.make_indirect(form_stream),
            }
        )
        page.obj["/Contents"] = pdf.make_indirect(
            pikepdf.Stream(
                pdf,
                (
                    "q 40 0 0 40 18 118 cm /Im1 Do Q\n"
                    "q 1 0 0 1 72 118 cm /X1 Do Q\n"
                    f"BT /F1 18 Tf 40 74 Td ({text}) Tj ET\n"
                ).encode("latin-1"),
            )
        )
        pdf.save(path)
    return path


def create_replacement_text_pdf(path: Path, pages: list[str]) -> Path:
    create_blank_pdf(path, len(pages))
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for page, text in zip(pdf.pages, pages, strict=True):
            page.obj["/Resources"] = pikepdf.Dictionary(
                {
                    "/Font": pikepdf.Dictionary(
                        {
                            "/F1": pikepdf.Dictionary(
                                {
                                    "/Type": Name("/Font"),
                                    "/Subtype": Name("/Type1"),
                                    "/BaseFont": Name("/Helvetica"),
                                }
                            )
                        }
                    )
                }
            )
            page.obj["/Contents"] = pdf.make_indirect(
                pikepdf.Stream(
                    pdf,
                    f"BT /F1 18 Tf 40 100 Td ({text}) Tj ET".encode("latin-1"),
                )
            )
        pdf.save(path)
    return path


def add_direct_page_key(path: Path, page_index: int, key: str) -> None:
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[page_index].obj[Name(key)] = Name("/R")
        pdf.save(path)


def test_read_source_pdf_revision_and_validate_expected_source_revision(tmp_path: Path) -> None:
    source_path = create_replacement_text_pdf(tmp_path / "source-revision.pdf", ["A", "B"])
    service = PdfPageMutationService()

    revision = service.read_source_pdf_revision(source_path)

    assert revision.resolved_path == source_path.resolve()
    assert revision.page_count == 2
    assert revision.sha256
    service._validate_expected_source_revision(
        source_path.resolve(),
        revision,
        operation_label="置換元",
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda revision, path: SourcePdfRevision(
                resolved_path=path.parent / "other.pdf",
                fingerprint=revision.fingerprint,
                sha256=revision.sha256,
                page_count=revision.page_count,
            ),
            "置換元PDFのパスが変化しました",
        ),
        (
            lambda revision, path: SourcePdfRevision(
                resolved_path=revision.resolved_path,
                fingerprint="different",
                sha256=revision.sha256,
                page_count=revision.page_count,
            ),
            "置換元PDFが変更されました",
        ),
        (
            lambda revision, path: SourcePdfRevision(
                resolved_path=revision.resolved_path,
                fingerprint=revision.fingerprint,
                sha256="0" * 64,
                page_count=revision.page_count,
            ),
            "置換元PDFが変更されました",
        ),
        (
            lambda revision, path: SourcePdfRevision(
                resolved_path=revision.resolved_path,
                fingerprint=revision.fingerprint,
                sha256=revision.sha256,
                page_count=revision.page_count + 1,
            ),
            "置換元PDFのページ数が変化しました",
        ),
    ],
)
def test_validate_expected_source_revision_rejects_drift(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    source_path = create_replacement_text_pdf(tmp_path / "source-drift.pdf", ["A", "B"])
    service = PdfPageMutationService()
    revision = service.read_source_pdf_revision(source_path)

    mutated_revision = mutate(revision, source_path.resolve())  # type: ignore[operator]

    with pytest.raises(PdfPageMutationError, match=message):
        service._validate_expected_source_revision(
            source_path.resolve(),
            mutated_revision,
            operation_label="置換元",
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"resolved_path": Path("relative.pdf")}, "resolved_path must be absolute"),
        (
            {"resolved_path": Path("not-a-pdf.txt")},
            "resolved_path must refer to a PDF",
        ),
        ({"page_count": True}, "page_count must be an integer"),
        ({"page_count": 0}, "page_count must be positive"),
        ({"sha256": "abc"}, "sha256 must be a lowercase SHA-256 hex digest"),
        (
            {"sha256": "A" * 64},
            "sha256 must be a lowercase SHA-256 hex digest",
        ),
    ],
)
def test_source_pdf_revision_rejects_invalid_invariants(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    valid = dict(
        resolved_path=(tmp_path / "source.pdf").resolve(),
        fingerprint="fingerprint",
        sha256="a" * 64,
        page_count=1,
    )
    if kwargs.get("resolved_path") == Path("not-a-pdf.txt"):
        kwargs = {**kwargs, "resolved_path": (tmp_path / "not-a-pdf.txt").resolve()}
    valid.update(kwargs)

    with pytest.raises(ValueError, match=message):
        SourcePdfRevision(**valid)


def test_validate_supported_replacement_source_page_keys_accepts_allowlist(tmp_path: Path) -> None:
    source_path = create_replacement_text_pdf(tmp_path / "allowed-keys.pdf", ["A"])
    service = PdfPageMutationService()

    with pikepdf.open(source_path) as pdf:
        service._validate_supported_replacement_source_page_keys(
            pdf.pages[0].obj,
            operation_label="置換元",
        )


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("/B", "置換元PDFのページに未対応の/Bがあります"),
        ("/Dur", "置換元PDFのページに未対応の/Durがあります"),
        ("/Trans", "置換元PDFのページに未対応の/Transがあります"),
        ("/Thumb", "置換元PDFのページに未対応の/Thumbがあります"),
        ("/Metadata", "置換元PDFのページに未対応の/Metadataがあります"),
        ("/PieceInfo", "置換元PDFのページに未対応の/PieceInfoがあります"),
        ("/StructParents", "置換元PDFのページに未対応の/StructParentsがあります"),
        ("/ID", "置換元PDFのページに未対応の/IDがあります"),
        ("/PZ", "置換元PDFのページに未対応の/PZがあります"),
        ("/SeparationInfo", "置換元PDFのページに未対応の/SeparationInfoがあります"),
        ("/Tabs", "置換元PDFのページに未対応の/Tabsがあります"),
        (
            "/TemplateInstantiated",
            "置換元PDFのページに未対応の/TemplateInstantiatedがあります",
        ),
        ("/PresSteps", "置換元PDFのページに未対応の/PresStepsがあります"),
        ("/UserUnit", "置換元PDFのページに未対応の/UserUnitがあります"),
        ("/VP", "置換元PDFのページに未対応の/VPがあります"),
        ("/CustomKey", "置換元PDFのページに未対応の/CustomKeyがあります"),
    ],
)
def test_validate_supported_replacement_source_page_keys_rejects_page_level_unsupported_keys(
    tmp_path: Path,
    key: str,
    message: str,
) -> None:
    source_path = create_replacement_text_pdf(tmp_path / "unsupported-page-key.pdf", ["A"])
    service = PdfPageMutationService()

    with pikepdf.open(source_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj[Name(key)] = Name("/Value")
        pdf.save(source_path)
    with pikepdf.open(source_path) as pdf, pytest.raises(PdfPageMutationError, match=message):
        service._validate_supported_replacement_source_page_keys(
            pdf.pages[0].obj,
            operation_label="置換元",
        )


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("/AA", "置換元PDFのページアクションは未対応です"),
        ("/Metadata", "置換元PDFのページに未対応の/Metadataがあります"),
    ],
)
def test_validate_supported_replacement_source_page_keys_rejects_unsupported_keys(
    tmp_path: Path,
    key: str,
    message: str,
) -> None:
    source_path = create_replacement_text_pdf(tmp_path / "unsupported-keys.pdf", ["A"])
    service = PdfPageMutationService()

    with pikepdf.open(source_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj[Name(key)] = Name("/Value")
        pdf.save(source_path)
    with pikepdf.open(source_path) as pdf, pytest.raises(PdfPageMutationError, match=message):
        service._validate_supported_replacement_source_page_keys(
            pdf.pages[0].obj,
            operation_label="置換元",
        )


def test_clear_replacement_target_page_dictionary_removes_disallowed_keys(tmp_path: Path) -> None:
    target_path = create_replacement_text_pdf(tmp_path / "target-clear.pdf", ["A"])
    service = PdfPageMutationService()

    with pikepdf.open(target_path, allow_overwriting_input=True) as pdf:
        page_object = pdf.pages[0].obj
        page_object[Name("/Metadata")] = Name("/Meta")
        page_object[Name("/VP")] = Name("/Viewport")
        page_object[Name("/Custom")] = Name("/CustomValue")
        service._clear_replacement_target_page_dictionary(page_object)
        assert set(map(str, page_object.keys())) == {"/Type", "/Parent"}


def test_copy_replacement_page_contents_and_resources_validate_shapes(tmp_path: Path) -> None:
    source_path = create_replacement_text_pdf(tmp_path / "copy-shapes.pdf", ["A"])
    target_path = create_replacement_text_pdf(tmp_path / "copy-target.pdf", ["B"])
    service = PdfPageMutationService()

    with (
        pikepdf.open(target_path, allow_overwriting_input=True) as target_pdf,
        pikepdf.open(
            source_path,
            allow_overwriting_input=True,
        ) as source_pdf,
    ):
        source_page = source_pdf.pages[0]
        copied_contents = service._copy_replacement_page_contents(
            target_pdf,
            source_pdf,
            source_page=source_page,
        )
        copied_resources = service._copy_effective_page_resources(
            target_pdf,
            source_pdf,
            source_page=source_page,
        )
        assert copied_contents is not None
        assert copied_resources is not None
        assert copied_contents.objgen != source_page.obj["/Contents"].objgen
        assert copied_resources.objgen != source_page.obj["/Resources"].objgen

        source_page.obj["/Contents"] = Name("/Invalid")
        with pytest.raises(PdfPageMutationError, match="/Contents構造が不正です"):
            service._copy_replacement_page_contents(
                target_pdf,
                source_pdf,
                source_page=source_page,
            )
        source_page.obj["/Contents"] = pikepdf.Array()
        source_page.obj["/Resources"] = Name("/Invalid")
        with pytest.raises(PdfPageMutationError, match="/Resources構造が不正です"):
            service._copy_effective_page_resources(
                target_pdf,
                source_pdf,
                source_page=source_page,
            )


def test_materialize_replacement_page_structure_sets_allowed_keys_only(tmp_path: Path) -> None:
    source_path = create_replacement_text_pdf(tmp_path / "source-materialize.pdf", ["A"])
    target_path = create_replacement_text_pdf(tmp_path / "target-materialize.pdf", ["B"])
    service = PdfPageMutationService()

    with (
        pikepdf.open(source_path) as source_pdf,
        pikepdf.open(
            target_path,
            allow_overwriting_input=True,
        ) as target_pdf,
    ):
        source_snapshot = service.snapshot_document_structure(source_path).pages[0]
        target_page = target_pdf.pages[0]
        copied_contents = service._copy_replacement_page_contents(
            target_pdf,
            source_pdf,
            source_page=source_pdf.pages[0],
        )
        copied_resources = service._copy_effective_page_resources(
            target_pdf,
            source_pdf,
            source_page=source_pdf.pages[0],
        )
        service._clear_replacement_target_page_dictionary(target_page.obj)
        service._materialize_replacement_page_structure(
            target_page,
            source_snapshot,
            copied_contents=copied_contents,
            copied_resources=copied_resources,
        )

        keys = set(map(str, target_page.obj.keys()))
        assert keys == {"/Type", "/Parent", "/Contents", "/Resources", "/MediaBox", "/CropBox"}


def create_valid_page_replacement_receipt(tmp_path: Path) -> PageReplacementReceipt:
    target_path = create_replacement_text_pdf(tmp_path / "receipt-target.pdf", ["A", "B"])
    source_path = create_replacement_text_pdf(tmp_path / "receipt-source.pdf", ["X"])
    service = PdfPageMutationService()
    mutation = service.replace_pages_from_pdf(target_path, source_path, (1,), (0,))
    return mutation.receipt


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda receipt: replace(receipt, working_copy_path=Path("relative.pdf")),
            "working_copy_path must be absolute",
        ),
        (
            lambda receipt: replace(
                receipt,
                working_copy_path=receipt.working_copy_path.with_suffix(".txt"),
            ),
            "working_copy_path must point to a PDF",
        ),
        (
            lambda receipt: replace(receipt, target_page_count_before=0),
            "target_page_count_before must be positive",
        ),
        (
            lambda receipt: replace(
                receipt,
                target_before_snapshot=replace(
                    receipt.target_before_snapshot,
                    page_count=receipt.target_page_count_before + 1,
                ),
            ),
            "target_before_snapshot page count must match target_page_count_before",
        ),
        (
            lambda receipt: replace(
                receipt,
                target_after_snapshot=replace(
                    receipt.target_after_snapshot,
                    page_count=receipt.target_page_count_before + 1,
                ),
            ),
            "target_after_snapshot page count must stay unchanged",
        ),
        (
            lambda receipt: replace(receipt, source_snapshot_path=Path("relative.pdf")),
            "source_snapshot_path must be absolute",
        ),
        (
            lambda receipt: replace(receipt, target_undo_snapshot_path=Path("relative.pdf")),
            "target_undo_snapshot_path must be absolute",
        ),
        (
            lambda receipt: replace(
                receipt,
                source_snapshot_path=receipt.working_copy_path,
            ),
            "source_snapshot_path must differ from working_copy_path",
        ),
        (
            lambda receipt: replace(
                receipt,
                target_undo_snapshot_path=receipt.working_copy_path,
            ),
            "target_undo_snapshot_path must differ from working_copy_path",
        ),
        (
            lambda receipt: replace(
                receipt,
                target_undo_snapshot_path=receipt.source_snapshot_path,
            ),
            "snapshot paths must differ",
        ),
        (
            lambda receipt: replace(receipt, source_snapshot_sha256="bad"),
            "source_snapshot_sha256 must be a lowercase SHA-256 hex digest",
        ),
        (
            lambda receipt: replace(receipt, target_undo_snapshot_sha256="bad"),
            "target_undo_snapshot_sha256 must be a lowercase SHA-256 hex digest",
        ),
        (
            lambda receipt: replace(receipt, source_snapshot_page_count=True),
            "source_snapshot_page_count must be an integer",
        ),
        (
            lambda receipt: replace(receipt, source_snapshot_page_count=0),
            "source_snapshot_page_count must be positive",
        ),
        (
            lambda receipt: replace(receipt, source_selected_page_snapshots=()),
            "source_selected_page_snapshots length must match source_page_indexes",
        ),
        (
            lambda receipt: replace(receipt, replacement_pairs=((0, 0),)),
            "replacement_pairs does not match the replacement plan",
        ),
        (
            lambda receipt: replace(receipt, replaced_page_indexes_after=(0,)),
            "replaced_page_indexes_after does not match the replacement plan",
        ),
        (
            lambda receipt: replace(
                receipt,
                execute_transition=PageIndexTransition(
                    old_page_count=receipt.target_page_count_before + 1,
                    new_page_count=receipt.target_page_count_before + 1,
                    cache_old_to_new=(*receipt.execute_transition.cache_old_to_new, 2),
                    current_page_old_to_new=(
                        *receipt.execute_transition.current_page_old_to_new,
                        2,
                    ),
                ),
            ),
            "execute_transition old_page_count is invalid",
        ),
        (
            lambda receipt: replace(
                receipt,
                execute_transition=PageIndexTransition(
                    old_page_count=receipt.execute_transition.old_page_count,
                    new_page_count=receipt.execute_transition.new_page_count + 1,
                    cache_old_to_new=receipt.execute_transition.cache_old_to_new,
                    current_page_old_to_new=receipt.execute_transition.current_page_old_to_new,
                ),
            ),
            "execute_transition new_page_count is invalid",
        ),
        (
            lambda receipt: replace(
                receipt,
                execute_transition=PageIndexTransition(
                    old_page_count=receipt.execute_transition.old_page_count,
                    new_page_count=receipt.execute_transition.new_page_count,
                    cache_old_to_new=(0, 1),
                    current_page_old_to_new=receipt.execute_transition.current_page_old_to_new,
                ),
            ),
            "execute_transition cache_old_to_new is invalid",
        ),
        (
            lambda receipt: replace(
                receipt,
                execute_transition=PageIndexTransition(
                    old_page_count=receipt.execute_transition.old_page_count,
                    new_page_count=receipt.execute_transition.new_page_count,
                    cache_old_to_new=receipt.execute_transition.cache_old_to_new,
                    current_page_old_to_new=(1, 0),
                ),
            ),
            "execute_transition current_page_old_to_new is invalid",
        ),
        (
            lambda receipt: replace(
                receipt,
                undo_transition=PageIndexTransition(
                    old_page_count=receipt.target_page_count_before + 1,
                    new_page_count=receipt.target_page_count_before + 1,
                    cache_old_to_new=(*receipt.undo_transition.cache_old_to_new, 2),
                    current_page_old_to_new=(
                        *receipt.undo_transition.current_page_old_to_new,
                        2,
                    ),
                ),
            ),
            "undo_transition old_page_count is invalid",
        ),
        (
            lambda receipt: replace(
                receipt,
                undo_transition=PageIndexTransition(
                    old_page_count=receipt.undo_transition.old_page_count,
                    new_page_count=receipt.undo_transition.new_page_count + 1,
                    cache_old_to_new=receipt.undo_transition.cache_old_to_new,
                    current_page_old_to_new=receipt.undo_transition.current_page_old_to_new,
                ),
            ),
            "undo_transition new_page_count is invalid",
        ),
        (
            lambda receipt: replace(
                receipt,
                undo_transition=PageIndexTransition(
                    old_page_count=receipt.undo_transition.old_page_count,
                    new_page_count=receipt.undo_transition.new_page_count,
                    cache_old_to_new=(0, 1),
                    current_page_old_to_new=receipt.undo_transition.current_page_old_to_new,
                ),
            ),
            "undo_transition cache_old_to_new is invalid",
        ),
        (
            lambda receipt: replace(
                receipt,
                undo_transition=PageIndexTransition(
                    old_page_count=receipt.undo_transition.old_page_count,
                    new_page_count=receipt.undo_transition.new_page_count,
                    cache_old_to_new=receipt.undo_transition.cache_old_to_new,
                    current_page_old_to_new=(1, 0),
                ),
            ),
            "undo_transition current_page_old_to_new is invalid",
        ),
    ],
)
def test_page_replacement_receipt_rejects_invalid_invariants(
    tmp_path: Path,
    mutate: object,
    message: str,
) -> None:
    receipt = create_valid_page_replacement_receipt(tmp_path)

    with pytest.raises(ValueError, match=message):
        mutate(receipt)  # type: ignore[misc,operator]


def test_replace_pages_from_pdf_executes_undoes_and_redoes_exact_order(tmp_path: Path) -> None:
    target_path = create_replacement_text_pdf(tmp_path / "target.pdf", ["A", "B", "C", "D"])
    source_path = create_replacement_text_pdf(tmp_path / "source.pdf", ["X", "Y", "Z"])
    service = PdfPageMutationService()
    import_source_sha = file_sha256(source_path)

    mutation = service.replace_pages_from_pdf(target_path, source_path, (1, 3), (0, 2))

    assert extract_pdfium_text(target_path) == "A X C Z"
    assert mutation.mutation_result.page_count == 4
    assert mutation.receipt.replacement_pairs == ((1, 0), (3, 2))
    assert mutation.receipt.replaced_page_indexes_after == (1, 3)
    assert mutation.mutation_result.page_index_transition is not None
    assert mutation.mutation_result.page_index_transition.cache_old_to_new == (0, None, 2, None)
    assert mutation.mutation_result.page_index_transition.current_page_old_to_new == (0, 1, 2, 3)
    assert file_sha256(source_path) == import_source_sha

    service.undo_page_replacement(target_path, mutation.receipt)
    assert extract_pdfium_text(target_path) == "A B C D"

    source_path.write_bytes(b"%PDF-1.4\nbroken")
    service.redo_page_replacement(target_path, mutation.receipt)
    assert extract_pdfium_text(target_path) == "A X C Z"


def test_replace_pages_from_pdf_preserves_target_metadata_outlines_destinations_and_attachments(
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

    mutation = service.replace_pages_from_pdf(target_path, source_path, (1,), (0,))
    after_target = service.snapshot_document_structure(target_path)

    assert after_target.metadata_fingerprint == before_target.metadata_fingerprint
    assert after_target.attachments_fingerprint == before_target.attachments_fingerprint
    assert tuple(item.destination_page_index for item in after_target.named_destinations) == (1,)
    assert after_target.outlines[0].destination_page_index == 0
    assert after_target.metadata_fingerprint != source_snapshot.metadata_fingerprint

    service.undo_page_replacement(target_path, mutation.receipt)
    assert service.snapshot_document_structure(target_path) == before_target


def test_replace_pages_from_pdf_preserves_target_and_source_annotations_on_replace(
    tmp_path: Path,
) -> None:
    target_path = create_target_with_existing_annotation(tmp_path / "target-existing-annot.pdf")
    source_path = create_supported_annotation_source_pdf(
        tmp_path / "source-existing-annot.pdf",
        subtype="/Square",
        include_parent=True,
        direct_annots=False,
        indirect_annotation=True,
        contents="replacement-annot",
    )
    service = PdfPageMutationService()
    before_target_annotation = annotation_details(target_path, page_index=1)

    mutation = service.replace_pages_from_pdf(target_path, source_path, (0,), (0,))

    assert annotation_details(target_path, page_index=1) == before_target_annotation
    imported_annotation = annotation_details(target_path, page_index=0)
    assert len(imported_annotation) == 1
    assert imported_annotation[0]["subtype"] == "/Square"
    assert imported_annotation[0]["contents"] == "replacement-annot"
    assert imported_annotation[0]["parent_objgen"] == imported_annotation[0]["page_objgen"]

    service.undo_page_replacement(target_path, mutation.receipt)
    assert annotation_details(target_path, page_index=0) == ()
    assert annotation_details(target_path, page_index=1) == before_target_annotation


def test_replace_pages_from_pdf_rejects_mismatched_selection_counts(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "target-counts.pdf", 2)
    source_path = create_blank_pdf(tmp_path / "source-counts.pdf", 1)
    service = PdfPageMutationService()

    with pytest.raises(ValueError, match="same length"):
        service.replace_pages_from_pdf(target_path, source_path, (0, 1), (0,))

    assert_no_page_replacement_temp_files(target_path)


def test_discard_page_replacement_receipt_removes_both_snapshots(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "target-dispose.pdf", 2)
    source_path = create_blank_pdf(tmp_path / "source-dispose.pdf", 1)
    service = PdfPageMutationService()

    mutation = service.replace_pages_from_pdf(target_path, source_path, (1,), (0,))

    assert mutation.receipt.source_snapshot_path.exists()
    assert mutation.receipt.target_undo_snapshot_path.exists()

    service.discard_page_replacement_receipt(target_path, mutation.receipt)

    assert not mutation.receipt.source_snapshot_path.exists()
    assert not mutation.receipt.target_undo_snapshot_path.exists()


def test_replace_pages_from_pdf_tampered_source_snapshot_blocks_redo(tmp_path: Path) -> None:
    target_path = create_blank_pdf(tmp_path / "target-tamper.pdf", 2)
    source_path = create_blank_pdf(tmp_path / "source-tamper.pdf", 1)
    service = PdfPageMutationService()

    mutation = service.replace_pages_from_pdf(target_path, source_path, (0,), (0,))
    service.undo_page_replacement(target_path, mutation.receipt)
    mutation.receipt.source_snapshot_path.write_bytes(b"%PDF-1.4\nbroken")

    with pytest.raises(PdfPageMutationError, match="整合性検証"):
        service.redo_page_replacement(target_path, mutation.receipt)


def test_replace_pages_from_pdf_replaces_effective_resources_with_source_snapshot(
    tmp_path: Path,
) -> None:
    target_path = create_resource_collision_pdf(
        tmp_path / "target-resources.pdf",
        text="TARGET",
        font_name="Helvetica",
        image_rgb=(255, 0, 0),
        form_text="old",
    )
    source_path = create_resource_collision_pdf(
        tmp_path / "source-resources.pdf",
        text="SOURCE",
        font_name="Courier",
        image_rgb=(0, 0, 255),
        form_text="new",
    )
    service = PdfPageMutationService()
    source_snapshot = service.snapshot_document_structure(source_path)

    mutation = service.replace_pages_from_pdf(target_path, source_path, (0,), (0,))
    target_snapshot = service.snapshot_document_structure(target_path)

    assert (
        target_snapshot.pages[0].resources_fingerprint
        == source_snapshot.pages[0].resources_fingerprint
    )
    assert (
        service._render_page_digests(source_path, (0,))[0]
        == service._render_page_digests(
            target_path,
            (0,),
        )[0]
    )

    service.undo_page_replacement(target_path, mutation.receipt)
    service.redo_page_replacement(target_path, mutation.receipt)
    assert (
        service._render_page_digests(source_path, (0,))[0]
        == service._render_page_digests(
            target_path,
            (0,),
        )[0]
    )


def test_replace_pages_from_pdf_rejects_stale_source_revision_before_snapshot_copy(
    tmp_path: Path,
) -> None:
    target_path = create_replacement_text_pdf(tmp_path / "target-stale-revision.pdf", ["A", "B"])
    source_path = create_replacement_text_pdf(tmp_path / "source-stale-revision.pdf", ["X"])
    service = PdfPageMutationService()
    expected_revision = service.read_source_pdf_revision(source_path)
    target_sha_before = file_sha256(target_path)

    create_replacement_text_pdf(source_path, ["Y"])

    with pytest.raises(PdfPageMutationError, match="置換元PDFが変更されました"):
        service.replace_pages_from_pdf(
            target_path,
            source_path,
            (0,),
            (0,),
            expected_source_revision=expected_revision,
        )

    assert file_sha256(target_path) == target_sha_before
    assert_no_page_replacement_temp_files(target_path)


def test_replace_pages_from_pdf_strips_old_target_page_keys_but_preserves_untouched_pages(
    tmp_path: Path,
) -> None:
    target_path = create_replacement_text_pdf(tmp_path / "target-page-keys.pdf", ["A", "B"])
    source_path = create_replacement_text_pdf(tmp_path / "source-page-keys.pdf", ["X"])
    add_direct_page_key(target_path, 0, "/Trans")
    add_direct_page_key(target_path, 1, "/Trans")
    service = PdfPageMutationService()

    service.replace_pages_from_pdf(target_path, source_path, (0,), (0,))

    with pikepdf.open(target_path) as pdf:
        assert "/Trans" not in pdf.pages[0].obj
        assert "/Trans" in pdf.pages[1].obj


def test_replace_pages_from_pdf_rejects_unsupported_source_page_keys(
    tmp_path: Path,
) -> None:
    target_path = create_replacement_text_pdf(tmp_path / "target-unsupported-key.pdf", ["A"])
    source_path = create_replacement_text_pdf(tmp_path / "source-unsupported-key.pdf", ["X"])
    add_direct_page_key(source_path, 0, "/Trans")
    service = PdfPageMutationService()
    target_sha_before = file_sha256(target_path)
    source_sha_before = file_sha256(source_path)

    with pytest.raises(PdfPageMutationError, match="置換元PDFのページに未対応の/Transがあります"):
        service.replace_pages_from_pdf(target_path, source_path, (0,), (0,))

    assert file_sha256(target_path) == target_sha_before
    assert file_sha256(source_path) == source_sha_before
    assert_no_page_replacement_temp_files(target_path)
