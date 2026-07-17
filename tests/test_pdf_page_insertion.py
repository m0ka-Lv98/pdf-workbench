from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter

from pdf_regression_utils import extract_pdfium_text, file_sha256
from pdf_test_utils import create_blank_pdf, create_simple_text_pdf
from pdf_workbench.services.pdf_page_mutation import PdfPageMutationError, PdfPageMutationService


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
