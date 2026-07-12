from __future__ import annotations

from pathlib import Path

from pdf_workbench.__main__ import (
    _discard_candidate,
    _handle_startup_recovery,
    _perform_initial_document_open,
    _restore_candidate,
    build_parser,
)
from pdf_workbench.ui.dialogs.recovery_dialog import RecoveryDialogAction, RecoveryDialogResult


class FakeWindow:
    def __init__(self) -> None:
        self.opened: list[Path] = []
        self.restored: list[object] = []
        self.errors: list[str] = []

    def open_document(self, path: Path) -> None:
        self.opened.append(path)

    def restore_session(self, session: object) -> bool:
        self.restored.append(session)
        return True


class FakeCandidate:
    def __init__(self, name: str) -> None:
        self.workspace_directory = Path(f"/tmp/{name}")


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


def test_initial_open_performs_recovery_before_cli_pdf(monkeypatch) -> None:
    call_order: list[str] = []
    window = FakeWindow()

    def record_recovery(_window: object, _service: object) -> None:
        call_order.append("recovery")

    monkeypatch.setattr("pdf_workbench.__main__._handle_startup_recovery", record_recovery)
    cli_pdf = Path("/tmp/example.pdf")

    _perform_initial_document_open(
        window,  # type: ignore[arg-type]
        recovery_service=object(),  # type: ignore[arg-type]
        cli_pdf=cli_pdf,
        skip_recovery_prompt=False,
    )

    assert call_order == ["recovery"]
    assert window.opened == [cli_pdf]


def test_initial_open_skips_recovery_when_requested(monkeypatch) -> None:
    call_order: list[str] = []
    window = FakeWindow()

    def record_recovery(_window: object, _service: object) -> None:
        call_order.append("recovery")

    monkeypatch.setattr("pdf_workbench.__main__._handle_startup_recovery", record_recovery)
    cli_pdf = Path("/tmp/example.pdf")

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

    _restore_candidate(window, FakeRecoveryService(), candidate)  # type: ignore[arg-type]

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

    _restore_candidate(window, FakeRecoveryService(), candidate)  # type: ignore[arg-type]

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
