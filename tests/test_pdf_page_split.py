from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pikepdf
import pytest

from pdf_regression_utils import file_sha256
from pdf_test_utils import create_simple_text_pdf
from pdf_workbench.domain.document_session import FileFingerprint
from pdf_workbench.domain.page_extraction import PageExtractionPlan
from pdf_workbench.domain.page_split import build_max_pages_split_plan, build_page_range_split_plan
from pdf_workbench.services.pdf_page_export import PageExtractionResult, PdfPageExportError
from pdf_workbench.services.pdf_page_mutation import SourcePdfRevision
from pdf_workbench.services.pdf_page_split import (
    PageSplitError,
    PageSplitOutputStatus,
    PdfPageSplitService,
)
from pdf_workbench.services.pdf_save_service import TargetChangedError, TargetSnapshot


class FakeExporter:
    def __init__(self, revision: SourcePdfRevision) -> None:
        self.revision = revision
        self.calls: list[Path] = []
        self.failures: dict[str, Exception] = {}
        self.revision_reads = 0
        self.drift_after_reads: int | None = None

    def read_source_pdf_revision(self, _path: Path) -> SourcePdfRevision:
        self.revision_reads += 1
        if self.drift_after_reads is not None and self.revision_reads > self.drift_after_reads:
            return replace(self.revision, sha256="1" * 64)
        return self.revision

    def extract_pages(
        self,
        _source_path: Path,
        target_path: Path,
        plan: PageExtractionPlan,
        *,
        working_copy_path: Path | None = None,
        expected_source_revision: SourcePdfRevision | None = None,
        expected_target_snapshot: TargetSnapshot | None = None,
    ) -> PageExtractionResult:
        del working_copy_path, expected_source_revision, expected_target_snapshot
        self.calls.append(target_path)
        if target_path.name in self.failures:
            raise self.failures[target_path.name]
        target_path.write_bytes(b"%PDF-1.7\n% split fake\n")
        return PageExtractionResult(
            target_path=target_path,
            fingerprint=FileFingerprint.from_path(target_path),
            exported_page_count=plan.output_page_count,
            exported_at=datetime.now(UTC),
        )


def make_revision(path: Path, page_count: int) -> SourcePdfRevision:
    path.write_bytes(b"%PDF-1.7\n% source\n")
    return SourcePdfRevision(
        resolved_path=path.resolve(),
        fingerprint=FileFingerprint.from_path(path),
        sha256="0" * 64,
        page_count=page_count,
    )


def test_split_service_processes_outputs_in_order_and_reports_success(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 5))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(5, 2, source_stem="source")
    progress: list[str] = []

    result = service.split_pdf(
        source,
        tmp_path,
        plan,
        expected_source_revision=exporter.revision,
        progress_callback=lambda item: progress.append(f"{item.output_number}:{item.status}"),
    )

    assert [path.name for path in exporter.calls] == [
        "source_pages_0001-0002.pdf",
        "source_pages_0003-0004.pdf",
        "source_pages_0005-0005.pdf",
    ]
    assert result.success_count == 3
    assert result.failure_count == 0
    assert all(output.status is PageSplitOutputStatus.SUCCESS for output in result.outputs)
    assert progress


def test_split_service_continues_after_individual_output_failure(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 5))
    exporter.failures["source_pages_0003-0004.pdf"] = PdfPageExportError("boom")
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(5, 2, source_stem="source")

    result = service.split_pdf(source, tmp_path, plan, expected_source_revision=exporter.revision)

    assert [output.status for output in result.outputs] == [
        PageSplitOutputStatus.SUCCESS,
        PageSplitOutputStatus.FAILED,
        PageSplitOutputStatus.SUCCESS,
    ]
    assert result.success_count == 2
    assert result.failure_count == 1
    assert "boom" in result.outputs[1].error_message


def test_split_service_records_target_snapshot_drift_as_output_failure(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    exporter.failures["source_pages_0001-0002.pdf"] = TargetChangedError("changed")
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")

    result = service.split_pdf(source, tmp_path, plan, expected_source_revision=exporter.revision)

    assert [output.status for output in result.outputs] == [
        PageSplitOutputStatus.FAILED,
        PageSplitOutputStatus.SUCCESS,
    ]


def test_split_service_source_revision_drift_stops_remaining_outputs(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 6))
    exporter.drift_after_reads = 2
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(6, 2, source_stem="source")

    result = service.split_pdf(source, tmp_path, plan, expected_source_revision=exporter.revision)

    assert [output.status for output in result.outputs] == [
        PageSplitOutputStatus.SUCCESS,
        PageSplitOutputStatus.FAILED,
        PageSplitOutputStatus.SKIPPED,
    ]
    assert len(exporter.calls) == 1


def test_split_service_cancels_between_outputs(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 6))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(6, 2, source_stem="source")
    checks = 0

    def should_cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    result = service.split_pdf(
        source,
        tmp_path,
        plan,
        expected_source_revision=exporter.revision,
        should_cancel=should_cancel,
    )

    assert [output.status for output in result.outputs] == [
        PageSplitOutputStatus.SUCCESS,
        PageSplitOutputStatus.CANCELLED,
        PageSplitOutputStatus.CANCELLED,
    ]
    assert len(exporter.calls) == 1


def test_split_service_rejects_overwrite_off_collision_before_outputs(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    (tmp_path / "source_pages_0001-0002.pdf").write_bytes(b"existing")
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")

    with pytest.raises(PageSplitError, match="既存"):
        service.split_pdf(source, tmp_path, plan, expected_source_revision=exporter.revision)

    assert exporter.calls == []


def test_split_service_rejects_managed_path_and_duplicate_targets(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")

    with pytest.raises(PageSplitError, match="一時作業"):
        service.split_pdf(
            source,
            tmp_path,
            plan,
            expected_source_revision=exporter.revision,
            is_managed_path=lambda _path: True,
        )


def test_split_service_splits_real_pdf_by_manual_ranges(tmp_path: Path) -> None:
    source = create_simple_text_pdf(tmp_path / "real-source.pdf", ["A", "B", "C", "D"])
    source_hash = file_sha256(source)
    service = PdfPageSplitService()
    revision = service.read_source_pdf_revision(source)
    plan = build_page_range_split_plan(4, "1-2\n3-4", source_stem=source.stem)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = service.split_pdf(
        source,
        output_dir,
        plan,
        expected_source_revision=revision,
    )

    assert result.success_count == 2
    assert file_sha256(source) == source_hash
    for output, expected_count in zip(result.outputs, (2, 2), strict=True):
        with pikepdf.open(output.target_path) as pdf:
            assert len(pdf.pages) == expected_count


def test_split_service_splits_real_pdf_by_max_pages(tmp_path: Path) -> None:
    source = create_simple_text_pdf(tmp_path / "real-source.pdf", ["A", "B", "C"])
    service = PdfPageSplitService()
    revision = service.read_source_pdf_revision(source)
    plan = build_max_pages_split_plan(3, 1, source_stem=source.stem)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    result = service.split_pdf(
        source,
        output_dir,
        plan,
        expected_source_revision=revision,
    )

    assert result.success_count == 3
    for output in result.outputs:
        with pikepdf.open(output.target_path) as pdf:
            assert len(pdf.pages) == 1


def test_split_service_preserves_existing_target_after_individual_failure(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 3))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(3, 1, source_stem="source")
    protected_target = tmp_path / "source_pages_0002-0002.pdf"
    protected_target.write_bytes(b"protected")
    original = protected_target.read_bytes()
    exporter.failures[protected_target.name] = PdfPageExportError("candidate validation failed")

    result = service.split_pdf(
        source,
        tmp_path,
        plan,
        expected_source_revision=exporter.revision,
        overwrite=True,
    )

    assert [output.status for output in result.outputs] == [
        PageSplitOutputStatus.SUCCESS,
        PageSplitOutputStatus.FAILED,
        PageSplitOutputStatus.SUCCESS,
    ]
    assert protected_target.read_bytes() == original
