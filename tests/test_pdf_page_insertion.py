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
