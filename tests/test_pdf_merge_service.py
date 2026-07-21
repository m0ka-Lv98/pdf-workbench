from __future__ import annotations

import os
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


def test_inspect_merge_input_returns_revision_bound_to_merge_input(tmp_path: Path) -> None:
    path = create_blank_pdf(tmp_path / "source.pdf", 2)
    service = PdfMergeService()

    inspected = service.inspect_merge_input(path)

    assert inspected.merge_input.path == path.resolve()
    assert inspected.merge_input.page_count == 2
    assert inspected.source_revision.resolved_path == inspected.merge_input.path
    assert inspected.source_revision.page_count == inspected.merge_input.page_count


def test_merge_rejects_missing_output_directory(tmp_path: Path) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "missing" / "merged.pdf")

    with pytest.raises(PdfMergeError, match="出力先フォルダ"):
        service.merge_pdfs(plan)


def test_merge_preflight_rejects_output_parent_file_and_managed_output(tmp_path: Path) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("x", encoding="utf-8")
    service = PdfMergeService()
    bad_parent_plan = build_plan(service, (first, second), parent_file / "merged.pdf")

    with pytest.raises(PdfMergeError, match="出力先フォルダ"):
        service.merge_pdfs(bad_parent_plan)

    managed_plan = build_plan(service, (first, second), tmp_path / "managed.pdf")
    with pytest.raises(PdfMergeError, match="一時作業フォルダ"):
        service.merge_pdfs(
            managed_plan,
            is_managed_path=lambda path: path == managed_plan.output_path,
        )


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


def test_merge_rejects_same_page_count_expected_source_revision_drift(tmp_path: Path) -> None:
    first = create_simple_text_pdf(tmp_path / "first.pdf", ["A"])
    second = create_simple_text_pdf(tmp_path / "second.pdf", ["B"])
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")
    revisions = {item.path: service.read_source_pdf_revision(item.path) for item in plan.inputs}
    create_simple_text_pdf(first, ["changed"])

    with pytest.raises(SourcePdfChangedError, match="変更"):
        service.merge_pdfs(plan, expected_source_revisions=revisions)


def test_merge_snapshot_revision_mismatch_is_rejected(tmp_path: Path) -> None:
    source = create_simple_text_pdf(tmp_path / "source.pdf", ["A"])
    snapshot = create_simple_text_pdf(tmp_path / "snapshot.pdf", ["changed"])
    service = PdfMergeService()
    revision = service.read_source_pdf_revision(source)

    with pytest.raises(SourcePdfChangedError, match="snapshot"):
        service._ensure_snapshot_matches_revision(snapshot, revision)


def test_merge_service_rejects_incomplete_expected_source_revisions(tmp_path: Path) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")
    revisions = {plan.inputs[0].path: service.read_source_pdf_revision(plan.inputs[0].path)}

    with pytest.raises(PdfMergeError, match="不足"):
        service.merge_pdfs(plan, expected_source_revisions=revisions)


def test_merge_service_rejects_extra_expected_source_revisions(tmp_path: Path) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    extra = create_blank_pdf(tmp_path / "extra.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")
    revisions = {item.path: service.read_source_pdf_revision(item.path) for item in plan.inputs}
    revisions[extra.resolve()] = service.read_source_pdf_revision(extra)

    with pytest.raises(PdfMergeError, match="余分"):
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


def test_merge_accepts_input_from_read_only_directory(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode semantics are not available on Windows")
    source_dir = tmp_path / "readonly"
    source_dir.mkdir()
    first = create_blank_pdf(source_dir / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    source_dir.chmod(0o555)
    try:
        service = PdfMergeService()
        plan = build_plan(service, (first, second), tmp_path / "merged.pdf")

        service.merge_pdfs(plan)
    finally:
        source_dir.chmod(0o755)

    assert plan.output_path.exists()


def test_merge_source_snapshot_is_created_outside_source_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    second_dir = tmp_path / "second-source"
    source_dir.mkdir()
    second_dir.mkdir()
    first = create_blank_pdf(source_dir / "first.pdf", 1)
    second = create_blank_pdf(second_dir / "second.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")
    snapshot_directories: list[Path] = []
    original = service._create_source_snapshot

    def record_snapshot(source_path: Path, *, snapshot_directory: Path) -> Path:
        snapshot_directories.append(snapshot_directory)
        snapshot = original(source_path, snapshot_directory=snapshot_directory)
        assert snapshot.parent != source_path.parent
        return snapshot

    monkeypatch.setattr(service, "_create_source_snapshot", record_snapshot)

    service.merge_pdfs(plan)

    assert snapshot_directories
    assert set(snapshot_directories) == {plan.output_path.parent}


def test_merge_keeps_only_one_source_snapshot_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = tuple(create_blank_pdf(tmp_path / f"source-{index}.pdf", 1) for index in range(3))
    service = PdfMergeService()
    plan = build_plan(service, paths, tmp_path / "merged.pdf")
    active = 0
    max_active = 0
    original_create = service._create_source_snapshot
    original_cleanup = service._cleanup_source_snapshot

    def create_snapshot(source_path: Path, *, snapshot_directory: Path) -> Path:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        return original_create(source_path, snapshot_directory=snapshot_directory)

    def cleanup_snapshot(
        snapshot_path: Path | None,
        *,
        primary_error: BaseException | None,
    ) -> None:
        nonlocal active
        original_cleanup(snapshot_path, primary_error=primary_error)
        if snapshot_path is not None:
            active -= 1

    monkeypatch.setattr(service, "_create_source_snapshot", create_snapshot)
    monkeypatch.setattr(service, "_cleanup_source_snapshot", cleanup_snapshot)

    service.merge_pdfs(plan)

    assert max_active == 1
    assert active == 0


def test_merge_snapshot_cleanup_failure_prevents_target_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    output = create_blank_pdf(tmp_path / "merged.pdf", 1)
    original_bytes = output.read_bytes()
    service = PdfMergeService()
    plan = build_plan(service, (first, second), output)

    def fail_cleanup(_snapshot_path: Path | None, *, primary_error: BaseException | None) -> None:
        if primary_error is None:
            raise PdfMergeError("snapshot cleanup failed")

    monkeypatch.setattr(service, "_cleanup_source_snapshot", fail_cleanup)

    with pytest.raises(PdfMergeError, match="snapshot cleanup"):
        service.merge_pdfs(plan, overwrite=True)

    assert output.read_bytes() == original_bytes


def test_merge_snapshot_cleanup_does_not_hide_primary_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")

    def fail_import(*_args: object, **_kwargs: object) -> None:
        raise PdfMergeError("primary import failure")

    def fail_cleanup(_snapshot_path: Path | None, *, primary_error: BaseException | None) -> None:
        if _snapshot_path is None:
            return
        assert primary_error is not None
        raise OSError("cleanup failure")

    monkeypatch.setattr(
        service._import_inspector,
        "reject_unsupported_document_structures",
        fail_import,
    )
    monkeypatch.setattr(service, "_cleanup_source_snapshot", fail_cleanup)

    with pytest.raises(PdfMergeError, match="primary import failure"):
        service.merge_pdfs(plan)


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


def test_merge_validation_renders_every_output_page(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 2)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")
    rendered: list[tuple[int, ...]] = []

    def validate(path: str, *, expected_page_count: int | None = None, render_page_indexes=None):
        assert Path(path).exists()
        assert expected_page_count == 3
        rendered.append(tuple(render_page_indexes))

    monkeypatch.setattr(service._validator, "validate", validate)

    service.merge_pdfs(plan)

    assert rendered == [(0, 1, 2)]


def test_merge_validation_reports_failing_output_page(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 2)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")

    def validate(*_args: object, **_kwargs: object) -> None:
        from pdf_workbench.services.pdf_document_validator import PdfDocumentValidationError

        raise PdfDocumentValidationError("3ページ目の描画検証に失敗しました")

    monkeypatch.setattr(service._validator, "validate", validate)

    with pytest.raises(PdfMergeError, match="3ページ目"):
        service.merge_pdfs(plan)


def test_merge_candidate_validation_rejects_missing_and_wrong_page_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    candidate = create_blank_pdf(tmp_path / "candidate.pdf", 1)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")

    with pytest.raises(PdfMergeError, match="作成されていません"):
        service._validate_candidate(plan, tmp_path / "missing.pdf", expected_pages=())

    monkeypatch.setattr(service._validator, "validate", lambda *_args, **_kwargs: None)
    with pytest.raises(PdfMergeError, match="ページ数"):
        service._validate_candidate(plan, candidate, expected_pages=())


def test_merge_candidate_validation_rejects_unsupported_root_entry(tmp_path: Path) -> None:
    candidate = create_blank_pdf(tmp_path / "candidate.pdf", 1)
    service = PdfMergeService()
    with pikepdf.open(candidate) as pdf:
        pdf.Root[pikepdf.Name("/OpenAction")] = pikepdf.Array(
            [pdf.pages[0].obj, pikepdf.Name("/Fit")]
        )

        with pytest.raises(PdfMergeError, match="/OpenAction"):
            service._reject_unsupported_candidate_root(pdf)


def test_merge_candidate_metadata_validation_rejects_source_fields_for_none_policy(
    tmp_path: Path,
) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    candidate = create_blank_pdf(tmp_path / "candidate.pdf", 1)
    with pikepdf.open(candidate, allow_overwriting_input=True) as pdf:
        pdf.docinfo[pikepdf.Name("/Title")] = pikepdf.String("Leaked")
        pdf.save(candidate)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")

    with pytest.raises(PdfMergeError, match="metadata"):
        service._validate_candidate_metadata(plan, candidate)


def test_merge_candidate_metadata_validation_rejects_custom_info_for_none_policy(
    tmp_path: Path,
) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    candidate = create_blank_pdf(tmp_path / "candidate.pdf", 1)
    with pikepdf.open(candidate, allow_overwriting_input=True) as pdf:
        pdf.docinfo[pikepdf.Name("/Custom")] = pikepdf.String("Nope")
        pdf.save(candidate)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")

    with pytest.raises(PdfMergeError, match="custom metadata"):
        service._validate_candidate_metadata(plan, candidate)


def test_merge_candidate_metadata_validation_rejects_selected_source_mismatch(
    tmp_path: Path,
) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    candidate = create_blank_pdf(tmp_path / "candidate.pdf", 1)
    with pikepdf.open(second, allow_overwriting_input=True) as pdf:
        pdf.docinfo[pikepdf.Name("/Title")] = pikepdf.String("Expected")
        pdf.save(second)
    service = PdfMergeService()
    inputs = tuple(service.read_merge_input(path) for path in (first, second))
    plan = build_pdf_merge_plan(
        inputs,
        tmp_path / "merged.pdf",
        metadata_policy=PdfMergeMetadataPolicy.SELECTED_SOURCE,
        metadata_source_path=second,
    )

    with pytest.raises(PdfMergeError, match="metadata"):
        service._validate_candidate_metadata(plan, candidate)


def test_merge_candidate_bookmark_validation_rejects_named_destination_tree(
    tmp_path: Path,
) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    candidate = create_blank_pdf(tmp_path / "candidate.pdf", 1)
    with pikepdf.open(candidate, allow_overwriting_input=True) as pdf:
        pdf.Root[pikepdf.Name("/Names")] = pikepdf.Dictionary(
            {
                "/Dests": pikepdf.Dictionary(
                    {
                        "/Names": pikepdf.Array(
                            [
                                pikepdf.String("chapter"),
                                pikepdf.Array([pdf.pages[0].obj, pikepdf.Name("/Fit")]),
                            ]
                        )
                    }
                )
            }
        )
        pdf.save(candidate)
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")

    with pytest.raises(PdfMergeError, match="named destination"):
        service._validate_candidate_bookmarks(plan, candidate)


def test_merge_candidate_bookmark_validation_rejects_leftover_outline(
    tmp_path: Path,
) -> None:
    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    candidate = create_outline_pdf(tmp_path / "candidate.pdf", "Unexpected")
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")

    with pytest.raises(PdfMergeError, match="bookmark"):
        service._validate_candidate_bookmarks(plan, candidate)


def test_merge_candidate_page_semantic_mismatch_is_rejected(tmp_path: Path) -> None:
    first = create_simple_text_pdf(tmp_path / "first.pdf", ["A"])
    second = create_simple_text_pdf(tmp_path / "second.pdf", ["B"])
    candidate = create_simple_text_pdf(tmp_path / "candidate.pdf", ["changed", "B"])
    service = PdfMergeService()
    plan = build_plan(service, (first, second), tmp_path / "merged.pdf")
    source_structure = service._mutation_service.snapshot_document_structure(first)
    second_structure = service._mutation_service.snapshot_document_structure(second)
    candidate_structure = service._mutation_service.snapshot_document_structure(candidate)
    expected = (
        service._merge_page_snapshot(
            source_structure.pages[0],
            source_path=first.resolve(),
            source_page_index=0,
            output_page_index=0,
        ),
        service._merge_page_snapshot(
            second_structure.pages[0],
            source_path=second.resolve(),
            source_page_index=0,
            output_page_index=1,
        ),
    )

    with pytest.raises(PdfMergeError, match="1ページ目"):
        service._validate_candidate_pages(plan, candidate_structure, expected)


def test_merge_destination_validation_rejects_bad_type_and_parameter_count() -> None:
    service = PdfMergeService()

    with pytest.raises(PdfMergeError, match="未対応"):
        service._validate_destination_array(pikepdf.Array([0, pikepdf.Name("/FitFoo")]))
    with pytest.raises(PdfMergeError, match="parameter"):
        service._validate_destination_array(pikepdf.Array([0, pikepdf.Name("/FitH"), 10, 20]))


def test_merge_destination_resolution_rejects_empty_and_missing_named_destination() -> None:
    service = PdfMergeService()

    with pytest.raises(PdfMergeError, match="解決できない"):
        service._offset_outline_destination(
            None,
            output_start_index=0,
            page_objgen_to_index={},
            named_destinations={},
        )
    with pytest.raises(PdfMergeError, match="空"):
        service._offset_outline_destination(
            pikepdf.Array(),
            output_start_index=0,
            page_objgen_to_index={},
            named_destinations={},
        )
    with pytest.raises(PdfMergeError, match="named destination"):
        service._offset_outline_destination(
            pikepdf.String("missing"),
            output_start_index=0,
            page_objgen_to_index={},
            named_destinations={},
        )


def test_merge_atomic_io_failures_are_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = PdfMergeService()
    candidate = tmp_path / "candidate.pdf"
    target = tmp_path / "target.pdf"
    candidate.write_bytes(b"x")
    target.write_bytes(b"y")

    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(PdfMergeError, match="置換"):
        service._replace_atomically(candidate, target)

    monkeypatch.setattr(os, "chmod", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(PdfMergeError, match="属性"):
        service._apply_existing_target_mode(candidate, target)


def test_merge_resolves_named_destination_to_explicit_output_destination(tmp_path: Path) -> None:
    first = create_outline_pdf(tmp_path / "first.pdf", "First")
    second = create_outline_pdf(tmp_path / "second.pdf", "Second")
    with pikepdf.open(first, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0].obj
        dest = pikepdf.Array([page, pikepdf.Name("/Fit")])
        pdf.Root[pikepdf.Name("/Names")] = pikepdf.Dictionary(
            {
                "/Dests": pikepdf.Dictionary(
                    {"/Names": pikepdf.Array([pikepdf.String("chapter"), dest])}
                )
            }
        )
        with pdf.open_outline() as outline:
            outline.root.clear()
            outline.root.append(pikepdf.OutlineItem("Named", pikepdf.String("chapter")))
        pdf.save(first)
    service = PdfMergeService()
    inputs = tuple(service.read_merge_input(path) for path in (first, second))
    plan = build_pdf_merge_plan(
        inputs,
        tmp_path / "merged.pdf",
        bookmark_policy=PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE,
    )

    service.merge_pdfs(plan)

    with pikepdf.open(plan.output_path) as pdf:
        assert "/Names" not in pdf.Root
    outline = PdfReader(plan.output_path).outline
    assert outline[1][0].title == "Named"


def test_merge_resolves_legacy_catalog_destination(tmp_path: Path) -> None:
    first = create_outline_pdf(tmp_path / "first.pdf", "First")
    second = create_outline_pdf(tmp_path / "second.pdf", "Second")
    with pikepdf.open(first, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0].obj
        pdf.Root[pikepdf.Name("/Dests")] = pikepdf.Dictionary(
            {"/chapter": pikepdf.Dictionary({"/D": pikepdf.Array([page, pikepdf.Name("/Fit")])})}
        )
        with pdf.open_outline() as outline:
            outline.root.clear()
            outline.root.append(pikepdf.OutlineItem("Legacy", pikepdf.Name("/chapter")))
        pdf.save(first)
    service = PdfMergeService()
    inputs = tuple(service.read_merge_input(path) for path in (first, second))
    plan = build_pdf_merge_plan(
        inputs,
        tmp_path / "merged.pdf",
        bookmark_policy=PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE,
    )

    service.merge_pdfs(plan)

    outline = PdfReader(plan.output_path).outline
    assert outline[1][0].title == "Legacy"


def test_merge_rejects_unresolved_named_destination(tmp_path: Path) -> None:
    first = create_outline_pdf(tmp_path / "first.pdf", "First")
    second = create_outline_pdf(tmp_path / "second.pdf", "Second")
    with pikepdf.open(first, allow_overwriting_input=True) as pdf:
        with pdf.open_outline() as outline:
            outline.root.clear()
            outline.root.append(pikepdf.OutlineItem("Missing", pikepdf.String("missing")))
        pdf.save(first)
    service = PdfMergeService()
    inputs = tuple(service.read_merge_input(path) for path in (first, second))
    plan = build_pdf_merge_plan(
        inputs,
        tmp_path / "merged.pdf",
        bookmark_policy=PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE,
    )

    with pytest.raises(PdfMergeError, match="named destination"):
        service.merge_pdfs(plan)


def test_merge_rejects_malformed_name_tree(tmp_path: Path) -> None:
    first = create_outline_pdf(tmp_path / "first.pdf", "First")
    second = create_outline_pdf(tmp_path / "second.pdf", "Second")
    with pikepdf.open(first, allow_overwriting_input=True) as pdf:
        pdf.Root[pikepdf.Name("/Names")] = pikepdf.Dictionary(
            {"/Dests": pikepdf.Dictionary({"/Names": pikepdf.Array([pikepdf.String("odd")])})}
        )
        with pdf.open_outline() as outline:
            outline.root.clear()
            outline.root.append(pikepdf.OutlineItem("Odd", pikepdf.String("odd")))
        pdf.save(first)
    service = PdfMergeService()
    inputs = tuple(service.read_merge_input(path) for path in (first, second))
    plan = build_pdf_merge_plan(
        inputs,
        tmp_path / "merged.pdf",
        bookmark_policy=PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE,
    )

    with pytest.raises(PdfMergeError, match="Names"):
        service.merge_pdfs(plan)


def test_merge_large_documents_without_rasterizing_all_pages(tmp_path: Path) -> None:
    paths = tuple(create_blank_pdf(tmp_path / f"source-{index}.pdf", 75) for index in range(3))
    service = PdfMergeService()
    plan = build_plan(service, paths, tmp_path / "merged.pdf")

    result = service.merge_pdfs(plan)

    assert result.total_page_count == 225
    assert len(PdfReader(result.target_path).pages) == 225
    assert not list(tmp_path.glob("*.merge-source.tmp.pdf"))
