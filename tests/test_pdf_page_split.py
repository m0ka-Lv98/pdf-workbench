from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pikepdf
import pytest

import pdf_workbench.services.pdf_page_split as page_split_module
from pdf_regression_utils import file_sha256
from pdf_test_utils import create_simple_text_pdf
from pdf_workbench.domain.document_session import FileFingerprint
from pdf_workbench.domain.page_extraction import PageExtractionPlan
from pdf_workbench.domain.page_split import build_max_pages_split_plan, build_page_range_split_plan
from pdf_workbench.services.pdf_page_export import (
    PageExtractionResult,
    PdfPageExportError,
    SourcePdfChangedError,
)
from pdf_workbench.services.pdf_page_mutation import SourcePdfRevision
from pdf_workbench.services.pdf_page_split import (
    PageSplitBatchResult,
    PageSplitError,
    PageSplitOutputResult,
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
        self.expected_snapshots: list[TargetSnapshot | None] = []
        self.before_write: Callable[[Path], None] | None = None

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
        del working_copy_path, expected_source_revision
        self.expected_snapshots.append(expected_target_snapshot)
        self.calls.append(target_path)
        if target_path.name in self.failures:
            raise self.failures[target_path.name]
        if self.before_write is not None:
            self.before_write(target_path)
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


def make_split_output_result(
    target_path: Path,
    *,
    page_count: int = 2,
) -> PageSplitOutputResult:
    plan = build_max_pages_split_plan(page_count, 1, source_stem="source")
    now = datetime.now(UTC)
    return PageSplitOutputResult(
        chunk=plan.chunks[0],
        target_path=target_path,
        status=PageSplitOutputStatus.SUCCESS,
        fingerprint=None,
        error_message="",
        started_at=now,
        completed_at=now,
    )


def test_split_batch_result_rejects_unresolved_output_directory(tmp_path: Path) -> None:
    output_directory = tmp_path / "exports"
    output_directory.mkdir()
    source = tmp_path / "source.pdf"
    revision = make_revision(source, 1)
    output = make_split_output_result(output_directory / "source_pages_0001-0001.pdf")

    with pytest.raises(ValueError, match="output_directory must be resolved"):
        PageSplitBatchResult(
            outputs=(output,),
            output_directory=Path("exports"),
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            source_revision=revision,
        )


def test_split_batch_result_rejects_output_outside_directory(tmp_path: Path) -> None:
    output_directory = (tmp_path / "exports").resolve()
    output_directory.mkdir()
    source = tmp_path / "source.pdf"
    revision = make_revision(source, 1)
    output = make_split_output_result(tmp_path / "source_pages_0001-0001.pdf")

    with pytest.raises(ValueError, match="all split outputs"):
        PageSplitBatchResult(
            outputs=(output,),
            output_directory=output_directory,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            source_revision=revision,
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
    assert result.output_directory == tmp_path.resolve()
    assert all(output.target_path.parent == result.output_directory for output in result.outputs)
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


def test_split_service_rejects_missing_source_before_outputs(tmp_path: Path) -> None:
    source = tmp_path / "missing.pdf"
    revision_source = tmp_path / "revision-source.pdf"
    exporter = FakeExporter(make_revision(revision_source, 4))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="missing")

    with pytest.raises(PageSplitError, match="分割元PDF"):
        service.split_pdf(source, tmp_path, plan, expected_source_revision=exporter.revision)

    assert exporter.calls == []


def test_split_service_rejects_missing_output_directory_before_outputs(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")

    with pytest.raises(PageSplitError, match="出力先フォルダ"):
        service.split_pdf(
            source,
            tmp_path / "missing-output-dir",
            plan,
            expected_source_revision=exporter.revision,
        )

    assert exporter.calls == []


def test_split_service_rejects_duplicate_targets_before_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")
    duplicate = tmp_path / "duplicate.pdf"
    monkeypatch.setattr(
        page_split_module,
        "build_split_target_path",
        lambda _directory, _chunk: duplicate,
    )

    with pytest.raises(PageSplitError, match="重複"):
        service.split_pdf(source, tmp_path, plan, expected_source_revision=exporter.revision)

    assert exporter.calls == []


def test_split_service_rejects_source_and_working_copy_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pdf"
    working_copy = tmp_path / "working.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    working_copy.write_bytes(b"%PDF-1.7\n% working\n")
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")

    monkeypatch.setattr(
        page_split_module,
        "build_split_target_path",
        lambda _directory, chunk: source if chunk.chunk_index == 0 else tmp_path / chunk.filename,
    )
    with pytest.raises(PageSplitError, match="同じ場所"):
        service.split_pdf(source, tmp_path, plan, expected_source_revision=exporter.revision)

    monkeypatch.setattr(
        page_split_module,
        "build_split_target_path",
        lambda _directory, chunk: (
            working_copy if chunk.chunk_index == 0 else tmp_path / chunk.filename
        ),
    )
    with pytest.raises(PageSplitError, match="作業コピー"):
        service.split_pdf(
            source,
            tmp_path,
            plan,
            working_copy_path=working_copy,
            expected_source_revision=exporter.revision,
        )

    assert exporter.calls == []


def test_split_overwrite_off_rejects_existing_snapshot_before_any_output(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")
    target = tmp_path / "source_pages_0001-0002.pdf"
    target.write_bytes(b"existing")
    snapshots = {
        tmp_path / "source_pages_0001-0002.pdf": TargetSnapshot.capture(target),
        tmp_path / "source_pages_0003-0004.pdf": TargetSnapshot.capture(
            tmp_path / "source_pages_0003-0004.pdf"
        ),
    }

    with pytest.raises(PageSplitError, match="既存"):
        service.split_pdf(
            source,
            tmp_path,
            plan,
            expected_source_revision=exporter.revision,
            expected_target_snapshots=snapshots,
        )

    assert exporter.calls == []


def test_split_overwrite_off_does_not_replace_target_created_after_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")
    protected = tmp_path / "source_pages_0001-0002.pdf"
    snapshots = {
        tmp_path / "source_pages_0001-0002.pdf": TargetSnapshot.capture(protected),
        tmp_path / "source_pages_0003-0004.pdf": TargetSnapshot.capture(
            tmp_path / "source_pages_0003-0004.pdf"
        ),
    }

    def create_target_before_write(target_path: Path) -> None:
        if target_path == protected:
            protected.write_bytes(b"late external target")
            raise TargetChangedError("changed")

    exporter.before_write = create_target_before_write

    result = service.split_pdf(
        source,
        tmp_path,
        plan,
        expected_source_revision=exporter.revision,
        expected_target_snapshots=snapshots,
    )

    assert [output.status for output in result.outputs] == [
        PageSplitOutputStatus.FAILED,
        PageSplitOutputStatus.SUCCESS,
    ]
    assert protected.read_bytes() == b"late external target"


def test_split_overwrite_on_uses_batch_start_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")
    target = tmp_path / "source_pages_0001-0002.pdf"
    target.write_bytes(b"original")
    snapshot = TargetSnapshot.capture(target)

    result = service.split_pdf(
        source,
        tmp_path,
        plan,
        expected_source_revision=exporter.revision,
        overwrite=True,
    )

    assert result.success_count == 2
    assert exporter.expected_snapshots[0] == snapshot


def test_split_rejects_incomplete_expected_target_snapshots(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")

    with pytest.raises(PageSplitError, match="不足"):
        service.split_pdf(
            source,
            tmp_path,
            plan,
            expected_source_revision=exporter.revision,
            expected_target_snapshots={
                tmp_path / "source_pages_0001-0002.pdf": TargetSnapshot.capture(
                    tmp_path / "source_pages_0001-0002.pdf"
                )
            },
        )


def test_split_rejects_extra_expected_target_snapshots(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")

    with pytest.raises(PageSplitError, match="未知"):
        service.split_pdf(
            source,
            tmp_path,
            plan,
            expected_source_revision=exporter.revision,
            expected_target_snapshots={
                tmp_path / "source_pages_0001-0002.pdf": TargetSnapshot.capture(
                    tmp_path / "source_pages_0001-0002.pdf"
                ),
                tmp_path / "source_pages_0003-0004.pdf": TargetSnapshot.capture(
                    tmp_path / "source_pages_0003-0004.pdf"
                ),
                tmp_path / "extra.pdf": TargetSnapshot.capture(tmp_path / "extra.pdf"),
            },
        )


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


def test_split_service_rejects_target_outside_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 4))
    service = PdfPageSplitService(exporter)
    plan = build_max_pages_split_plan(4, 2, source_stem="source")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    monkeypatch.setattr(
        page_split_module,
        "build_split_target_path",
        lambda _directory, chunk: (tmp_path / f"outside-{chunk.chunk_index}.pdf").resolve(),
    )

    with pytest.raises(PageSplitError, match="フォルダ外"):
        service.split_pdf(source, output_dir, plan, expected_source_revision=exporter.revision)


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


def test_split_source_drift_inside_export_skips_all_remaining_outputs(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 6))
    plan = build_max_pages_split_plan(6, 2, source_stem="source")
    exporter.failures["source_pages_0003-0004.pdf"] = SourcePdfChangedError("source changed")
    service = PdfPageSplitService(exporter)

    result = service.split_pdf(source, tmp_path, plan, expected_source_revision=exporter.revision)

    assert [output.status for output in result.outputs] == [
        PageSplitOutputStatus.SUCCESS,
        PageSplitOutputStatus.FAILED,
        PageSplitOutputStatus.SKIPPED,
    ]
    assert [path.name for path in exporter.calls] == [
        "source_pages_0001-0002.pdf",
        "source_pages_0003-0004.pdf",
    ]


def test_split_source_deleted_between_outputs_skips_all_remaining_outputs(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    exporter = FakeExporter(make_revision(source, 6))
    plan = build_max_pages_split_plan(6, 2, source_stem="source")
    service = PdfPageSplitService(exporter)

    def fail_after_first_read(_path: Path) -> SourcePdfRevision:
        exporter.revision_reads += 1
        if exporter.revision_reads > 2:
            raise FileNotFoundError("gone")
        return exporter.revision

    exporter.read_source_pdf_revision = fail_after_first_read  # type: ignore[method-assign]

    result = service.split_pdf(source, tmp_path, plan, expected_source_revision=exporter.revision)

    assert [output.status for output in result.outputs] == [
        PageSplitOutputStatus.SUCCESS,
        PageSplitOutputStatus.FAILED,
        PageSplitOutputStatus.SKIPPED,
    ]
    assert len(exporter.calls) == 1
