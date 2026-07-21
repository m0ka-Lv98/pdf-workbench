from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader, PdfWriter

from pdf_test_utils import create_blank_pdf, create_simple_text_pdf
from pdf_workbench.domain.pdf_merge import (
    PdfMergeBookmarkPolicy,
    PdfMergeMetadataPolicy,
    build_pdf_merge_plan,
)
from pdf_workbench.services.pdf_merge import (
    PdfMergeCancelled,
    PdfMergeError,
    PdfMergeService,
    SourcePdfChangedError,
)
from pdf_workbench.services.pdf_save_service import TargetChangedError, TargetSnapshot


def build_plan(service: PdfMergeService, paths: tuple[Path, ...], output: Path):
    inputs = tuple(service.read_merge_input(path) for path in paths)
    return build_pdf_merge_plan(inputs, output)


def create_outline_pdf(path: Path, title: str) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_outline_item(title, 0)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def test_merge_pdfs_writes_ordered_independent_output(tmp_path: Path) -> None:
    first = create_simple_text_pdf(tmp_path / "first.pdf", ["A1", "A2"])
    second = create_simple_text_pdf(tmp_path / "second.pdf", ["B1"])
    output = tmp_path / "merged.pdf"
    service = PdfMergeService()
    plan = build_plan(service, (first, second), output)

    result = service.merge_pdfs(plan)

    reader = PdfReader(output)
    assert len(reader.pages) == 3
    assert [page.extract_text().strip() for page in reader.pages] == ["A1", "A2", "B1"]
    assert result.target_path == output.resolve()
    assert result.total_page_count == 3
    assert [item.display_range for item in result.inputs] == ["1-2", "3-3"]


def test_read_merge_input_rejects_missing_and_non_pdf_paths(tmp_path: Path) -> None:
    service = PdfMergeService()
    text_file = tmp_path / "notes.txt"
    text_file.write_text("not pdf", encoding="utf-8")

    with pytest.raises(PdfMergeError, match="存在しません"):
        service.read_merge_input(tmp_path / "missing.pdf")
    with pytest.raises(PdfMergeError, match="PDFファイル"):
        service.read_merge_input(text_file)


def test_merge_rejects_missing_output_directory(tmp_path: Path) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "missing" / "merged.pdf")

    with pytest.raises(PdfMergeError, match="出力先フォルダ"):
        service.merge_pdfs(plan)


def test_merge_metadata_policy_copies_only_allowed_selected_source_fields(
    tmp_path: Path,
) -> None:
    first = create_simple_text_pdf(tmp_path / "first.pdf", ["A"])
    second = create_simple_text_pdf(tmp_path / "second.pdf", ["B"])
    with pikepdf.open(second, allow_overwriting_input=True) as pdf:
        pdf.docinfo[pikepdf.Name("/Title")] = pikepdf.String("Chosen")
        pdf.docinfo[pikepdf.Name("/Producer")] = pikepdf.String("Do not copy")
        pdf.docinfo[pikepdf.Name("/Custom")] = pikepdf.String("Nope")
        pdf.save(second)
    service = PdfMergeService()
    inputs = tuple(service.read_merge_input(path) for path in (first, second))
    plan = build_pdf_merge_plan(
        inputs,
        tmp_path / "merged.pdf",
        metadata_policy=PdfMergeMetadataPolicy.SELECTED_SOURCE,
        metadata_source_path=second,
    )

    service.merge_pdfs(plan)

    with pikepdf.open(plan.output_path) as pdf:
        docinfo = {str(key): str(value) for key, value in pdf.docinfo.items()}
    assert docinfo["/Title"] == "Chosen"
    assert docinfo.get("/Producer") != "Do not copy"
    assert "/Custom" not in docinfo


def test_merge_rejects_existing_output_without_overwrite(tmp_path: Path) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    output = create_blank_pdf(tmp_path / "merged.pdf", 1)
    original_bytes = output.read_bytes()
    service = PdfMergeService()
    plan = build_plan(service, (first, second), output)

    with pytest.raises(PdfMergeError, match="既に存在"):
        service.merge_pdfs(plan, overwrite=False)

    assert output.read_bytes() == original_bytes


def test_merge_detects_target_snapshot_drift_before_replace(tmp_path: Path) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    output = create_blank_pdf(tmp_path / "merged.pdf", 1)
    snapshot = TargetSnapshot.capture(output)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), output)
    output.write_bytes(b"%PDF-1.4\nchanged\n")

    with pytest.raises(TargetChangedError):
        service.merge_pdfs(plan, overwrite=True, expected_target_snapshot=snapshot)


def test_merge_detects_source_revision_drift(tmp_path: Path) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")
    revisions = {item.path: service.read_source_pdf_revision(item.path) for item in plan.inputs}
    shutil.copyfile(create_blank_pdf(tmp_path / "replacement.pdf", 2), first)

    with pytest.raises(SourcePdfChangedError):
        service.merge_pdfs(plan, expected_source_revisions=revisions)


def test_merge_cancel_removes_candidate_and_preserves_target(tmp_path: Path) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    output = create_blank_pdf(tmp_path / "merged.pdf", 1)
    original_bytes = output.read_bytes()
    service = PdfMergeService()
    plan = build_plan(service, (first, second), output)
    calls = 0

    def cancel_after_first_poll() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    with pytest.raises(PdfMergeCancelled):
        service.merge_pdfs(plan, overwrite=True, should_cancel=cancel_after_first_poll)

    assert output.read_bytes() == original_bytes
    assert not list(tmp_path.glob("*.merge.tmp.pdf"))


def test_merge_rejects_workspace_managed_input(tmp_path: Path) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")

    with pytest.raises(PdfMergeError, match="一時作業フォルダ"):
        service.merge_pdfs(plan, is_managed_path=lambda path: path == first.resolve())


def test_merge_grouped_bookmark_policy_creates_unique_top_level_groups(tmp_path: Path) -> None:
    second_dir = tmp_path / "other"
    first_dir = tmp_path / "same"
    first_dir.mkdir()
    second_dir.mkdir()
    first = create_outline_pdf(first_dir / "source.pdf", "First bookmark")
    second = create_outline_pdf(second_dir / "source.pdf", "Second bookmark")
    service = PdfMergeService()
    inputs = tuple(service.read_merge_input(path) for path in (first, second))
    plan = build_pdf_merge_plan(
        inputs,
        tmp_path / "merged.pdf",
        bookmark_policy=PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE,
    )

    service.merge_pdfs(plan)

    outline = PdfReader(plan.output_path).outline
    assert outline[0].title == "source.pdf"
    assert outline[1][0].title == "First bookmark"
    assert outline[2].title == "source.pdf (2)"
    assert outline[3][0].title == "Second bookmark"


def test_merge_large_documents_without_rasterizing_all_pages(tmp_path: Path) -> None:
    paths = tuple(create_blank_pdf(tmp_path / f"source-{index}.pdf", 75) for index in range(3))
    service = PdfMergeService()
    plan = build_plan(service, paths, tmp_path / "merged.pdf")

    result = service.merge_pdfs(plan)

    assert result.total_page_count == 225
    assert len(PdfReader(result.target_path).pages) == 225
    assert not list(tmp_path.glob("*.merge-source.tmp.pdf"))
