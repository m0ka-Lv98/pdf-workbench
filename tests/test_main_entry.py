from __future__ import annotations

import json
from pathlib import Path

from pdf_test_utils import create_unfilterable_resource_stream_pdf
from pdf_workbench.__main__ import (
    _discard_candidate,
    _handle_startup_recovery,
    _perform_initial_document_open,
    _restore_candidate,
    _run_page_mutation_smoke,
    build_parser,
)
from pdf_workbench.ui.dialogs.recovery_dialog import RecoveryDialogAction, RecoveryDialogResult
from pdf_workbench.ui.main_window import RestoreSessionResult


class FakeWindow:
    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.restored: list[object] = []
        self.errors: list[str] = []

    def open_document(self, path: Path) -> None:
        self.opened.append(path)

    def restore_session(self, session: object) -> RestoreSessionResult:
        self.restored.append(session)
        return RestoreSessionResult.ATTACHED


class FakeCandidate:
    def __init__(self, name: str, *, source_path: Path | None = None) -> None:
        self.workspace_directory = Path(f"/tmp/{name}")
        self.metadata = type(
            "Metadata",
            (),
            {"source_path": source_path or Path(f"/tmp/{name}.pdf")},
        )()


class FakeRecoveryDialog:
    next_result = RecoveryDialogResult(RecoveryDialogAction.LATER, [])

    def __init__(self, _candidates: list[object], _parent: object) -> None:
        self.result_value = self.next_result

    def show(self) -> None:
        return None

    def raise_(self) -> None:
        return None

    def activateWindow(self) -> None:
        return None

    def exec(self) -> int:
        return 0


def test_build_parser_supports_skip_recovery_prompt() -> None:
    parser = build_parser()
    args = parser.parse_args(["--skip-recovery-prompt"])

    assert args.skip_recovery_prompt is True


def test_page_mutation_smoke_preserves_source_and_restores_structure(tmp_path: Path) -> None:
    source = create_unfilterable_resource_stream_pdf(tmp_path / "source.pdf", 3)
    result_path = tmp_path / "page-mutation-smoke.json"

    exit_code = _run_page_mutation_smoke(source, result_path)

    assert exit_code == 0
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["duplicate"] == "success"
    assert payload["undo_duplicate"] == "success"
    assert payload["redo_duplicate"] == "success"
    assert payload["final_undo_duplicate"] == "success"
    assert payload["delete"] == "success"
    assert payload["undo_delete"] == "success"
    assert payload["redo_delete"] == "success"
    assert payload["final_undo_delete"] == "success"
    assert payload["duplicate_render_digest_matches_source"] is True
    assert payload["delete_survivor_render_digest_matches_source"] is True
    assert payload["undo_delete_render_digests_restored"] is True
    assert payload["working_copy_render_digests_restored"] is True
    assert payload["working_copy_structure_restored"] is True
    assert payload["source_unchanged"] is True
    assert payload["candidate_cleanup"] is True
    assert payload["delete_undo_snapshot_cleanup"] is True
    assert payload["insert_replace_snapshot_cleanup"] is True
    assert payload["working_directory_contains_only_working_pdf"] is True


def test_initial_open_performs_recovery_before_cli_pdf(
    monkeypatch,
    tmp_path: Path,
) -> None:
    call_order: list[str] = []
    window = FakeWindow()

    def record_recovery(_window: object, _service: object) -> None:
        call_order.append("recovery")

    monkeypatch.setattr("pdf_workbench.__main__._handle_startup_recovery", record_recovery)
    cli_pdf = (tmp_path / "example.pdf").resolve()

    _perform_initial_document_open(
        window,  # type: ignore[arg-type]
        recovery_service=object(),  # type: ignore[arg-type]
        cli_pdf=cli_pdf,
        skip_recovery_prompt=False,
    )

    assert call_order == ["recovery"]
    assert window.opened == [cli_pdf]


def test_initial_open_skips_recovery_when_requested(
    monkeypatch,
    tmp_path: Path,
) -> None:
    call_order: list[str] = []
    window = FakeWindow()

    def record_recovery(_window: object, _service: object) -> None:
        call_order.append("recovery")

    monkeypatch.setattr("pdf_workbench.__main__._handle_startup_recovery", record_recovery)
    cli_pdf = (tmp_path / "example.pdf").resolve()

    _perform_initial_document_open(
        window,  # type: ignore[arg-type]
        recovery_service=object(),  # type: ignore[arg-type]
        cli_pdf=cli_pdf,
        skip_recovery_prompt=True,
    )

    assert call_order == []
    assert window.opened == [cli_pdf]


def test_handle_startup_recovery_recovers_selected_and_releases_others(monkeypatch) -> None:
    first = FakeCandidate("first")
    second = FakeCandidate("second")
    released: list[FakeCandidate] = []
    restored: list[FakeCandidate] = []
    window = FakeWindow()

    class FakeRecoveryService:
        def scan_candidates(self):
            class Result:
                def __init__(self) -> None:
                    self.candidates = [first, second]

            return Result()

        def release_candidate(self, candidate: FakeCandidate) -> None:
            released.append(candidate)

    monkeypatch.setattr("pdf_workbench.__main__.RecoveryDialog", FakeRecoveryDialog)
    monkeypatch.setattr(
        "pdf_workbench.__main__._restore_candidate",
        lambda _window, _service, candidate: restored.append(candidate),
    )
    FakeRecoveryDialog.next_result = RecoveryDialogResult(RecoveryDialogAction.RECOVER, [first])

    _handle_startup_recovery(window, FakeRecoveryService())  # type: ignore[arg-type]

    assert restored == [first]
    assert released == [second]


def test_handle_startup_recovery_keeps_second_duplicate_candidate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_path = (tmp_path / "shared.pdf").resolve()
    first = FakeCandidate("first", source_path=source_path)
    second = FakeCandidate("second", source_path=source_path)
    released: list[FakeCandidate] = []
    restored: list[FakeCandidate] = []
    messages: list[str] = []
    window = FakeWindow()

    class FakeRecoveryService:
        def scan_candidates(self):
            class Result:
                def __init__(self) -> None:
                    self.candidates = [first, second]

            return Result()

        def release_candidate(self, candidate: FakeCandidate) -> None:
            released.append(candidate)

    monkeypatch.setattr("pdf_workbench.__main__.RecoveryDialog", FakeRecoveryDialog)
    monkeypatch.setattr(
        "pdf_workbench.__main__._restore_candidate",
        lambda _window, _service, candidate: (
            restored.append(candidate),
            RestoreSessionResult.ATTACHED,
        )[1],
    )
    monkeypatch.setattr(
        "pdf_workbench.__main__.QMessageBox.information",
        lambda _parent, _title, message: messages.append(message),
    )
    FakeRecoveryDialog.next_result = RecoveryDialogResult(
        RecoveryDialogAction.RECOVER,
        [first, second],
    )

    _handle_startup_recovery(window, FakeRecoveryService())  # type: ignore[arg-type]

    assert restored == [first]
    assert released == [second]
    assert messages == [
        "同じ元ファイルの復旧セッションが既に開かれているため、この候補は後で復旧できるよう保持しました。"
    ]


def test_restore_candidate_passes_restored_session_to_window(monkeypatch) -> None:
    candidate = FakeCandidate("restore")
    session = object()
    window = FakeWindow()

    class FakeRecoveryService:
        def restore_candidate(self, _candidate: FakeCandidate) -> object:
            return session

        def release_candidate(self, _candidate: FakeCandidate) -> None:
            raise AssertionError("candidate should not be released on success")

    monkeypatch.setattr("pdf_workbench.__main__.QMessageBox.critical", lambda *args, **kwargs: 0)

    result = _restore_candidate(window, FakeRecoveryService(), candidate)  # type: ignore[arg-type]

    assert result is RestoreSessionResult.ATTACHED
    assert window.restored == [session]


def test_restore_candidate_releases_candidate_and_reports_error(monkeypatch) -> None:
    candidate = FakeCandidate("broken")
    released: list[FakeCandidate] = []
    reported: list[str] = []
    window = FakeWindow()

    class FakeRecoveryService:
        def restore_candidate(self, _candidate: FakeCandidate) -> object:
            raise RuntimeError("boom")

        def release_candidate(self, restored_candidate: FakeCandidate) -> None:
            released.append(restored_candidate)

    monkeypatch.setattr(
        "pdf_workbench.__main__.QMessageBox.critical",
        lambda _parent, _title, message: reported.append(message),
    )

    result = _restore_candidate(window, FakeRecoveryService(), candidate)  # type: ignore[arg-type]

    assert result is RestoreSessionResult.FAILED
    assert released == [candidate]
    assert reported == ["boom"]


def test_discard_candidate_reports_error(monkeypatch) -> None:
    candidate = FakeCandidate("discard")
    reported: list[str] = []
    window = FakeWindow()

    class FakeRecoveryService:
        def discard_candidate(self, _candidate: FakeCandidate) -> None:
            raise RuntimeError("discard failed")

    monkeypatch.setattr(
        "pdf_workbench.__main__.QMessageBox.critical",
        lambda _parent, _title, message: reported.append(message),
    )

    _discard_candidate(window, FakeRecoveryService(), candidate)  # type: ignore[arg-type]

    assert reported == ["discard failed"]
