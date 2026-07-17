from __future__ import annotations

from pathlib import Path

import pytest

from pdf_regression_utils import extract_pdfium_text, file_sha256
from pdf_test_utils import create_blank_pdf, create_simple_text_pdf
from pdf_workbench.services.pdf_page_mutation import PdfPageMutationError, PdfPageMutationService
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


def test_replace_pages_from_pdf_executes_undoes_and_redoes_exact_order(tmp_path: Path) -> None:
    target_path = create_simple_text_pdf(tmp_path / "target.pdf", ["A", "B", "C", "D"])
    source_path = create_simple_text_pdf(tmp_path / "source.pdf", ["X", "Y", "Z"])
    service = PdfPageMutationService()
    import_source_sha = file_sha256(source_path)

    mutation = service.replace_pages_from_pdf(target_path, source_path, (1, 3), (0, 2))

    assert extract_pdfium_text(target_path) == "A X C Z"
    assert mutation.mutation_result.page_count == 4
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
