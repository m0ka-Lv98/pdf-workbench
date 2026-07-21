from __future__ import annotations

import shutil
from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfWriter

from pdf_regression_utils import extract_pdfium_text, file_sha256
from pdf_test_utils import create_simple_text_pdf
from pdf_workbench.domain.page_extraction import (
    build_page_extraction_plan,
    parse_page_range_extraction_plan,
)
from pdf_workbench.services.pdf_page_export import PdfPageExportError, PdfPageExportService
from pdf_workbench.services.pdf_save_service import TargetChangedError, TargetSnapshot


def compatibility_fixture(name: str, destination: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "compatibility" / name
    shutil.copyfile(source, destination)
    return destination


def create_metadata_outline_pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    writer.add_outline_item("Second", 1)
    writer.add_metadata({"/Title": "Source Title", "/Author": "Source Author"})
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def create_unsupported_widget_pdf(path: Path) -> Path:
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(200, 200))
        widget = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/Annot"),
                    "/Subtype": pikepdf.Name("/Widget"),
                    "/Rect": pikepdf.Array([20, 20, 80, 80]),
                    "/T": pikepdf.String("field"),
                }
            )
        )
        page.obj["/Annots"] = pikepdf.Array([widget])
        pdf.save(path)
    return path


def test_extracts_multiple_non_contiguous_pages_in_source_order(tmp_path: Path) -> None:
    source = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C"])
    target = tmp_path / "out.pdf"
    service = PdfPageExportService()
    plan = build_page_extraction_plan(3, (0, 2))
    source_hash = file_sha256(source)

    result = service.extract_pages(source, target, plan)

    assert result.target_path == target.resolve()
    assert result.exported_page_count == 2
    assert file_sha256(source) == source_hash
    assert extract_pdfium_text(target)


@pytest.mark.parametrize(
    "fixture_name",
    ["japanese-text.pdf", "rotations.pdf", "page-boxes.pdf", "annotations.pdf"],
)
def test_extracts_compatibility_fixtures_with_reopen_and_render_validation(
    tmp_path: Path,
    fixture_name: str,
) -> None:
    source = compatibility_fixture(fixture_name, tmp_path / fixture_name)
    target = tmp_path / f"{fixture_name}.extract.pdf"
    service = PdfPageExportService()
    revision = service.read_source_pdf_revision(source)
    plan = build_page_extraction_plan(revision.page_count, (0,))

    result = service.extract_pages(
        source,
        target,
        plan,
        expected_source_revision=revision,
        expected_target_snapshot=TargetSnapshot.capture(target),
    )

    assert result.exported_page_count == 1
    with pikepdf.open(target) as pdf:
        assert len(pdf.pages) == 1


def test_extracts_all_pages_from_range_plan(tmp_path: Path) -> None:
    source = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C"])
    target = tmp_path / "all.pdf"
    service = PdfPageExportService()
    plan = parse_page_range_extraction_plan(3, "1-3")

    service.extract_pages(source, target, plan)

    with pikepdf.open(target) as pdf:
        assert len(pdf.pages) == 3


def test_extract_does_not_copy_metadata_or_bookmarks(tmp_path: Path) -> None:
    source = create_metadata_outline_pdf(tmp_path / "source.pdf")
    target = tmp_path / "out.pdf"

    PdfPageExportService().extract_pages(
        source,
        target,
        build_page_extraction_plan(2, (0,)),
    )

    with pikepdf.open(target) as pdf:
        docinfo = {str(key): str(value) for key, value in pdf.docinfo.items()}
        assert docinfo.get("/Title") != "Source Title"
        assert docinfo.get("/Author") != "Source Author"
        assert "/Outlines" not in pdf.Root


def test_rejects_target_equals_source_or_working_copy(tmp_path: Path) -> None:
    source = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C"])
    service = PdfPageExportService()
    plan = build_page_extraction_plan(3, (0,))

    with pytest.raises(PdfPageExportError, match="同じ場所"):
        service.extract_pages(source, source, plan)
    with pytest.raises(PdfPageExportError, match="作業コピー"):
        service.extract_pages(
            source,
            tmp_path / "out.pdf",
            plan,
            working_copy_path=tmp_path / "out.pdf",
        )


def test_rejects_source_revision_drift(tmp_path: Path) -> None:
    source = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C"])
    service = PdfPageExportService()
    revision = service.read_source_pdf_revision(source)
    source.write_bytes(source.read_bytes() + b"\\n% changed")

    with pytest.raises(PdfPageExportError, match="変更"):
        service.extract_pages(
            source,
            tmp_path / "out.pdf",
            build_page_extraction_plan(3, (0,)),
            expected_source_revision=revision,
        )


def test_rejects_target_snapshot_drift_and_preserves_existing_target(tmp_path: Path) -> None:
    source = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C"])
    target = compatibility_fixture("japanese-text.pdf", tmp_path / "target.pdf")
    original_target = target.read_bytes()
    snapshot = TargetSnapshot.capture(target)
    target.write_bytes(original_target + b"\\n% changed")

    with pytest.raises(TargetChangedError):
        PdfPageExportService().extract_pages(
            source,
            target,
            build_page_extraction_plan(3, (0,)),
            expected_target_snapshot=snapshot,
        )

    assert target.read_bytes() == original_target + b"\\n% changed"


def test_rejects_unsupported_annotation_without_modifying_source_or_target(
    tmp_path: Path,
) -> None:
    source = create_unsupported_widget_pdf(tmp_path / "source.pdf")
    target = compatibility_fixture("english-text.pdf", tmp_path / "target.pdf")
    source_hash = file_sha256(source)
    target_hash = file_sha256(target)

    with pytest.raises(PdfPageExportError, match="Widget"):
        PdfPageExportService().extract_pages(
            source,
            target,
            build_page_extraction_plan(1, (0,)),
            expected_target_snapshot=TargetSnapshot.capture(target),
        )

    assert file_sha256(source) == source_hash
    assert file_sha256(target) == target_hash


def test_candidate_cleanup_on_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C"])
    target = tmp_path / "out.pdf"
    service = PdfPageExportService()

    def fail_validation(_path: Path, _page_count: int) -> None:
        raise PdfPageExportError("boom")

    monkeypatch.setattr(service, "_validate_candidate_render", fail_validation)
    with pytest.raises(PdfPageExportError, match="boom"):
        service.extract_pages(source, target, build_page_extraction_plan(3, (0,)))

    assert not target.exists()
    assert not list(tmp_path.glob("*.extract.tmp.pdf"))


def test_atomic_replace_failure_preserves_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = create_simple_text_pdf(tmp_path / "source.pdf", ["A", "B", "C"])
    target = compatibility_fixture("japanese-text.pdf", tmp_path / "target.pdf")
    original_target = target.read_bytes()
    service = PdfPageExportService()

    def fail_replace(_candidate_path: Path, _target_path: Path) -> None:
        raise PdfPageExportError("検証済み抽出PDFの置換に失敗しました")

    monkeypatch.setattr(service, "_replace_atomically", fail_replace)
    with pytest.raises(PdfPageExportError, match="置換"):
        service.extract_pages(
            source,
            target,
            build_page_extraction_plan(3, (0,)),
            expected_target_snapshot=TargetSnapshot.capture(target),
        )

    assert target.read_bytes() == original_target
