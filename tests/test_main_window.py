from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QImage, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTabBar,
    QToolButton,
    QWidget,
)
from pytestqt.qtbot import QtBot

from pdf_regression_utils import extract_pdfium_text, file_sha256
from pdf_test_utils import (
    copy_pdf_fixture,
    create_blank_pdf,
    create_image_only_pdf,
    create_qt_text_pdf,
    create_simple_text_pdf,
)
from pdf_workbench.domain.command_history import (
    CommandChange,
    DocumentCommand,
)
from pdf_workbench.domain.document_session import DocumentSession, FileFingerprint, SourceStatus
from pdf_workbench.domain.mutation import PageIndexTransition, WorkingCopyMutationResult
from pdf_workbench.domain.page_commands import DeletePagesCommand, RotatePagesCommand
from pdf_workbench.services.page_coordinates import PageMetadata
from pdf_workbench.services.pdf_page_mutation import (
    PdfPageMutationError,
    PdfPageMutationService,
)
from pdf_workbench.services.pdf_renderer import (
    DocumentMetadata,
    DocumentReleaseResult,
    DocumentRevision,
    PageTextIndex,
    PdfiumDocumentBackend,
    PdfRenderService,
)
from pdf_workbench.services.pdf_save_service import (
    PdfSaveError,
    PdfSaveService,
    SaveResult,
    TargetChangedError,
    TargetSnapshot,
)
from pdf_workbench.services.session_recovery import (
    RecoveryMetadataError,
    SessionRecoveryService,
)
from pdf_workbench.services.session_workspace import SessionWorkspaceManager
from pdf_workbench.services.source_change_monitor import (
    SourceChangeMonitor,
    SourceCheckResult,
)
from pdf_workbench.ui.main_window import DocumentTab, MainWindow, RestoreSessionResult
from pdf_workbench.ui.pdf_view import PdfView, PdfViewMutationSnapshot
from pdf_workbench.ui.widgets.search_bar import SearchBar, SearchInputSurface


def patch_pdf_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_open_document(self: PdfView, path: Path) -> None:
        self._path = path
        self._current_page_index = 0
        self._metadata = DocumentMetadata(
            revision=DocumentRevision.from_path(path),
            pages=(PageMetadata.from_size(144.0, 144.0),),
        )
        self.state_changed.emit()

    monkeypatch.setattr(PdfView, "open_document", fake_open_document)


def create_settings(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "app.ini"), QSettings.Format.IniFormat)


def create_workspace_manager(tmp_path: Path) -> SessionWorkspaceManager:
    return SessionWorkspaceManager(tmp_path / "sessions")


def show_window(qtbot: QtBot, window: MainWindow) -> None:
    window.show()
    qtbot.waitUntil(window.isVisible)


def assert_search_ui_ready(window: MainWindow) -> None:
    assert window._search_toolbar is not None
    assert window._search_surface is not None
    assert window._search_toolbar.isVisible()
    assert window._search_surface.isVisible()
    assert window._search_bar.isVisible()
    assert window._search_bar.search_input_surface.isVisible()
    assert window._search_bar.search_input.isVisible()
    if QApplication.platformName() != "offscreen":
        assert window._search_bar.search_input.hasFocus()
        assert window._search_bar.search_input_surface.property("focused") is True
    assert window._search_toolbar.geometry().width() > 0
    assert window._search_toolbar.geometry().height() > 0
    assert window._search_surface.geometry().width() > 0
    assert window._search_surface.geometry().height() > 0
    assert window._search_bar.geometry().width() > 0
    assert window._search_bar.geometry().height() > 0
    assert window._search_bar.search_input_surface.geometry().width() > 0
    assert window._search_bar.search_input_surface.geometry().height() == 40
    assert window._search_bar.search_input.geometry().width() > 0
    assert window._search_bar.search_input.geometry().height() == 28
    assert window._search_toolbar.geometry().height() >= window._search_bar.geometry().height()
    assert (
        window._search_bar.geometry().height()
        >= window._search_bar.search_input.geometry().height()
    )
    assert window._search_surface.geometry().height() >= window._search_bar.sizeHint().height()
    assert window._search_surface.geometry().height() >= window._search_bar.geometry().height()
    search_top = window._search_toolbar.mapTo(
        window,
        window._search_toolbar.rect().topLeft(),
    ).y()
    if window._main_toolbar is not None:
        assert search_top >= window._main_toolbar.geometry().bottom()
    tab_bar_bottom = (
        window._tabs.tabBar()
        .mapTo(
            window,
            window._tabs.tabBar().rect().bottomLeft(),
        )
        .y()
    )
    assert search_top >= tab_bar_bottom
    toolbar_right = window._search_toolbar.rect().right()
    surface_right = window._search_surface.geometry().right()
    assert toolbar_right - surface_right < 40
    input_surface_rect = window._search_bar.search_input_surface.rect()
    child_widgets = (
        window._search_bar.search_icon,
        window._search_bar.search_input,
        window._search_bar.previous_button,
        window._search_bar.next_button,
        window._search_bar.close_button,
        window._search_bar.counter_label,
    )
    for widget in (
        window._search_bar.search_input_surface,
        window._search_bar.previous_button,
        window._search_bar.next_button,
        window._search_bar.close_button,
        window._search_bar.counter_label,
    ):
        top_left = widget.mapTo(window._search_surface, widget.rect().topLeft())
        bottom_right = widget.mapTo(window._search_surface, widget.rect().bottomRight())
        child_rect = QRect(top_left, bottom_right)
        assert window._search_surface.rect().contains(child_rect)
    input_widgets: tuple[QWidget, ...] = child_widgets[:2]
    for input_widget in input_widgets:
        top_left = input_widget.mapTo(
            window._search_bar.search_input_surface,
            input_widget.rect().topLeft(),
        )
        bottom_right = input_widget.mapTo(
            window._search_bar.search_input_surface,
            input_widget.rect().bottomRight(),
        )
        child_rect = QRect(top_left, bottom_right)
        assert input_surface_rect.contains(child_rect)
    surface_center_y = window._search_bar.search_input_surface.geometry().center().y()
    center_aligned_widgets: tuple[QWidget, ...] = (
        window._search_bar.search_icon,
        window._search_bar.search_input,
    )
    for center_widget in center_aligned_widgets:
        assert abs(center_widget.geometry().center().y() - surface_center_y) <= 1


def assert_button_icon_valid(button: QToolButton) -> None:
    icon = button.icon()
    assert not icon.isNull()
    pixmap = icon.pixmap(16, 16)
    assert not pixmap.isNull()


class DelayedTextBackend(PdfiumDocumentBackend):
    def __init__(self, path: Path, delay_seconds: float) -> None:
        super().__init__(path)
        self._delay_seconds = delay_seconds

    def extract_text_page(
        self,
        page_index: int,
        revision: DocumentRevision,
    ) -> PageTextIndex:
        time.sleep(self._delay_seconds)
        return super().extract_text_page(page_index, revision)


def create_real_main_window(
    qtbot: QtBot,
    tmp_path: Path,
    *,
    delay_seconds: float = 0.0,
) -> MainWindow:
    settings = create_settings(tmp_path)

    def backend_factory(path: Path) -> PdfiumDocumentBackend:
        if delay_seconds > 0:
            return DelayedTextBackend(path, delay_seconds)
        return PdfiumDocumentBackend(path)

    service = PdfRenderService(backend_factory=backend_factory)
    window = MainWindow(
        settings,
        render_service=service,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=PdfSaveService(),
    )
    service.setParent(window)
    window.destroyed.connect(lambda *_args, _service=service: _service.shutdown())
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(window.isVisible)
    return window


class FakeSaveService(PdfSaveService):
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[tuple[Path, int]] = []

    def save_atomic(
        self,
        session: DocumentSession,
        target_path: Path,
        expected_page_count: int,
        target_snapshot: object,
    ) -> SaveResult:
        self.calls.append((target_path.expanduser().resolve(), expected_page_count))
        if self.should_fail:
            raise PdfSaveError("保存テスト失敗")
        resolved_target = target_path.expanduser().resolve()
        resolved_target.write_bytes(session.document_path.read_bytes())
        fingerprint = FileFingerprint.from_path(resolved_target)
        saved_at = datetime.now(UTC)
        session.mark_saved(resolved_target, fingerprint, saved_at)
        return SaveResult(
            target_path=resolved_target,
            fingerprint=fingerprint,
            saved_at=saved_at,
        )


class SnapshotRecordingSaveService(PdfSaveService):
    def __init__(self, replacement_pdf: Path | None = None) -> None:
        super().__init__()
        self._replacement_pdf = replacement_pdf
        self.snapshots: list[TargetSnapshot] = []
        self.calls: list[Path] = []

    def save_atomic(
        self,
        session: DocumentSession,
        target_path: Path,
        expected_page_count: int,
        target_snapshot: object,
    ) -> SaveResult:
        assert isinstance(target_snapshot, TargetSnapshot)
        self.snapshots.append(target_snapshot)
        self.calls.append(target_path.expanduser().resolve())
        if self._replacement_pdf is not None:
            target_path.write_bytes(self._replacement_pdf.read_bytes())
        return super().save_atomic(
            session,
            target_path,
            expected_page_count,
            target_snapshot,
        )


class TargetChangedSaveService(PdfSaveService):
    def save_atomic(
        self,
        session: DocumentSession,
        target_path: Path,
        expected_page_count: int,
        target_snapshot: object,
    ) -> SaveResult:
        raise TargetChangedError("changed elsewhere")


class RaceTargetChangedSaveService(PdfSaveService):
    def __init__(self, replacement_pdf: Path) -> None:
        super().__init__()
        self._replacement_pdf = replacement_pdf

    def save_atomic(
        self,
        session: DocumentSession,
        target_path: Path,
        expected_page_count: int,
        target_snapshot: object,
    ) -> SaveResult:
        assert isinstance(target_snapshot, TargetSnapshot)
        assert target_snapshot.fingerprint == session.source_fingerprint
        target_path.write_bytes(self._replacement_pdf.read_bytes())
        return super().save_atomic(
            session,
            target_path,
            expected_page_count,
            target_snapshot,
        )


class FailingSourceChangeMonitor(SourceChangeMonitor):
    def register_session(self, session: DocumentSession) -> None:
        raise RuntimeError(f"monitor failure: {session.session_id}")


class FakeRecoveryService(SessionRecoveryService):
    def __init__(self) -> None:
        self.write_calls: list[str] = []
        self.should_fail = False

    def write_metadata(self, session: DocumentSession) -> None:
        self.write_calls.append(session.session_id)
        if self.should_fail:
            raise RecoveryMetadataError("metadata failure")


class TrackingWorkspaceManager(SessionWorkspaceManager):
    def __init__(self, sessions_root: Path) -> None:
        super().__init__(sessions_root)
        self.cleaned_sessions: list[str] = []

    def cleanup_session(self, session: DocumentSession) -> None:
        self.cleaned_sessions.append(session.session_id)
        super().cleanup_session(session)


class StubDocumentCommand(DocumentCommand):
    def __init__(
        self,
        description: str,
        *,
        affected_pages: frozenset[int] | None = None,
        fail_execute: bool = False,
        fail_undo: bool = False,
        fail_redo: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.description = description
        self.affected_pages = affected_pages
        self._fail_execute = fail_execute
        self._fail_undo = fail_undo
        self._fail_redo = fail_redo
        self._events = events if events is not None else []

    def execute(self) -> CommandChange:
        self._events.append(f"execute:{self.description}")
        if self._fail_execute:
            raise RuntimeError(f"execute failed: {self.description}")
        return CommandChange.from_command(self)

    def undo(self) -> CommandChange:
        self._events.append(f"undo:{self.description}")
        if self._fail_undo:
            raise RuntimeError(f"undo failed: {self.description}")
        return CommandChange.from_command(self)

    def redo(self) -> CommandChange:
        self._events.append(f"redo:{self.description}")
        if self._fail_redo:
            raise RuntimeError(f"redo failed: {self.description}")
        return CommandChange.from_command(self)


class CountingDocumentCommand(StubDocumentCommand):
    def __init__(self, description: str) -> None:
        super().__init__(description)
        self.execute_calls = 0
        self.undo_calls = 0
        self.redo_calls = 0

    def execute(self) -> CommandChange:
        self.execute_calls += 1
        return super().execute()

    def undo(self) -> CommandChange:
        self.undo_calls += 1
        return super().undo()

    def redo(self) -> CommandChange:
        self.redo_calls += 1
        return super().redo()


class MutatingStubDocumentCommand(StubDocumentCommand):
    mutates_working_copy = True
    requires_document_reload = True

    def __init__(self, description: str, mutation_result: WorkingCopyMutationResult) -> None:
        super().__init__(description, affected_pages=mutation_result.affected_pages)
        self.last_mutation_result = mutation_result

    def execute(self) -> CommandChange:
        return CommandChange.from_command(self)


def test_main_window_opens_and_closes_multiple_documents(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)

    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.touch()
    second.touch()

    window.open_document(first)
    window.open_document(second)

    assert window._tabs.count() == 2
    assert window._documents[0].session.source_path == first.resolve()
    assert window._documents[1].session.source_path == second.resolve()
    assert window._stack.currentWidget() is window._tabs
    assert window._tabs.tabBar().elideMode() == Qt.TextElideMode.ElideMiddle
    assert window._tabs.tabBar().usesScrollButtons() is True
    assert window._tabs.tabsClosable() is False
    assert 32 <= window._tabs.tabBar().height() <= 42
    assert window._tabs.tabBar().drawBase() is False
    close_button = window._tabs.tabBar().tabButton(0, QTabBar.ButtonPosition.RightSide)
    assert isinstance(close_button, QToolButton)
    assert close_button.objectName() == "tabCloseButton"

    assert window.close_document_at(1) is True
    assert window._tabs.count() == 1
    assert window._documents[0].session.source_path == first.resolve()


def test_main_window_requires_confirmation_for_modified_document(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=FakeSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "modified.pdf", 1)
    window.open_document(document_path)
    window._documents[0].session.mark_modified("test change")

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Cancel,
    )
    assert window.close_current_document() is False
    assert window._tabs.count() == 1

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    assert window.close_current_document() is True
    assert window._tabs.count() == 0


def test_main_window_save_actions_follow_modified_state(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=FakeSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "save-state.pdf", 1)
    window.open_document(document_path)

    assert window.save_action.isEnabled() is False
    assert window.save_as_action.isEnabled() is True

    document = window._documents[0]
    document.session.mark_modified("page operation")
    window._update_actions()

    assert window.save_action.isEnabled() is True
    assert window.save_as_action.isEnabled() is True

    document.view.set_rotation(90)
    assert document.session.is_modified is True
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    assert window.close_current_document() is True


def test_main_window_save_shortcut_clears_modified_marker(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    save_service = FakeSaveService()
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=save_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "shortcut-save.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    document.session.mark_modified("edit")
    window._update_actions()

    QTest.keySequence(window, QKeySequence.StandardKey.Save)

    assert document.session.is_modified is False
    assert not window._tab_title(document).endswith("*")
    assert window.save_action.isEnabled() is False
    assert save_service.calls[0][0] == document_path.resolve()


def test_main_window_save_shortcut_routes_recovered_session_to_save_as(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    save_service = FakeSaveService()
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=save_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "recovered-source.pdf", 1)
    target_path = tmp_path / "recovered-saved.pdf"
    window.open_document(document_path)
    document = window._documents[0]
    document.session.mark_recovered(SourceStatus.MISSING)
    window._update_actions()

    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(target_path), "PDF files (*.pdf)"),
    )

    QTest.keySequence(window, QKeySequence.StandardKey.Save)

    assert save_service.calls[0][0] == target_path.resolve()
    assert document.session.source_path == target_path.resolve()
    assert document.session.requires_save_as is False


def test_main_window_save_as_updates_tab_title_and_recent_file(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    save_service = FakeSaveService()
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=save_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    document.session.mark_modified("edit")

    target_path = tmp_path / "saved-as.pdf"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(target_path), "PDF files (*.pdf)"),
    )

    assert window._save_current_document_as() is True

    assert document.session.source_path == target_path.resolve()
    assert window._tabs.tabText(0) == "saved-as.pdf"
    assert window._recent_files[0] == target_path.resolve()


def test_main_window_open_document_cleans_workspace_when_metadata_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    recovery_service = FakeRecoveryService()
    recovery_service.should_fail = True
    window = MainWindow(
        settings,
        workspace_manager=workspace_manager,
        save_service=FakeSaveService(),
        recovery_service=recovery_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )

    document_path = create_blank_pdf(tmp_path / "metadata-fail.pdf", 1)
    window.open_document(document_path)

    assert window._tabs.count() == 0
    assert list(workspace_manager.sessions_root.iterdir()) == []


def test_main_window_debounces_recovery_metadata_for_navigation(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    recovery_service = FakeRecoveryService()
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=FakeSaveService(),
        recovery_service=recovery_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "debounce.pdf", 1)
    window.open_document(document_path)
    recovery_service.write_calls.clear()

    window._next_page()
    window._previous_page()
    window._set_zoom_from_toolbar(1.25)

    assert recovery_service.write_calls == []
    qtbot.waitUntil(lambda: len(recovery_service.write_calls) == 1, timeout=2000)


def test_main_window_save_as_shortcut_uses_selected_destination(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    save_service = FakeSaveService()
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=save_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "shortcut-save-as-source.pdf", 1)
    target_path = tmp_path / "shortcut-save-as-target.pdf"
    window.open_document(document_path)
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(target_path), "PDF files (*.pdf)"),
    )

    QTest.keySequence(window, QKeySequence.StandardKey.SaveAs)

    assert save_service.calls[0][0] == target_path.resolve()
    assert window._documents[0].session.source_path == target_path.resolve()


def test_main_window_save_as_rejects_target_open_in_another_tab(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    save_service = FakeSaveService()
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=save_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    window.open_document(first)
    window.open_document(second)
    document = window._documents[1]
    document.session.mark_modified("edit")

    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(first), "PDF files (*.pdf)"),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )

    assert window._save_current_document_as() is False
    assert document.session.is_modified is True
    assert save_service.calls == []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    assert window.close_current_document() is True


@pytest.mark.parametrize(
    "target_factory",
    [
        lambda document, manager, root: document.session.working_copy_path,
        lambda document, manager, root: document.session.workspace_directory / "other.pdf",
        lambda document, manager, root: manager.sessions_root / "root-target.pdf",
    ],
)
def test_main_window_save_as_rejects_managed_paths(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
    target_factory: Callable[[DocumentTab, SessionWorkspaceManager, Path], Path],
) -> None:
    patch_pdf_open(monkeypatch)
    save_service = FakeSaveService()
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=workspace_manager,
        save_service=save_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "managed-path-source.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    document.session.mark_modified("edit")
    original_source_path = document.session.source_path
    original_fingerprint = document.session.source_fingerprint
    original_working_copy_bytes = document.session.document_path.read_bytes()

    target_path = target_factory(document, workspace_manager, tmp_path)
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(target_path), "PDF files (*.pdf)"),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )

    assert window._save_current_document_as() is False
    assert save_service.calls == []
    assert document.session.is_modified is True
    assert document.session.source_path == original_source_path
    assert document.session.source_fingerprint == original_fingerprint
    assert document.session.document_path.read_bytes() == original_working_copy_bytes
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    assert window.close_current_document() is True


def test_main_window_save_as_rejects_other_session_workspace_path(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    save_service = FakeSaveService()
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=workspace_manager,
        save_service=save_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    first = create_blank_pdf(tmp_path / "first.pdf", 1)
    second = create_blank_pdf(tmp_path / "second.pdf", 1)
    window.open_document(first)
    window.open_document(second)
    first_document = window._documents[0]
    second_document = window._documents[1]
    second_document.session.mark_modified("edit")
    original_source_path = second_document.session.source_path
    original_fingerprint = second_document.session.source_fingerprint
    original_working_copy_bytes = second_document.session.document_path.read_bytes()

    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (
            str(first_document.session.workspace_directory / "forbidden.pdf"),
            "PDF files (*.pdf)",
        ),
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )

    assert window._save_current_document_as() is False
    assert save_service.calls == []
    assert second_document.session.is_modified is True
    assert second_document.session.source_path == original_source_path
    assert second_document.session.source_fingerprint == original_fingerprint
    assert second_document.session.document_path.read_bytes() == original_working_copy_bytes
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    assert window.close_document_at(1) is True
    assert window.close_document_at(0) is True


def test_main_window_save_as_allows_similar_non_managed_directory(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    save_service = FakeSaveService()
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=workspace_manager,
        save_service=save_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "allowed-save-as-source.pdf", 1)
    target_directory = tmp_path / "sessions-copy"
    target_directory.mkdir()
    target_path = target_directory / "allowed.pdf"
    window.open_document(document_path)

    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(target_path), "PDF files (*.pdf)"),
    )

    assert window._save_current_document_as() is True
    assert save_service.calls[0][0] == target_path.resolve()


def test_main_window_close_modified_document_save_failure_keeps_tab_open(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=FakeSaveService(should_fail=True),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "save-fail.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    document.session.mark_modified("edit")

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Save,
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )

    assert window.close_current_document() is False
    assert window._tabs.count() == 1
    assert document.session.is_modified is True
    assert document.session.workspace_directory.exists()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    assert window.close_current_document() is True


def test_main_window_close_modified_document_discard_cleans_workspace(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=FakeSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "discard.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    document.session.mark_modified("edit")
    workspace_directory = document.session.workspace_directory

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )

    assert window.close_current_document() is True
    assert not workspace_directory.exists()


def test_main_window_close_modified_document_cancel_keeps_workspace(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=FakeSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "cancel.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    document.session.mark_modified("edit")
    workspace_directory = document.session.workspace_directory

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Cancel,
    )

    assert window.close_current_document() is False
    assert workspace_directory.exists()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    assert window.close_current_document() is True


def test_main_window_close_event_cleans_all_session_workspaces(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=FakeSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    first = create_blank_pdf(tmp_path / "first-close.pdf", 1)
    second = create_blank_pdf(tmp_path / "second-close.pdf", 1)
    window.open_document(first)
    window.open_document(second)
    workspaces = [document.session.workspace_directory for document in window._documents]

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is True
    assert all(not workspace.exists() for workspace in workspaces)


def test_main_window_close_event_cleans_each_session_once(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    workspace_manager = TrackingWorkspaceManager(tmp_path / "sessions")
    window = MainWindow(
        settings,
        workspace_manager=workspace_manager,
        save_service=FakeSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    first = create_blank_pdf(tmp_path / "first-close-once.pdf", 1)
    second = create_blank_pdf(tmp_path / "second-close-once.pdf", 1)
    window.open_document(first)
    window.open_document(second)
    session_ids = [document.session.session_id for document in window._documents]

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is True
    assert workspace_manager.cleaned_sessions == session_ids[::-1]


def test_main_window_open_document_cleans_workspace_on_view_constructor_failure(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    window = MainWindow(settings, workspace_manager=workspace_manager)
    qtbot.addWidget(window)
    show_window(qtbot, window)
    document_path = create_blank_pdf(tmp_path / "constructor-failure.pdf", 1)

    def fail_constructor(*_args: object, **_kwargs: object) -> PdfView:
        raise RuntimeError("view constructor failed")

    monkeypatch.setattr("pdf_workbench.ui.main_window.PdfView", fail_constructor)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )

    window.open_document(document_path)

    assert window._tabs.count() == 0
    assert window._documents == []
    assert list(workspace_manager.sessions_root.iterdir()) == []


def test_main_window_open_document_cleans_workspace_on_set_zoom_failure(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    window = MainWindow(settings, workspace_manager=workspace_manager)
    qtbot.addWidget(window)
    show_window(qtbot, window)
    document_path = create_blank_pdf(tmp_path / "zoom-failure.pdf", 1)

    original_set_zoom = PdfView.set_zoom

    def fail_set_zoom(self: PdfView, zoom: float) -> None:
        raise RuntimeError(f"zoom failure {zoom}")

    monkeypatch.setattr(PdfView, "set_zoom", fail_set_zoom)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )

    window.open_document(document_path)

    monkeypatch.setattr(PdfView, "set_zoom", original_set_zoom)
    assert window._tabs.count() == 0
    assert window._documents == []
    assert list(workspace_manager.sessions_root.iterdir()) == []


def test_main_window_open_document_cleans_workspace_on_open_failure(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    window = MainWindow(settings, workspace_manager=workspace_manager)
    qtbot.addWidget(window)
    show_window(qtbot, window)
    document_path = create_blank_pdf(tmp_path / "open-failure.pdf", 1)

    def fail_open(self: PdfView, _path: Path) -> None:
        raise RuntimeError("open failure")

    monkeypatch.setattr(PdfView, "open_document", fail_open)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda *args, **kwargs: QMessageBox.StandardButton.Ok,
    )

    window.open_document(document_path)

    assert window._tabs.count() == 0
    assert window._documents == []
    assert list(workspace_manager.sessions_root.iterdir()) == []


def test_main_window_persists_recent_files_and_geometry(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)
    show_window(qtbot, window)

    document_path = tmp_path / "recent.pdf"
    document_path.touch()

    window.resize(900, 700)
    window.open_document(document_path)
    window._save_window_state()

    reopened = MainWindow(settings)
    qtbot.addWidget(reopened)

    assert reopened._recent_files[0] == document_path.resolve()
    assert settings.value(MainWindow._GEOMETRY_KEY) is not None


def test_main_window_avoids_duplicate_tabs_for_same_document(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)
    show_window(qtbot, window)

    document_path = tmp_path / "duplicate.pdf"
    document_path.touch()

    window.open_document(document_path)
    window.open_document(document_path)

    assert window._tabs.count() == 1
    assert window._tabs.currentIndex() == 0
    assert window._recent_files == [document_path.resolve()]


def test_main_window_restore_session_releases_duplicate_recovery_lock(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    render_service = PdfRenderService()
    window = MainWindow(
        settings,
        render_service=render_service,
        workspace_manager=workspace_manager,
        save_service=PdfSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "duplicate-recovery.pdf", 1)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._tabs.count() == 1)

    duplicate_session = workspace_manager.create_session(document_path)

    result = window.restore_session(duplicate_session)

    assert result is RestoreSessionResult.DUPLICATE
    assert window._tabs.count() == 1
    assert duplicate_session.session_id not in workspace_manager._active_leases
    assert duplicate_session.workspace_directory.exists()
    assert render_service.shutdown() is True


def test_main_window_drops_missing_recent_files_from_menu(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    missing_path = tmp_path / "missing.pdf"
    settings.setValue(
        MainWindow._RECENT_FILES_KEY,
        f'["{missing_path}"]',
    )

    window = MainWindow(settings)
    qtbot.addWidget(window)

    assert window._recent_files == []
    assert window.recent_files_menu.actions()[0].isEnabled() is False


def test_main_window_starts_on_empty_state_and_updates_toolbar(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    assert window._stack.currentWidget() is window._empty_state
    assert window._toolbar_widget.page_field.text() == "—"
    assert window._toolbar_widget.zoom_field.currentText() == "100%"

    document_path = tmp_path / "state.pdf"
    document_path.touch()
    window.open_document(document_path)

    assert window._stack.currentWidget() is window._tabs
    assert window._toolbar_widget.page_field.value() == 1
    assert window._toolbar_widget.zoom_field.currentText() == "100%"
    assert window._documents[0].session.zoom_factor == 1.0
    assert window._documents[0].view.zoom_factor == pytest.approx(1.5)


def test_main_window_search_toolbar_starts_hidden(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    assert window._search_toolbar is not None
    assert window._search_toolbar.isHidden()
    assert window.find_action.isEnabled() is False
    assert window._toolbar_widget.search_button.isEnabled() is False


def test_main_window_restores_page_after_async_document_load(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    render_service = PdfRenderService()
    window = MainWindow(
        settings,
        render_service=render_service,
        workspace_manager=workspace_manager,
        save_service=PdfSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    persist_calls: list[int] = []
    monkeypatch.setattr(
        window,
        "_schedule_recovery_metadata_persist",
        lambda restored_session: persist_calls.append(restored_session.current_page_index),
    )

    document_path = create_blank_pdf(tmp_path / "restore-page.pdf", 3)
    session = workspace_manager.create_session(document_path)
    session.set_navigation_state(page_index=2, zoom_factor=1.25)

    result = window.restore_session(session)

    assert result is RestoreSessionResult.ATTACHED
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)
    qtbot.waitUntil(lambda: window._documents[0].view.page_index == 2)
    assert window._documents[0].session.current_page_index == 2
    assert window._documents[0].session.zoom_factor == pytest.approx(1.25)
    assert window._documents[0].view._page_organizer.current_page_index == 2
    assert window._toolbar_widget.page_field.value() == 3
    assert "3 / 3" in window._status_summary.text()
    assert persist_calls == [2]


def test_main_window_clamps_restored_page_after_async_document_load(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    render_service = PdfRenderService()
    window = MainWindow(
        settings,
        render_service=render_service,
        workspace_manager=workspace_manager,
        save_service=PdfSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    persist_calls: list[int] = []
    monkeypatch.setattr(
        window,
        "_schedule_recovery_metadata_persist",
        lambda restored_session: persist_calls.append(restored_session.current_page_index),
    )

    document_path = create_blank_pdf(tmp_path / "restore-clamp.pdf", 3)
    session = workspace_manager.create_session(document_path)
    session.set_navigation_state(page_index=8, zoom_factor=1.0)

    result = window.restore_session(session)

    assert result is RestoreSessionResult.ATTACHED
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)
    qtbot.waitUntil(lambda: window._documents[0].view.page_index == 2)
    assert window._documents[0].session.current_page_index == 2
    assert persist_calls == [2]


def test_main_window_restores_page_only_once_for_synchronous_load(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    window = MainWindow(settings, workspace_manager=workspace_manager)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "restore-once.pdf", 3)
    session = workspace_manager.create_session(document_path)
    session.set_navigation_state(page_index=2, zoom_factor=1.0)

    page_apply_calls: list[int] = []
    metadata_persist_calls: list[int] = []

    def fake_open_document(self: PdfView, path: Path) -> None:
        self._path = path
        self._metadata = DocumentMetadata(
            revision=DocumentRevision.from_path(path),
            pages=(
                PageMetadata.from_size(144.0, 144.0),
                PageMetadata.from_size(144.0, 144.0),
                PageMetadata.from_size(144.0, 144.0),
            ),
        )
        self.document_loaded.emit()

    def tracking_set_page(self: PdfView, page_index: int) -> None:
        page_apply_calls.append(page_index)
        self._current_page_index = page_index
        self.current_page_changed.emit(page_index)

    monkeypatch.setattr(PdfView, "open_document", fake_open_document)
    monkeypatch.setattr(PdfView, "set_page", tracking_set_page)
    monkeypatch.setattr(
        window,
        "_schedule_recovery_metadata_persist",
        lambda _session: metadata_persist_calls.append(_session.current_page_index),
    )

    result = window.restore_session(session)

    assert result is RestoreSessionResult.ATTACHED
    assert page_apply_calls == [2]
    assert metadata_persist_calls == [2]


def test_main_window_restoring_page_zero_does_not_schedule_duplicate_persist(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    render_service = PdfRenderService()
    window = MainWindow(
        settings,
        render_service=render_service,
        workspace_manager=workspace_manager,
        save_service=PdfSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    persist_calls: list[int] = []
    monkeypatch.setattr(
        window,
        "_schedule_recovery_metadata_persist",
        lambda restored_session: persist_calls.append(restored_session.current_page_index),
    )

    document_path = create_blank_pdf(tmp_path / "restore-zero.pdf", 2)
    session = workspace_manager.create_session(document_path)
    session.set_navigation_state(page_index=0, zoom_factor=1.0)

    assert window.restore_session(session) is RestoreSessionResult.ATTACHED
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 2)
    QTest.qWait(20)

    assert window._documents[0].session.current_page_index == 0
    assert persist_calls == []


def test_main_window_view_navigation_updates_session_and_toolbar(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    render_service = PdfRenderService()
    window = MainWindow(
        settings,
        render_service=render_service,
        workspace_manager=workspace_manager,
        save_service=PdfSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "session-sync.pdf", 3)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)

    document = window._documents[0]
    document.view.set_page(2)
    qtbot.waitUntil(lambda: document.session.current_page_index == 2)

    assert window._toolbar_widget.page_field.value() == 3
    assert "3 / 3" in window._status_summary.text()
    assert document.view._page_organizer.current_page_index == 2
    assert document.command_history.can_undo is False

    window.close()
    qtbot.waitUntil(lambda: not render_service._thread.isRunning())


def test_main_window_page_organizer_selection_does_not_dirty_session_or_history(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    render_service = PdfRenderService()
    window = MainWindow(
        settings,
        render_service=render_service,
        workspace_manager=workspace_manager,
        save_service=PdfSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "organizer-selection.pdf", 4)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 4)
    document = window._documents[0]

    document.view._page_organizer.set_selected_page_indexes((0, 2), current_index=0)

    assert document.view.selected_page_indexes == (0, 2)
    assert document.session.is_modified is False
    assert document.command_history.can_undo is False
    assert window.save_action.isEnabled() is False

    window.close()
    qtbot.waitUntil(lambda: not render_service._thread.isRunning())


def test_main_window_page_organizer_navigation_updates_session_once(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    workspace_manager = create_workspace_manager(tmp_path)
    render_service = PdfRenderService()
    window = MainWindow(
        settings,
        render_service=render_service,
        workspace_manager=workspace_manager,
        save_service=PdfSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "organizer-nav.pdf", 4)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 4)
    document = window._documents[0]
    persist_calls: list[int] = []
    monkeypatch.setattr(
        window,
        "_schedule_recovery_metadata_persist",
        lambda session: persist_calls.append(session.current_page_index),
    )

    index = document.view._page_organizer.list_view.model().index(2, 0)
    rect = document.view._page_organizer.list_view.visualRect(index)
    QTest.mouseClick(
        document.view._page_organizer.list_view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        rect.center(),
    )
    qtbot.waitUntil(lambda: document.session.current_page_index == 2)

    assert persist_calls == [2]
    assert window._toolbar_widget.page_field.value() == 3

    window.close()
    qtbot.waitUntil(lambda: not render_service._thread.isRunning())


def working_copy_effective_rotations(path: Path) -> tuple[int, ...]:
    with pikepdf.open(str(path)) as pdf:
        inherited = int(pdf.Root.Pages.get("/Rotate", 0))
        rotations: list[int] = []
        for page in pdf.pages:
            direct = page.obj.get("/Rotate", None)
            rotations.append((int(direct) if direct is not None else inherited) % 360)
        return tuple(rotations)


def working_copy_page_count(path: Path) -> int:
    with pikepdf.open(str(path)) as pdf:
        return len(pdf.pages)


def test_main_window_rotate_selected_pages_persists_into_working_copy_and_restores_selection(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_blank_pdf(tmp_path / "rotate-selected.pdf", 3)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)
    document = window._documents[0]
    source_before = file_sha256(document_path)
    working_copy_before = file_sha256(document.session.document_path)
    document.view._page_organizer.set_selected_page_indexes((0, 2), current_index=2)
    document.view.set_page(2)
    qtbot.waitUntil(lambda: document.session.current_page_index == 2)

    window._rotate_page()

    qtbot.waitUntil(lambda: document.command_history.can_undo)
    qtbot.waitUntil(lambda: document.view.page_index == 2)
    assert document.view.selected_page_indexes == (0, 2)
    assert working_copy_effective_rotations(document.session.document_path) == (90, 0, 90)
    assert file_sha256(document_path) == source_before
    assert file_sha256(document.session.document_path) != working_copy_before
    assert document.session.is_modified is True
    assert document.session.operation_history[-1] == "2ページを時計回りに回転"

    render_service = window._render_service
    window.close()
    qtbot.waitUntil(lambda: not render_service._thread.isRunning())


def test_main_window_duplicate_toolbar_button_persists_into_working_copy_and_restores_duplicates(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_simple_text_pdf(
        tmp_path / "duplicate-selected.pdf",
        ["A", "B", "C", "D", "E"],
    )
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 5)
    document = window._documents[0]
    source_before = file_sha256(document_path)
    working_copy_before = file_sha256(document.session.document_path)

    assert window._toolbar_widget.duplicate_button.objectName() == "duplicatePagesButton"
    assert window._toolbar_widget.duplicate_button.accessibleName() == "Duplicate selected pages"
    assert window._toolbar_widget.duplicate_button.toolTip() == "選択したページを複製"
    assert window.duplicate_pages_action.text() == "選択したページを複製"

    document.view._page_organizer.set_selected_page_indexes((1, 3), current_index=1)
    document.view.set_page(1)
    qtbot.waitUntil(lambda: document.session.current_page_index == 1)
    qtbot.waitUntil(window._toolbar_widget.duplicate_button.isEnabled)

    QTest.mouseClick(window._toolbar_widget.duplicate_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: document.command_history.can_undo)
    qtbot.waitUntil(lambda: document.view.page_count == 7)
    qtbot.waitUntil(lambda: document.view.page_index == 2)

    assert document.view.selected_page_indexes == (2, 5)
    assert working_copy_page_count(document.session.document_path) == 7
    assert extract_pdfium_text(document.session.document_path) == "A B B C D D E"
    assert file_sha256(document_path) == source_before
    assert file_sha256(document.session.document_path) != working_copy_before
    assert document.session.is_modified is True
    assert window.save_action.isEnabled() is True
    assert window.undo_action.isEnabled() is True
    assert window.redo_action.isEnabled() is False
    assert window._tabs.tabText(0).endswith("*")
    assert document.session.operation_history[-1] == "2ページを複製"

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_duplicate_menu_action_save_marks_clean_and_undo_restores_dirty(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_simple_text_pdf(tmp_path / "duplicate-save.pdf", ["A", "B"])
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 2)
    document = window._documents[0]

    document.view._page_organizer.set_selected_page_indexes((0,), current_index=0)
    qtbot.waitUntil(lambda: window.duplicate_pages_action.isEnabled())

    window.duplicate_pages_action.trigger()

    qtbot.waitUntil(lambda: document.command_history.can_undo)
    qtbot.waitUntil(lambda: document.view.page_count == 3)
    assert document.view.selected_page_indexes == (1,)
    assert extract_pdfium_text(document.session.document_path) == "A A B"
    assert extract_pdfium_text(document_path) == "A B"
    assert window._save_current_document() is True
    assert document.session.is_modified is False
    assert document.command_history.is_dirty is False
    assert window.save_action.isEnabled() is False
    assert extract_pdfium_text(document_path) == "A A B"

    assert window._undo_current_command() is True
    qtbot.waitUntil(lambda: document.view.page_count == 2)
    assert document.session.is_modified is True
    assert document.command_history.is_dirty is True
    assert extract_pdfium_text(document.session.document_path) == "A B"
    assert extract_pdfium_text(document_path) == "A A B"
    assert document.session.operation_history[-1] == "Undo: 1ページを複製"

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_duplicate_page_noops_when_selection_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_blank_pdf(tmp_path / "duplicate-empty-selection.pdf", 3)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((), current_index=1)
    calls: list[str] = []
    monkeypatch.setattr(
        window,
        "execute_document_command",
        lambda command: calls.append(command.description) or True,
    )

    window._duplicate_selected_pages()

    assert calls == []
    assert document.command_history.can_undo is False
    assert working_copy_page_count(document.session.document_path) == 3

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_duplicate_controls_disable_during_mutation_and_save(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_blank_pdf(tmp_path / "duplicate-guard-controls.pdf", 1)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 1)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((0,), current_index=0)
    window._sync_toolbar(document)
    window._update_actions()
    assert window._toolbar_widget.duplicate_button.isEnabled() is True
    assert window.duplicate_pages_action.isEnabled() is True

    document.mutation_in_progress = True
    window._sync_toolbar(document)
    window._update_actions()
    assert window._toolbar_widget.duplicate_button.isEnabled() is False
    assert window.duplicate_pages_action.isEnabled() is False

    document.mutation_in_progress = False
    document.session.is_saving = True
    window._sync_toolbar(document)
    window._update_actions()
    assert window._toolbar_widget.duplicate_button.isEnabled() is False
    assert window.duplicate_pages_action.isEnabled() is False

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_delete_toolbar_button_persists_into_working_copy_and_maps_current_page(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_simple_text_pdf(
        tmp_path / "delete-selected.pdf",
        ["A", "B", "C", "D", "E"],
    )
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 5)
    document = window._documents[0]
    source_before = file_sha256(document_path)
    working_copy_before = file_sha256(document.session.document_path)

    assert window._toolbar_widget.delete_button.objectName() == "deletePagesButton"
    assert window._toolbar_widget.delete_button.accessibleName() == "Delete selected pages"
    assert window._toolbar_widget.delete_button.toolTip() == "選択したページを削除"
    assert window.delete_pages_action.text() == "選択したページを削除"

    document.view._page_organizer.set_selected_page_indexes((1, 3), current_index=1)
    document.view.set_page(1)
    qtbot.waitUntil(lambda: document.session.current_page_index == 1)
    qtbot.waitUntil(window._toolbar_widget.delete_button.isEnabled)

    QTest.mouseClick(window._toolbar_widget.delete_button, Qt.MouseButton.LeftButton)

    qtbot.waitUntil(lambda: document.command_history.can_undo)
    qtbot.waitUntil(lambda: document.view.page_count == 3)
    qtbot.waitUntil(lambda: document.view.page_index == 1)

    assert document.view.selected_page_indexes == (1,)
    assert working_copy_page_count(document.session.document_path) == 3
    assert extract_pdfium_text(document.session.document_path) == "A C E"
    assert file_sha256(document_path) == source_before
    assert file_sha256(document.session.document_path) != working_copy_before
    assert document.session.is_modified is True
    assert window.save_action.isEnabled() is True
    assert window.undo_action.isEnabled() is True
    assert window.redo_action.isEnabled() is False
    assert document.session.operation_history[-1] == "2ページを削除"

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_reorder_request_persists_into_working_copy_and_tracks_identity(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_simple_text_pdf(
        tmp_path / "reorder-selected.pdf",
        ["A", "B", "C", "D", "E", "F"],
    )
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 6)
    document = window._documents[0]
    source_before = file_sha256(document_path)
    working_copy_before = file_sha256(document.session.document_path)
    document.view._page_organizer.set_selected_page_indexes((1, 3), current_index=3)
    document.view.set_page(3)
    qtbot.waitUntil(lambda: document.session.current_page_index == 3)

    document.view._page_organizer.pages_reorder_requested.emit((1, 3), 6)

    qtbot.waitUntil(lambda: document.command_history.can_undo)
    qtbot.waitUntil(lambda: document.view.page_index == 5)

    assert document.view.selected_page_indexes == (4, 5)
    assert extract_pdfium_text(document.session.document_path) == "A C E F B D"
    assert file_sha256(document_path) == source_before
    assert file_sha256(document.session.document_path) != working_copy_before
    assert document.session.is_modified is True
    assert window.undo_action.text() == "元に戻す: 2ページを移動"
    assert window.redo_action.isEnabled() is False
    assert document.session.operation_history[-1] == "2ページを移動"

    assert window._undo_current_command() is True
    qtbot.waitUntil(lambda: document.view.page_index == 3)
    assert document.view.selected_page_indexes == (1, 3)
    assert extract_pdfium_text(document.session.document_path) == "A B C D E F"
    assert document.session.operation_history[-1] == "Undo: 2ページを移動"

    assert window._redo_current_command() is True
    qtbot.waitUntil(lambda: document.view.page_index == 5)
    assert document.view.selected_page_indexes == (4, 5)
    assert extract_pdfium_text(document.session.document_path) == "A C E F B D"
    assert document.session.operation_history[-1] == "Redo: 2ページを移動"

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_reorder_noop_does_not_create_history_entry(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_blank_pdf(tmp_path / "reorder-noop.pdf", 5)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 5)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((1, 2), current_index=2)
    calls: list[str] = []
    reported: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window,
        "execute_document_command",
        lambda command: calls.append(command.description) or True,
    )
    monkeypatch.setattr(
        window,
        "_report_error",
        lambda title, message: reported.append((title, message)),
    )

    document.view._page_organizer.pages_reorder_requested.emit((1, 2), 3)

    assert calls == []
    assert reported == []
    assert document.command_history.can_undo is False
    assert document.session.operation_history == []

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_reorder_failure_preserves_history_dirty_and_viewer_state(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_qt_text_pdf(
        tmp_path / "reorder-failure.pdf",
        ["A", "B", "C", "D"],
    )
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 4)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((1, 3), current_index=3)
    document.view.set_page(3)
    qtbot.waitUntil(lambda: document.session.current_page_index == 3)
    working_copy_before = file_sha256(document.session.document_path)
    reported: list[tuple[str, str]] = []

    def fail_reorder(*_args: object, **_kwargs: object) -> object:
        raise PdfPageMutationError("reorder failed")

    monkeypatch.setattr(window._page_mutation_service, "reorder_pages", fail_reorder)
    monkeypatch.setattr(
        window,
        "_report_error",
        lambda title, message: reported.append((title, message)),
    )

    document.view._page_organizer.pages_reorder_requested.emit((1, 3), 4)

    qtbot.waitUntil(lambda: bool(reported))
    assert reported[-1] == ("編集に失敗しました", "reorder failed")
    assert document.command_history.can_undo is False
    assert document.command_history.is_dirty is False
    assert document.session.is_modified is False
    assert document.view.selected_page_indexes == (1, 3)
    assert document.view.page_index == 3
    assert file_sha256(document.session.document_path) == working_copy_before
    assert document.mutation_in_progress is False
    assert document.session.operation_history == []

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_reorder_requests_are_blocked_while_saving_or_mutating(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_blank_pdf(tmp_path / "reorder-guards.pdf", 4)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 4)
    document = window._documents[0]
    calls: list[str] = []
    monkeypatch.setattr(
        window,
        "execute_document_command",
        lambda command: calls.append(command.description) or True,
    )

    document.session.is_saving = True
    document.view._page_organizer.pages_reorder_requested.emit((1,), 4)
    document.session.is_saving = False
    document.mutation_in_progress = True
    document.view._page_organizer.pages_reorder_requested.emit((1,), 4)

    assert calls == []
    assert document.command_history.can_undo is False

    document.mutation_in_progress = False
    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_ignores_stale_reorder_request_when_selection_no_longer_matches(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_blank_pdf(tmp_path / "reorder-stale.pdf", 5)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 5)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((1, 3), current_index=3)
    calls: list[str] = []
    reported: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window,
        "execute_document_command",
        lambda command: calls.append(command.description) or True,
    )
    monkeypatch.setattr(
        window,
        "_report_error",
        lambda title, message: reported.append((title, message)),
    )

    document.view._page_organizer.pages_reorder_requested.emit((1, 2), 5)

    assert calls == []
    assert reported == []
    assert document.command_history.can_undo is False
    assert document.command_history.is_dirty is False
    assert document.session.is_modified is False

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_delete_menu_action_save_marks_clean_and_undo_restores_dirty(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_simple_text_pdf(tmp_path / "delete-save.pdf", ["A", "B", "C"])
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)
    document = window._documents[0]

    document.view._page_organizer.set_selected_page_indexes((1,), current_index=1)
    document.view.set_page(1)
    qtbot.waitUntil(lambda: document.session.current_page_index == 1)
    qtbot.waitUntil(lambda: window.delete_pages_action.isEnabled())

    window.delete_pages_action.trigger()

    qtbot.waitUntil(lambda: document.command_history.can_undo)
    qtbot.waitUntil(lambda: document.view.page_count == 2)
    assert document.view.selected_page_indexes == (1,)
    assert extract_pdfium_text(document.session.document_path) == "A C"
    assert extract_pdfium_text(document_path) == "A B C"
    assert window._save_current_document() is True
    assert document.session.is_modified is False
    assert document.command_history.is_dirty is False
    assert window.save_action.isEnabled() is False
    assert extract_pdfium_text(document_path) == "A C"

    assert window._undo_current_command() is True
    qtbot.waitUntil(lambda: document.view.page_count == 3)
    assert document.session.is_modified is True
    assert document.command_history.is_dirty is True
    assert document.view.selected_page_indexes == (1,)
    assert document.view.page_index == 1
    assert extract_pdfium_text(document.session.document_path) == "A B C"
    assert extract_pdfium_text(document_path) == "A C"
    assert document.session.operation_history[-1] == "Undo: 1ページを削除"

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_delete_controls_disable_for_all_page_selection_and_noop_when_empty(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_blank_pdf(tmp_path / "delete-guards.pdf", 2)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 2)
    document = window._documents[0]
    calls: list[str] = []
    monkeypatch.setattr(
        window,
        "execute_document_command",
        lambda command: calls.append(command.description) or True,
    )

    document.view._page_organizer.set_selected_page_indexes((), current_index=0)
    window._sync_toolbar(document)
    window._update_actions()
    window._delete_selected_pages()
    assert calls == []

    document.view._page_organizer.set_selected_page_indexes((0, 1), current_index=0)
    window._sync_toolbar(document)
    window._update_actions()
    assert window._toolbar_widget.delete_button.isEnabled() is False
    assert window.delete_pages_action.isEnabled() is False
    window._delete_selected_pages()
    assert calls == []

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_delete_failure_preserves_history_dirty_and_viewer_state(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_qt_text_pdf(tmp_path / "delete-failure.pdf", ["A", "B", "C"])
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((1,), current_index=1)
    document.view.set_page(1)
    qtbot.waitUntil(lambda: document.session.current_page_index == 1)
    working_copy_before = file_sha256(document.session.document_path)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window,
        "_report_error",
        lambda title, message: errors.append((title, message)),
    )

    def fail_delete(*args: object, **kwargs: object) -> object:
        raise RuntimeError("deletion failed")

    monkeypatch.setattr(window._page_mutation_service, "delete_pages", fail_delete)

    window._delete_selected_pages()

    qtbot.waitUntil(lambda: bool(errors))
    assert document.command_history.can_undo is False
    assert document.session.is_modified is False
    assert document.view.page_index == 1
    assert document.view.selected_page_indexes == (1,)
    assert working_copy_page_count(document.session.document_path) == 3
    assert file_sha256(document.session.document_path) == working_copy_before
    assert errors[-1][1] == "deletion failed"

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_close_document_cleans_delete_undo_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_simple_text_pdf(tmp_path / "delete-close.pdf", ["A", "B", "C"])
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((1,), current_index=1)

    window._delete_selected_pages()

    qtbot.waitUntil(lambda: document.command_history.can_undo)
    undo_command = document.command_history.undo_command
    assert isinstance(undo_command, DeletePagesCommand)
    receipt = undo_command._receipt
    assert receipt is not None
    snapshot_path = receipt.undo_snapshot_path
    assert snapshot_path.exists()

    assert window.close_document_at(0) is True
    assert not snapshot_path.exists()
    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_close_document_falls_back_to_workspace_cleanup_after_dispose_failure(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_simple_text_pdf(tmp_path / "delete-close-fallback.pdf", ["A", "B", "C"])
    source_sha_before = file_sha256(document_path)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((1,), current_index=1)

    window._delete_selected_pages()

    qtbot.waitUntil(lambda: document.command_history.can_undo)
    undo_command = document.command_history.undo_command
    assert isinstance(undo_command, DeletePagesCommand)
    receipt = undo_command._receipt
    assert receipt is not None
    snapshot_path = receipt.undo_snapshot_path
    working_copy_path = document.session.document_path
    assert snapshot_path.exists()
    assert working_copy_path.exists()

    def fail_discard(_working_copy_path: Path, _receipt: object) -> None:
        raise PdfPageMutationError("cleanup failed")

    monkeypatch.setattr(
        undo_command._mutation_service,
        "discard_page_deletion_receipt",
        fail_discard,
    )

    assert window.close_document_at(0) is True
    assert not snapshot_path.exists()
    assert not working_copy_path.exists()
    assert document_path.exists()
    assert file_sha256(document_path) == source_sha_before
    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_rotate_page_noops_when_selection_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_blank_pdf(tmp_path / "rotate-empty-selection.pdf", 3)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((), current_index=1)
    calls: list[str] = []
    monkeypatch.setattr(
        window,
        "execute_document_command",
        lambda command: calls.append(command.description) or True,
    )

    window._rotate_page()

    assert calls == []
    assert document.command_history.can_undo is False
    assert working_copy_effective_rotations(document.session.document_path) == (0, 0, 0)

    render_service = window._render_service
    window.close()
    qtbot.waitUntil(lambda: not render_service._thread.isRunning())


def test_main_window_mutation_flag_blocks_same_document_actions_until_reload_completes(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    document_path = create_blank_pdf(tmp_path / "mutation-blocking.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    revision = DocumentRevision.from_path(document.session.document_path)
    mutation_result = WorkingCopyMutationResult(
        old_revision=revision,
        new_revision=revision,
        page_count=1,
        affected_pages=frozenset({0}),
    )
    command = MutatingStubDocumentCommand("Rotate", mutation_result)
    lifecycle_states: list[tuple[bool, bool, bool, bool, bool]] = []
    original_snapshot = document.view.suspend_for_working_copy_mutation()
    monkeypatch.setattr(
        document.view,
        "suspend_for_working_copy_mutation",
        lambda: original_snapshot,
    )
    monkeypatch.setattr(
        document.view,
        "release_renderer_backend",
        lambda timeout_ms=3000: DocumentReleaseResult(
            request_id="fake",
            document_id="doc",
            requested_generation=1,
            closed_generation=1,
            success=True,
        ),
    )

    def delayed_reload(snapshot: object) -> bool:
        assert snapshot == original_snapshot

        def emit_completion() -> None:
            lifecycle_states.append(
                (
                    document.mutation_in_progress,
                    window.undo_action.isEnabled(),
                    window.redo_action.isEnabled(),
                    window.save_action.isEnabled(),
                    window.close_action.isEnabled(),
                )
            )
            document.view.mutation_reload_completed.emit(True)

        QTimer.singleShot(0, emit_completion)
        return True

    monkeypatch.setattr(document.view, "reload_after_working_copy_mutation", delayed_reload)

    assert window.execute_document_command(command) is True

    assert lifecycle_states == [(True, False, False, False, False)]
    assert document.mutation_in_progress is False

    assert window._render_service.shutdown() is True
    window.deleteLater()


def test_main_window_mutation_flag_blocks_close_save_undo_redo(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    document_path = create_blank_pdf(tmp_path / "mutation-guards.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    assert window.execute_document_command(StubDocumentCommand("Rotate")) is True
    document.mutation_in_progress = True
    window._update_actions()

    assert window.close_current_document() is False
    assert window._save_current_document() is False
    assert window._undo_current_command() is False
    assert window._redo_current_command() is False
    assert window.save_action.isEnabled() is False
    assert window.save_as_action.isEnabled() is False
    assert window.close_action.isEnabled() is False

    document.mutation_in_progress = False
    assert window._render_service.shutdown() is True
    window.deleteLater()


@pytest.mark.parametrize("current_page_override", [0, 2])
def test_transform_mutation_snapshot_applies_valid_current_page_override(
    qtbot: QtBot,
    tmp_path: Path,
    current_page_override: int,
) -> None:
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    document_path = create_blank_pdf(tmp_path / "transform-valid.pdf", 3)
    revision = DocumentRevision.from_path(document_path)
    snapshot = PdfViewMutationSnapshot(
        current_page_index=1,
        selected_page_indexes=(1,),
        logical_zoom=1.5,
        search_query="Alpha",
    )
    change = CommandChange(
        affected_pages=frozenset(),
        mutation_result=WorkingCopyMutationResult(
            old_revision=revision,
            new_revision=revision,
            page_count=3,
            affected_pages=frozenset(),
            page_index_transition=PageIndexTransition(
                old_page_count=4,
                new_page_count=3,
                cache_old_to_new=(0, None, 1, 2),
                current_page_old_to_new=(0, 0, 1, 2),
            ),
        ),
        current_page_index_after=current_page_override,
    )

    transformed = window._transform_mutation_snapshot(snapshot, change)

    assert transformed.current_page_index == current_page_override
    window.close()


@pytest.mark.parametrize("current_page_override", [-1, 3])
def test_transform_mutation_snapshot_rejects_invalid_current_page_override(
    qtbot: QtBot,
    tmp_path: Path,
    current_page_override: int,
) -> None:
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    document_path = create_blank_pdf(tmp_path / "transform-invalid.pdf", 3)
    revision = DocumentRevision.from_path(document_path)
    snapshot = PdfViewMutationSnapshot(
        current_page_index=1,
        selected_page_indexes=(1,),
        logical_zoom=1.5,
        search_query="Alpha",
    )
    change = CommandChange(
        affected_pages=frozenset(),
        mutation_result=WorkingCopyMutationResult(
            old_revision=revision,
            new_revision=revision,
            page_count=3,
            affected_pages=frozenset(),
            page_index_transition=PageIndexTransition(
                old_page_count=4,
                new_page_count=3,
                cache_old_to_new=(0, None, 1, 2),
                current_page_old_to_new=(0, 0, 1, 2),
            ),
        ),
        current_page_index_after=current_page_override,
    )

    with pytest.raises(ValueError, match="outside the new page range"):
        window._transform_mutation_snapshot(snapshot, change)

    window.close()


def test_main_window_save_as_does_not_open_dialog_during_mutation(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    document_path = create_blank_pdf(tmp_path / "mutation-save-as.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    document.mutation_in_progress = True
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: calls.append(args) or ("", ""),
    )

    assert window._save_current_document_as() is False
    assert calls == []

    assert window._render_service.shutdown() is True
    window.deleteLater()


def test_main_window_reload_after_mutation_times_out_and_ignores_late_completion(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "mutation_timeout_probe.py"
    document_path = tmp_path / "mutation-timeout.pdf"
    script_path.write_text(
        """
from pathlib import Path
import json

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from pypdf import PdfWriter

from pdf_workbench.services.session_workspace import SessionWorkspaceManager
from pdf_workbench.ui.main_window import MainWindow
from pdf_workbench.ui.pdf_view import PdfView
from pdf_workbench.services.pdf_renderer import DocumentMetadata, DocumentRevision
from pdf_workbench.services.page_coordinates import PageMetadata

app = QApplication([])

def fake_open_document(self: PdfView, path: Path) -> None:
    self._path = path
    self._current_page_index = 0
    self._metadata = DocumentMetadata(
        revision=DocumentRevision.from_path(path),
        pages=(PageMetadata.from_size(144.0, 144.0),),
    )
    self.state_changed.emit()

PdfView.open_document = fake_open_document

writer = PdfWriter()
writer.add_blank_page(width=144, height=144)
document_path = Path(r\"\"\""""
        + str(document_path)
        + """\"\"\")
with document_path.open("wb") as stream:
    writer.write(stream)

settings = QSettings(str(Path(r\"\"\""""
        + str(tmp_path / "settings.ini")
        + """\"\"\")), QSettings.Format.IniFormat)
window = MainWindow(
    settings,
    workspace_manager=SessionWorkspaceManager(Path(r\"\"\""""
        + str(tmp_path / "sessions")
        + """\"\"\")),
)
window.show()
window.open_document(document_path)
document = window._documents[0]
snapshot = document.view.suspend_for_working_copy_mutation()
document.mutation_in_progress = True
window._update_actions()
document.view.reload_after_working_copy_mutation = lambda _snapshot: True

result = window._reload_after_mutation(document, snapshot, timeout_ms=10)
payload = {
    "result": result,
    "mutation_in_progress": document.mutation_in_progress,
    "close_enabled": window.close_action.isEnabled(),
    "operation_id": document.mutation_operation_id,
}
print(json.dumps(payload))
window.close()
app.quit()
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=Path(__file__).resolve().parent.parent,
        env={"QT_QPA_PLATFORM": "offscreen", **os.environ},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip())
    assert payload["result"] is False
    assert payload["mutation_in_progress"] is False
    assert payload["close_enabled"] is True
    assert payload["operation_id"] == 1


def test_main_window_rotate_command_undo_and_redo_restore_working_copy(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_blank_pdf(tmp_path / "rotate-undo-redo.pdf", 2)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 2)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((1,), current_index=1)

    assert window.execute_document_command(
        RotatePagesCommand(
            document.session.document_path,
            (1,),
            PdfPageMutationService(),
        )
    )
    qtbot.waitUntil(
        lambda: working_copy_effective_rotations(document.session.document_path) == (0, 90)
    )

    assert window._undo_current_command() is True
    qtbot.waitUntil(
        lambda: working_copy_effective_rotations(document.session.document_path) == (0, 0)
    )

    assert window._redo_current_command() is True
    qtbot.waitUntil(
        lambda: working_copy_effective_rotations(document.session.document_path) == (0, 90)
    )

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_recovers_view_after_failed_persistent_rotation(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_blank_pdf(tmp_path / "rotate-failure.pdf", 2)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 2)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((1,), current_index=1)
    document.view.set_page(1)
    qtbot.waitUntil(lambda: document.session.current_page_index == 1)
    reload_calls: list[tuple[int, tuple[int, ...]]] = []
    original_reload = document.view.reload_document

    def tracking_reload(**kwargs: object) -> bool:
        restore_page_index = kwargs.get("restore_page_index", document.view.page_index)
        restore_selected = kwargs.get(
            "restore_selected_page_indexes",
            document.view.selected_page_indexes,
        )
        reload_calls.append((int(restore_page_index), tuple(restore_selected)))
        return original_reload(**kwargs)

    monkeypatch.setattr(document.view, "reload_document", tracking_reload)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window,
        "_report_error",
        lambda title, message: errors.append((title, message)),
    )
    original_apply = window._page_mutation_service.apply_rotation_states

    def fail_apply(*args: object, **kwargs: object) -> object:
        raise RuntimeError("mutation failed")

    monkeypatch.setattr(window._page_mutation_service, "apply_rotation_states", fail_apply)

    window._rotate_page()

    qtbot.waitUntil(lambda: len(reload_calls) >= 1)
    qtbot.waitUntil(lambda: document.view.page_index == 1)
    assert document.command_history.can_undo is False
    assert document.session.is_modified is False
    assert document.view.page_index == 1
    assert document.view.selected_page_indexes == (1,)
    assert reload_calls[-1] == (1, (1,))
    assert errors and errors[-1][1] == "mutation failed"
    monkeypatch.setattr(window._page_mutation_service, "apply_rotation_states", original_apply)

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_duplicate_failure_preserves_history_dirty_and_viewer_state(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    window = create_real_main_window(qtbot, tmp_path)
    document_path = create_qt_text_pdf(tmp_path / "duplicate-failure.pdf", ["A", "B", "C"])
    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 3)
    document = window._documents[0]
    document.view._page_organizer.set_selected_page_indexes((1,), current_index=1)
    document.view.set_page(1)
    qtbot.waitUntil(lambda: document.session.current_page_index == 1)
    working_copy_before = file_sha256(document.session.document_path)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window,
        "_report_error",
        lambda title, message: errors.append((title, message)),
    )

    def fail_duplicate(*args: object, **kwargs: object) -> object:
        raise RuntimeError("duplication failed")

    monkeypatch.setattr(window._page_mutation_service, "duplicate_pages", fail_duplicate)

    window._duplicate_selected_pages()

    qtbot.waitUntil(lambda: bool(errors))
    assert document.command_history.can_undo is False
    assert document.session.is_modified is False
    assert document.view.page_index == 1
    assert document.view.selected_page_indexes == (1,)
    assert working_copy_page_count(document.session.document_path) == 3
    assert file_sha256(document.session.document_path) == working_copy_before
    assert errors[-1][1] == "duplication failed"

    window.close()
    qtbot.waitUntil(lambda: not window._render_service._thread.isRunning())


def test_main_window_toolbar_search_button_opens_search_ui(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "toolbar-search.pdf", 1)
    window.open_document(document_path)
    qtbot.waitUntil(lambda: bool(window._documents[0].view._canvas.pages))

    assert window._toolbar_widget.search_button.isEnabled() is True
    QTest.mouseClick(window._toolbar_widget.search_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window._search_ui_is_ready())
    assert_search_ui_ready(window)
    document = window._documents[0]
    assert window._search_surface is not None
    surface_bottom = window._search_surface.mapTo(
        window,
        window._search_surface.rect().bottomLeft(),
    ).y()
    page_top = (
        document.view._canvas.pages[0]
        .mapTo(
            window,
            document.view._canvas.pages[0].rect().topLeft(),
        )
        .y()
    )
    assert page_top >= surface_bottom + 8


def test_main_window_responsive_toolbar_keeps_controls_visible_at_800_width(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    window.resize(800, 600)
    show_window(qtbot, window)

    document_path = tmp_path / "responsive.pdf"
    document_path.touch()
    window.open_document(document_path)

    for widget in (
        window._toolbar_widget.open_button,
        window._toolbar_widget.search_button,
        window._toolbar_widget.previous_button,
        window._toolbar_widget.page_field,
        window._toolbar_widget.next_button,
        window._toolbar_widget.zoom_out_button,
        window._toolbar_widget.zoom_field,
        window._toolbar_widget.zoom_in_button,
        window._toolbar_widget.rotate_button,
        window._toolbar_widget.duplicate_button,
    ):
        assert widget.geometry().width() > 0
        assert widget.geometry().height() > 0
        assert (
            widget.mapTo(window._toolbar_widget, widget.rect().topRight()).x()
            <= window._toolbar_widget.width()
        )
    assert window._main_toolbar is not None
    assert window._main_toolbar.minimumSizeHint().width() <= 800
    assert 56 <= window._toolbar_widget.page_field.width() <= 64
    assert 90 <= window._toolbar_widget.zoom_field.width() <= 104


def test_main_window_status_left_container_has_margin_and_valid_icon(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    layout = window._status_left.layout()
    assert isinstance(layout, QHBoxLayout)
    assert 16 <= layout.contentsMargins().left() <= 18
    assert not window._status_icon.pixmap().isNull()
    assert window._status_message.geometry().left() > window._status_icon.geometry().right()
    assert window.statusBar().currentMessage() == ""
    assert window._status_message.text() == "準備完了"
    assert len(window.findChildren(QLabel, "statusMessageLabel")) == 1

    right_layout = window._status_right.layout()
    assert isinstance(right_layout, QHBoxLayout)
    assert 16 <= right_layout.contentsMargins().right() <= 18


def test_main_window_status_message_uses_custom_label_and_resets_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    window._set_status_message("エラーです", error=True, timeout_ms=10)

    assert window.statusBar().currentMessage() == ""
    assert window._status_message.text() == "エラーです"
    assert window._status_icon.property("error") is True
    assert window._status_icon.geometry().right() < window._status_message.geometry().left()

    qtbot.waitUntil(lambda: window._status_message.text() == "準備完了")
    assert window.statusBar().currentMessage() == ""
    assert window._status_icon.property("error") is False


def test_main_window_uses_custom_tab_close_buttons(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.touch()
    second.touch()
    window.open_document(first)
    window.open_document(second)

    tab_bar = window._tabs.tabBar()
    first_button = tab_bar.tabButton(0, QTabBar.ButtonPosition.RightSide)
    second_button = tab_bar.tabButton(1, QTabBar.ButtonPosition.RightSide)
    assert isinstance(first_button, QToolButton)
    assert isinstance(second_button, QToolButton)
    assert first_button.toolTip() == "閉じる"
    assert first_button.accessibleName() == "タブを閉じる"
    assert_button_icon_valid(first_button)
    assert_button_icon_valid(second_button)

    QTest.mouseClick(first_button, Qt.MouseButton.LeftButton)
    assert window._tabs.count() == 1
    assert window._documents[0].session.source_path == second.resolve()

    remaining_button = tab_bar.tabButton(0, QTabBar.ButtonPosition.RightSide)
    assert isinstance(remaining_button, QToolButton)
    QTest.mouseClick(remaining_button, Qt.MouseButton.LeftButton)
    assert window._tabs.count() == 0


def test_main_window_refreshes_tab_close_button_icons_on_theme_change(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "theme.pdf"
    document_path.touch()
    window.open_document(document_path)
    button = window._tabs.tabBar().tabButton(0, QTabBar.ButtonPosition.RightSide)
    assert isinstance(button, QToolButton)
    assert_button_icon_valid(button)

    from pdf_workbench.ui.theme import ColorScheme, apply_application_theme

    app = QApplication.instance()
    assert isinstance(app, QApplication)
    apply_application_theme(app, ColorScheme.DARK)
    window.refresh_theme_assets()
    assert_button_icon_valid(button)


def test_main_window_find_action_opens_search_ui(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "menu-search.pdf"
    document_path.touch()
    window.open_document(document_path)

    assert window.find_action.isEnabled() is True
    window.find_action.trigger()
    qtbot.waitUntil(lambda: window._search_ui_is_ready())
    assert_search_ui_ready(window)


def test_main_window_search_shortcut_opens_search_ui(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "shortcut-search.pdf"
    document_path.touch()
    window.open_document(document_path)
    document = window._documents[0]
    document.view.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(document.view.hasFocus)

    expected_sequence = (
        QKeySequence("Meta+F") if sys.platform == "darwin" else QKeySequence("Ctrl+F")
    )
    QTest.keySequence(window, expected_sequence)

    qtbot.waitUntil(lambda: window._search_ui_is_ready())
    assert_search_ui_ready(window)


def test_main_window_open_search_bar_refocuses_existing_input(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "refocus-search.pdf"
    document_path.touch()
    window.open_document(document_path)

    assert window.open_search_bar() is True
    window._toolbar_widget.page_field.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(window._toolbar_widget.page_field.hasFocus)

    assert window.open_search_bar() is True
    assert_search_ui_ready(window)


def test_main_window_copy_action_tracks_focus_and_selection(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "copy.pdf"
    document_path.touch()
    window.open_document(document_path)
    assert window.open_search_bar() is True

    line_edit = window._search_bar.search_input
    line_edit.setText("selected")
    line_edit.setSelection(0, len(line_edit.text()))
    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: line_edit))
    window._update_actions()

    assert window.copy_action.isEnabled()

    document = window._documents[0]
    document.view._selection = None
    document.view._search_query = ""
    window._on_focus_changed(line_edit, document.view)

    assert QApplication.focusWidget() is not None


def test_main_window_copy_action_prefers_line_edit_selection(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "copy-priority.pdf"
    document_path.touch()
    window.open_document(document_path)
    assert window.open_search_bar() is True

    line_edit = window._search_bar.search_input
    line_edit.setText("line-edit")
    line_edit.setSelection(0, 4)
    document = window._documents[0]
    document.view._selection = object()  # type: ignore[assignment]
    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: line_edit))

    copied: list[str] = []
    monkeypatch.setattr(line_edit, "copy", lambda: copied.append(line_edit.selectedText()))
    monkeypatch.setattr(document.view, "copy_selected_text", lambda: False)

    window._copy_selection()

    assert copied == ["line"]


def test_main_window_search_bar_enter_and_shift_enter_fire_once(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "search-events.pdf"
    document_path.touch()
    window.open_document(document_path)
    assert window.open_search_bar() is True

    next_calls = 0
    previous_calls = 0

    def record_next() -> bool:
        nonlocal next_calls
        next_calls += 1
        return False

    def record_previous() -> bool:
        nonlocal previous_calls
        previous_calls += 1
        return False

    monkeypatch.setattr(window._documents[0].view, "next_match", record_next)
    monkeypatch.setattr(window._documents[0].view, "previous_match", record_previous)

    QTest.keyClick(window._search_bar.search_input, Qt.Key.Key_Return)
    QTest.keyClick(
        window._search_bar.search_input,
        Qt.Key.Key_Return,
        Qt.KeyboardModifier.ShiftModifier,
    )

    assert next_calls == 1
    assert previous_calls == 1


def test_main_window_search_input_surface_tracks_focus_and_clear_button(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "search-focus.pdf", 1)
    window.open_document(document_path)
    assert window.open_search_bar() is True

    search_bar = window._search_bar
    assert "paintEvent" not in SearchBar.__dict__
    assert "paintEvent" in SearchInputSurface.__dict__
    assert search_bar.search_input_surface.height() == 40
    assert search_bar.search_icon.height() == 18
    assert search_bar.search_input.height() == 28
    assert search_bar.clear_button.height() == 26
    assert search_bar.search_input.actions() == []
    assert search_bar.clear_button.isHidden()
    assert search_bar.search_input_surface.property("focused") is True

    search_bar.search_input.setText("Alpha")
    qtbot.waitUntil(search_bar.clear_button.isVisible)
    assert search_bar.search_input_surface.property("focused") is True

    surface_center_y = search_bar.search_input_surface.geometry().center().y()
    for widget in (
        search_bar.search_icon,
        search_bar.search_input,
        search_bar.clear_button,
    ):
        assert abs(widget.geometry().center().y() - surface_center_y) <= 1
        assert widget.geometry().top() >= 0
        assert widget.geometry().bottom() <= search_bar.search_input_surface.height() - 1

    QTest.mouseClick(search_bar.clear_button, Qt.MouseButton.LeftButton)
    assert search_bar.search_input.text() == ""
    assert search_bar.clear_button.isHidden()
    window._toolbar_widget.page_field.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(lambda: window._search_bar.search_input_surface.property("focused") is False)


def test_main_window_search_progress_text_uses_failed_page_count() -> None:
    from pdf_workbench.ui.pdf_view import PdfSearchState

    state = PdfSearchState(
        query="",
        current_index=0,
        total_count=0,
        indexed_pages=2,
        total_pages=10,
        failed_pages=2,
        indexing_completed=False,
    )
    completed = PdfSearchState(
        query="",
        current_index=0,
        total_count=0,
        indexed_pages=8,
        total_pages=10,
        failed_pages=2,
        indexing_completed=True,
        text_pages_with_content=8,
    )

    assert MainWindow._search_progress_text(state) == "索引作成中 2 / 10\uff082ページ失敗\uff09"
    assert MainWindow._search_progress_text(completed) == "索引完了\uff082ページ失敗\uff09"


def test_main_window_search_progress_text_distinguishes_no_text_and_ocr() -> None:
    from pdf_workbench.ui.pdf_view import PdfSearchState

    no_text_state = PdfSearchState(
        query="abc",
        current_index=0,
        total_count=0,
        indexed_pages=1,
        total_pages=1,
        failed_pages=0,
        indexing_completed=True,
        text_pages_with_content=0,
        image_only_pages=0,
        empty_text_pages=1,
    )
    image_state = PdfSearchState(
        query="abc",
        current_index=0,
        total_count=0,
        indexed_pages=1,
        total_pages=1,
        failed_pages=0,
        indexing_completed=True,
        text_pages_with_content=0,
        image_only_pages=1,
        empty_text_pages=1,
    )

    assert MainWindow._search_progress_text(no_text_state) == "テキストレイヤーがありません"
    assert MainWindow._search_progress_text(image_state) == "OCRが必要な画像PDF"


def test_main_window_real_search_updates_after_index_completion(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = copy_pdf_fixture(
        "real-search-english.pdf",
        tmp_path / "real-search-english.pdf",
    )
    window = create_real_main_window(qtbot, tmp_path, delay_seconds=0.35)

    window.open_document(document_path)
    assert window.open_search_bar() is True
    search_input = window._search_bar.search_input
    search_input.clear()
    search_input.setText("Alpha")
    window._search_bar.submit_current_query()

    qtbot.waitUntil(lambda: window._documents[0].view.search_state.query == "Alpha", timeout=8000)
    qtbot.waitUntil(lambda: window._documents[0].view.search_state.total_count == 2, timeout=8000)
    qtbot.waitUntil(lambda: window._documents[0].view.search_state.current_index == 1, timeout=8000)
    qtbot.waitUntil(lambda: window._documents[0].view.search_state.indexing_completed, timeout=8000)

    document = window._documents[0]
    assert document.view.search_state.failed_pages == 0
    assert window._search_bar.counter_label.text() == "1 / 2"
    assert window._search_bar.progress_label.text() == ""
    assert len(document.view._canvas.pages[0]._current_match_boxes) == 1
    assert len(document.view._canvas.pages[0]._match_boxes) == 2

    QTest.keyClick(window, Qt.Key.Key_F3)
    qtbot.waitUntil(lambda: document.view.search_state.current_index == 2)
    assert window._search_bar.counter_label.text() == "2 / 2"

    QTest.keyClick(window, Qt.Key.Key_F3, Qt.KeyboardModifier.ShiftModifier)
    qtbot.waitUntil(lambda: document.view.search_state.current_index == 1)

    document.view.close_document()
    assert isinstance(document.view._render_service, PdfRenderService)
    assert document.view._render_service.shutdown()


def test_main_window_real_search_supports_japanese_text(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = copy_pdf_fixture(
        "real-search-japanese.pdf",
        tmp_path / "real-search-japanese.pdf",
    )
    window = create_real_main_window(qtbot, tmp_path)

    window.open_document(document_path)
    assert window.open_search_bar() is True
    search_input = window._search_bar.search_input
    search_input.clear()
    search_input.setText("検索")
    window._search_bar.submit_current_query()

    qtbot.waitUntil(lambda: window._documents[0].view.search_state.query == "検索", timeout=8000)
    qtbot.waitUntil(lambda: window._documents[0].view.search_state.indexing_completed, timeout=8000)
    qtbot.waitUntil(lambda: window._documents[0].view.search_state.total_count == 2, timeout=8000)

    document = window._documents[0]
    assert document.view.search_state.current_index == 1
    assert window._search_bar.counter_label.text() == "1 / 2"
    assert window._search_bar.progress_label.text() == ""
    assert len(document.view._canvas.pages[0]._current_match_boxes) == 1

    document.view.close_document()
    assert isinstance(document.view._render_service, PdfRenderService)
    assert document.view._render_service.shutdown()


def test_main_window_real_search_reports_blank_pdf_without_text_layer(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_blank_pdf(tmp_path / "blank.pdf", 1)
    window = create_real_main_window(qtbot, tmp_path)

    window.open_document(document_path)
    assert window.open_search_bar() is True
    window._search_bar.search_input.setText("Alpha")
    window._search_bar.submit_current_query()

    qtbot.waitUntil(lambda: window._documents[0].view.search_state.indexing_completed, timeout=8000)

    assert window._search_bar.progress_label.text() == "テキストレイヤーがありません"
    assert window._search_bar.counter_label.text() == "0 / 0"

    document = window._documents[0]
    document.view.close_document()
    assert isinstance(document.view._render_service, PdfRenderService)
    assert document.view._render_service.shutdown()


def test_main_window_real_search_reports_image_pdf_needs_ocr(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    document_path = create_image_only_pdf(tmp_path / "image-only.pdf")
    window = create_real_main_window(qtbot, tmp_path)

    window.open_document(document_path)
    assert window.open_search_bar() is True
    window._search_bar.search_input.setText("scan")
    window._search_bar.submit_current_query()

    qtbot.waitUntil(lambda: window._documents[0].view.search_state.indexing_completed, timeout=8000)

    assert window._search_bar.progress_label.text() == "OCRが必要な画像PDF"
    assert window._search_bar.counter_label.text() == "0 / 0"

    document = window._documents[0]
    document.view.close_document()
    assert isinstance(document.view._render_service, PdfRenderService)
    assert document.view._render_service.shutdown()


def test_main_window_copy_action_falls_back_to_pdf_view_when_line_edit_has_no_selection(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "copy-fallback.pdf"
    document_path.touch()
    window.open_document(document_path)
    assert window.open_search_bar() is True

    line_edit = window._search_bar.search_input
    line_edit.setText("line-edit")
    line_edit.deselect()
    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: line_edit))

    copied = {"pdf": 0}

    def copy_selected_text() -> bool:
        copied["pdf"] += 1
        return True

    monkeypatch.setattr(
        window._documents[0].view,
        "copy_selected_text",
        copy_selected_text,
    )

    window._copy_selection()

    assert copied["pdf"] == 1


@pytest.mark.parametrize(
    ("user_zoom", "logical_zoom"),
    [
        (0.25, 0.375),
        (1.0, 1.5),
        (1.25, 1.875),
        (2.0, 3.0),
        (3.33, pytest.approx(4.995)),
        (4.0, 6.0),
        (5.0, 7.5),
    ],
)
def test_main_window_maps_user_zoom_to_logical_zoom(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
    user_zoom: float,
    logical_zoom: float,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    document_path = tmp_path / "zoom.pdf"
    document_path.touch()
    window.open_document(document_path)
    window._set_zoom_from_toolbar(user_zoom)

    assert window._documents[0].session.zoom_factor == pytest.approx(user_zoom)
    assert window._documents[0].view.zoom_factor == pytest.approx(logical_zoom)


def test_main_window_accepts_pdf_drag_and_drop(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    document_path = tmp_path / "drop.pdf"
    document_path.touch()
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(document_path))])

    drag_event = QDragEnterEvent(
        QPoint(10, 10),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dragEnterEvent(drag_event)
    assert drag_event.isAccepted()

    drop_event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    window.dropEvent(drop_event)

    assert window._tabs.count() == 1
    assert window._documents[0].session.source_path == document_path.resolve()


def test_main_window_opens_real_pdf_document(
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    document_path = create_blank_pdf(tmp_path / "real.pdf", 1)

    window.open_document(document_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 1)

    assert window._tabs.count() == 1
    assert window._documents[0].session.source_path == document_path.resolve()
    assert window._documents[0].view.page_count == 1


def test_main_window_save_as_integration_preserves_source_and_cleans_workspace(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    source_path = create_qt_text_pdf(
        tmp_path / "integration-source.pdf",
        ["Alpha Search Alpha", "Second page"],
    )
    source_bytes = source_path.read_bytes()
    target_path = tmp_path / "integration-output.pdf"
    window = create_real_main_window(qtbot, tmp_path)

    window.open_document(source_path)
    qtbot.waitUntil(lambda: window._documents[0].view.page_count == 2, timeout=8000)

    document = window._documents[0]
    workspace_directory = document.session.workspace_directory
    assert document.session.document_path == workspace_directory / "working.pdf"
    assert document.session.document_path.read_bytes() == source_bytes

    document.session.mark_modified("simulated page operation")
    window._update_actions()
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        lambda *args, **kwargs: (str(target_path), "PDF files (*.pdf)"),
    )

    assert window._save_current_document_as() is True
    assert document.session.source_path == target_path.resolve()
    assert document.session.is_modified is False
    assert source_path.read_bytes() == source_bytes

    with pikepdf.open(target_path) as saved_pdf:
        assert len(saved_pdf.pages) == 2

    rendered_document = pdfium.PdfDocument(str(target_path))
    try:
        assert len(rendered_document) == 2
        page = rendered_document[0]
        try:
            bitmap = page.render(scale=0.2)
            assert bitmap.width > 0
            assert bitmap.height > 0
        finally:
            page.close()
    finally:
        rendered_document.close()

    assert window.close_current_document() is True
    assert not workspace_directory.exists()


def test_main_window_shares_one_render_service_across_tabs(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.touch()
    second.touch()

    window.open_document(first)
    window.open_document(second)

    assert window._documents[0].view._render_service is window._render_service
    assert window._documents[1].view._render_service is window._render_service


def test_main_window_keeps_open_when_render_service_shutdown_times_out(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    monkeypatch.setattr(window._render_service, "shutdown", lambda timeout_ms=3000: False)

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is False
    monkeypatch.undo()
    window._render_service._thread.quit()
    assert window._render_service._thread.wait(5000) is True


def test_main_window_diagnostic_capture_stays_at_800_by_600(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    from pdf_workbench.__main__ import _apply_window_size, _build_ui_state

    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)

    document_path = tmp_path / "diag.pdf"
    document_path.touch()
    _apply_window_size(window, "800x600")
    window.show()
    qtbot.waitUntil(window.isVisible)
    window.open_document(document_path)
    assert window.open_search_bar() is True
    qtbot.waitUntil(lambda: window.width() == 800 and window.height() == 600)

    screenshot_path = tmp_path / "window-800x600.png"
    assert window.grab().save(str(screenshot_path))
    image = QImage(str(screenshot_path))

    payload = _build_ui_state(window, requested_window_size="800x600")

    assert not image.isNull()
    assert image.width() == 800
    assert image.height() == 600
    assert payload["actual_window_size"] == [800, 600]
    assert payload["search_input_surface_size"] == [360, 40]
    assert payload["search_input_surface_geometry"][3] == 40
    assert payload["search_input_surface_border_geometry"][3] == 40
    assert (
        payload["search_input_surface_geometry"] == payload["search_input_surface_border_geometry"]
    )


def test_main_window_shows_source_change_banner_and_tab_suffix(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "source.pdf"
    document_path.touch()
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None

    result = SourceCheckResult(
        path=document.session.source_path,
        status=SourceStatus.MODIFIED,
        expected_fingerprint=document.session.source_fingerprint,
        current_fingerprint=FileFingerprint(size_bytes=999, modified_time_ns=999),
        checked_at=datetime.now(UTC),
        error_message="modified",
    )
    window._on_source_status_changed(document.session.session_id, result)

    assert window._source_change_banner.isVisible() is True
    assert "[外部変更]" in window._tab_title(document)
    assert "外部で変更" in window._tabs.tabToolTip(window._tabs.currentIndex())
    assert window.save_action.isEnabled() is True


def test_main_window_dismissed_banner_reappears_after_new_revision(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "source.pdf"
    document_path.touch()
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None

    first_result = SourceCheckResult(
        path=document.session.source_path,
        status=SourceStatus.MODIFIED,
        expected_fingerprint=document.session.source_fingerprint,
        current_fingerprint=FileFingerprint(size_bytes=999, modified_time_ns=999),
        checked_at=datetime.now(UTC),
        error_message="modified",
    )
    window._on_source_status_changed(document.session.session_id, first_result)
    window._dismiss_current_source_banner()
    assert window._source_change_banner.isVisible() is False

    second_result = SourceCheckResult(
        path=document.session.source_path,
        status=SourceStatus.MISSING,
        expected_fingerprint=document.session.source_fingerprint,
        current_fingerprint=None,
        checked_at=datetime.now(UTC),
        error_message="missing",
    )
    window._on_source_status_changed(document.session.session_id, second_result)

    assert window._source_change_banner.isVisible() is True
    assert "削除または移動" in window._source_change_banner.message_label.text()


def test_main_window_dismissed_banner_stays_hidden_for_identical_result(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "source.pdf"
    document_path.touch()
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None

    result = SourceCheckResult(
        path=document.session.source_path,
        status=SourceStatus.MODIFIED,
        expected_fingerprint=document.session.source_fingerprint,
        current_fingerprint=FileFingerprint(size_bytes=999, modified_time_ns=999),
        checked_at=datetime.now(UTC),
        error_message="modified",
    )
    window._on_source_status_changed(document.session.session_id, result)
    window._dismiss_current_source_banner()

    window._on_source_status_changed(document.session.session_id, result)

    assert window._source_change_banner.isVisible() is False


def test_main_window_save_routes_to_save_as_when_source_is_modified(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "source.pdf"
    document_path.touch()
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None
    document.session.mark_modified("edit")
    modified_result = SourceCheckResult(
        path=document.session.source_path,
        status=SourceStatus.MODIFIED,
        expected_fingerprint=document.session.source_fingerprint,
        current_fingerprint=FileFingerprint(size_bytes=999, modified_time_ns=999),
        checked_at=datetime.now(UTC),
        error_message="modified",
    )
    monkeypatch.setattr(
        window._source_change_monitor,
        "check_session_now",
        lambda _session: modified_result,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard,
    )
    routed: list[bool] = []
    monkeypatch.setattr(window, "_save_current_document_as", lambda: routed.append(True) or True)

    assert window._save_current_document() is True
    assert routed == [True]


def test_main_window_target_changed_error_keeps_session_dirty(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    window = MainWindow(settings, save_service=TargetChangedSaveService())
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = tmp_path / "source.pdf"
    document_path.touch()
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None
    document.session.mark_modified("edit")
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard,
    )
    reported: list[str] = []
    monkeypatch.setattr(window, "_report_error", lambda _title, message: reported.append(message))

    assert window._save_current_document() is False
    assert document.session.is_modified is True
    assert "上書きを中止しました" in reported[0]


def test_main_window_normal_save_race_does_not_adopt_new_source_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    replacement_pdf = create_blank_pdf(tmp_path / "changed-by-other-process.pdf", 3)
    settings = create_settings(tmp_path)
    window = MainWindow(
        settings,
        save_service=RaceTargetChangedSaveService(replacement_pdf),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None

    baseline_fingerprint = document.session.source_fingerprint
    baseline_source_bytes = document.session.source_path.read_bytes()
    working_copy_bytes = document.session.document_path.read_bytes()
    document.session.mark_modified("edit")

    reported: list[str] = []
    monkeypatch.setattr(window, "_report_error", lambda _title, message: reported.append(message))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard,
    )

    assert window._save_current_document() is False
    assert document.session.is_modified is True
    assert document.session.source_fingerprint == baseline_fingerprint
    assert document.session.document_path.read_bytes() == working_copy_bytes
    assert document.session.source_path.read_bytes() != baseline_source_bytes
    assert document.session.source_path.read_bytes() == replacement_pdf.read_bytes()
    assert document.session.source_status is SourceStatus.MODIFIED
    assert document.session.requires_save_as is True
    assert window._source_change_banner.isVisible() is True
    assert "上書きを中止しました" in reported[0]


def test_main_window_save_as_modified_source_confirms_once(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    save_service = FakeSaveService()
    window = MainWindow(settings, save_service=save_service)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    changed_pdf = create_blank_pdf(tmp_path / "changed.pdf", 2)
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None
    document.session.mark_modified("edit")
    document.session.source_path.write_bytes(changed_pdf.read_bytes())

    warning_calls: list[str] = []

    def confirm_once(*_args: object, **_kwargs: object) -> QMessageBox.StandardButton:
        warning_calls.append("warning")
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", confirm_once)

    assert window._save_document(document, target_path=document.session.source_path) is True
    assert warning_calls == ["warning"]
    assert len(save_service.calls) == 1


def test_main_window_save_as_current_source_rechecks_undetected_modified_file(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    save_service = FakeSaveService()
    window = MainWindow(settings, save_service=save_service)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    changed_pdf = create_blank_pdf(tmp_path / "changed.pdf", 2)
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None
    document.session.mark_modified("edit")
    baseline_bytes = document.session.source_path.read_bytes()
    changed_bytes = changed_pdf.read_bytes()
    document.session.source_path.write_bytes(changed_bytes)
    assert document.session.source_status is SourceStatus.UNCHANGED

    warning_calls: list[str] = []

    def choose_source_path(_document: DocumentTab) -> Path:
        return document.session.source_path

    def cancel_modified_warning(*_args: object, **_kwargs: object) -> QMessageBox.StandardButton:
        warning_calls.append("warning")
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(window, "_choose_save_as_path", choose_source_path)
    monkeypatch.setattr(QMessageBox, "warning", cancel_modified_warning)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard,
    )

    assert window._save_current_document_as() is False
    assert warning_calls == ["warning"]
    assert save_service.calls == []
    assert document.session.source_path.read_bytes() == changed_bytes
    assert document.session.source_path.read_bytes() != baseline_bytes
    assert document.session.source_status is SourceStatus.MODIFIED
    assert document.session.requires_save_as is True
    assert window._source_change_banner.isVisible() is True


def test_main_window_save_as_current_source_uses_baseline_snapshot_when_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    save_service = SnapshotRecordingSaveService()
    window = MainWindow(settings, save_service=save_service)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None
    document.session.mark_modified("edit")
    baseline_fingerprint = document.session.source_fingerprint

    monkeypatch.setattr(
        window, "_choose_save_as_path", lambda _document: document.session.source_path
    )

    assert window._save_current_document_as() is True
    assert save_service.calls == [document.session.source_path]
    assert save_service.snapshots == [TargetSnapshot(exists=True, fingerprint=baseline_fingerprint)]


def test_main_window_save_as_modified_source_rechecks_then_rejects_post_confirmation_change(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    changed_pdf = create_blank_pdf(tmp_path / "changed.pdf", 2)
    changed_again_pdf = create_blank_pdf(tmp_path / "changed-again.pdf", 3)
    save_service = SnapshotRecordingSaveService(changed_again_pdf)
    window = MainWindow(settings, save_service=save_service)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None
    document.session.mark_modified("edit")
    baseline_fingerprint = document.session.source_fingerprint
    document.session.source_path.write_bytes(changed_pdf.read_bytes())
    changed_fingerprint = FileFingerprint.from_path(document.session.source_path)

    warning_calls: list[str] = []
    reported: list[str] = []
    monkeypatch.setattr(
        window, "_choose_save_as_path", lambda _document: document.session.source_path
    )
    monkeypatch.setattr(window, "_report_error", lambda _title, message: reported.append(message))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard,
    )

    def accept_modified_warning(*_args: object, **_kwargs: object) -> QMessageBox.StandardButton:
        warning_calls.append("warning")
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", accept_modified_warning)

    assert window._save_current_document_as() is False
    assert warning_calls == ["warning"]
    assert save_service.snapshots == [TargetSnapshot(exists=True, fingerprint=changed_fingerprint)]
    assert document.session.source_path.read_bytes() == changed_again_pdf.read_bytes()
    assert document.session.is_modified is True
    assert document.session.source_fingerprint == baseline_fingerprint
    assert "上書きを中止しました" in reported[0]


def test_main_window_save_as_current_source_rechecks_undetected_missing_file(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    save_service = FakeSaveService()
    window = MainWindow(settings, save_service=save_service)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None
    document.session.mark_modified("edit")
    document.session.source_path.unlink()
    missing_result = SourceCheckResult(
        path=document.session.source_path,
        status=SourceStatus.MISSING,
        expected_fingerprint=document.session.source_fingerprint,
        current_fingerprint=None,
        checked_at=datetime.now(UTC),
        error_message="missing",
    )
    assert document.session.source_status is SourceStatus.UNCHANGED

    warning_calls: list[str] = []
    monkeypatch.setattr(
        window, "_choose_save_as_path", lambda _document: document.session.source_path
    )
    monkeypatch.setattr(
        window._source_change_monitor,
        "check_session_now",
        lambda _session: missing_result,
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard,
    )

    def cancel_missing_warning(*_args: object, **_kwargs: object) -> QMessageBox.StandardButton:
        warning_calls.append("warning")
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "warning", cancel_missing_warning)

    assert window._save_current_document_as() is False
    assert warning_calls == ["warning"]
    assert save_service.calls == []
    assert document.session.source_status is SourceStatus.MISSING
    assert document.session.requires_save_as is True


def test_main_window_save_as_missing_source_rejects_recreation_race(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    recreated_pdf = create_blank_pdf(tmp_path / "recreated.pdf", 4)
    save_service = SnapshotRecordingSaveService(recreated_pdf)
    window = MainWindow(settings, save_service=save_service)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None
    document.session.mark_modified("edit")
    baseline_fingerprint = document.session.source_fingerprint
    document.session.source_path.unlink()
    missing_result = SourceCheckResult(
        path=document.session.source_path,
        status=SourceStatus.MISSING,
        expected_fingerprint=document.session.source_fingerprint,
        current_fingerprint=None,
        checked_at=datetime.now(UTC),
        error_message="missing",
    )

    warning_calls: list[str] = []
    reported: list[str] = []
    monkeypatch.setattr(
        window, "_choose_save_as_path", lambda _document: document.session.source_path
    )
    monkeypatch.setattr(
        window._source_change_monitor,
        "check_session_now",
        lambda _session: missing_result,
    )
    monkeypatch.setattr(window, "_report_error", lambda _title, message: reported.append(message))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard,
    )

    def accept_missing_warning(*_args: object, **_kwargs: object) -> QMessageBox.StandardButton:
        warning_calls.append("warning")
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", accept_missing_warning)

    assert window._save_current_document_as() is False
    assert warning_calls == ["warning"]
    assert save_service.snapshots == [TargetSnapshot(exists=False, fingerprint=None)]
    assert document.session.source_path.read_bytes() == recreated_pdf.read_bytes()
    assert document.session.is_modified is True
    assert document.session.source_fingerprint == baseline_fingerprint
    assert "上書きを中止しました" in reported[0]


def test_main_window_save_as_current_source_rejects_unreadable_source(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    save_service = FakeSaveService()
    window = MainWindow(settings, save_service=save_service)
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    window.open_document(document_path)
    document = window._current_document()
    assert document is not None
    document.session.mark_modified("edit")
    reported: list[tuple[str, str]] = []
    unreadable_result = SourceCheckResult(
        path=document.session.source_path,
        status=SourceStatus.UNREADABLE,
        expected_fingerprint=document.session.source_fingerprint,
        current_fingerprint=None,
        checked_at=datetime.now(UTC),
        error_message="unreadable",
    )

    monkeypatch.setattr(
        window, "_choose_save_as_path", lambda _document: document.session.source_path
    )
    monkeypatch.setattr(
        window._source_change_monitor,
        "check_session_now",
        lambda _session: unreadable_result,
    )
    monkeypatch.setattr(
        window, "_report_error", lambda title, message: reported.append((title, message))
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Discard,
    )

    assert window._save_current_document_as() is False
    assert save_service.calls == []
    assert document.session.source_status is SourceStatus.UNREADABLE
    assert reported == [
        (
            "保存できません",
            "元のPDFの状態を確認できないため、この場所への上書きはできません。別の保存先を選択してください。",
        )
    ]


def test_main_window_open_document_rolls_back_when_monitor_registration_fails(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    settings = create_settings(tmp_path)
    workspace_manager = TrackingWorkspaceManager(tmp_path / "sessions")
    window = MainWindow(
        settings,
        workspace_manager=workspace_manager,
        source_change_monitor=FailingSourceChangeMonitor(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)

    document_path = create_blank_pdf(tmp_path / "source.pdf", 1)
    reported: list[str] = []
    monkeypatch.setattr(window, "_report_error", lambda _title, message: reported.append(message))

    window.open_document(document_path)

    assert window._tabs.count() == 0
    assert window._documents == []
    assert workspace_manager.cleaned_sessions
    assert "monitor failure" in reported[0]


def test_main_window_undo_redo_disabled_without_document(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    window = MainWindow(create_settings(tmp_path))
    qtbot.addWidget(window)
    show_window(qtbot, window)

    assert window.undo_action.isEnabled() is False
    assert window.redo_action.isEnabled() is False
    assert window.undo_action.text() == "元に戻す"
    assert window.redo_action.text() == "やり直す"


def test_main_window_execute_command_updates_dirty_state_and_actions(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    recovery_service = FakeRecoveryService()
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
        recovery_service=recovery_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    document_path = create_blank_pdf(tmp_path / "history.pdf", 1)
    window.open_document(document_path)
    recovery_service.write_calls.clear()

    command = StubDocumentCommand("回転", affected_pages=frozenset({0}))

    assert window.execute_document_command(command) is True

    document = window._documents[0]
    assert document.session.is_modified is True
    assert document.session.operation_history[-1] == "回転"
    assert window.undo_action.isEnabled() is True
    assert window.undo_action.text() == "元に戻す: 回転"
    assert window.redo_action.isEnabled() is False
    assert recovery_service.write_calls == [document.session.session_id]


def test_main_window_undo_and_redo_follow_clean_marker(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    document_path = create_blank_pdf(tmp_path / "clean-marker.pdf", 1)
    window.open_document(document_path)

    assert window.execute_document_command(StubDocumentCommand("Rotate")) is True

    document = window._documents[0]
    assert window._save_current_document() is True
    assert document.session.is_modified is False
    assert window.undo_action.isEnabled() is True
    assert window.redo_action.isEnabled() is False

    assert window._undo_current_command() is True
    assert document.session.is_modified is True
    assert window.redo_action.isEnabled() is True
    assert document.session.operation_history[-1] == "Undo: Rotate"

    assert window._redo_current_command() is True
    assert document.session.is_modified is False
    assert document.session.operation_history[-1] == "Redo: Rotate"


def test_main_window_save_failure_does_not_move_clean_marker(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    save_service = FakeSaveService(should_fail=True)
    reported: list[tuple[str, str]] = []
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=save_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    monkeypatch.setattr(
        window,
        "_report_error",
        lambda title, message: reported.append((title, message)),
    )
    document_path = create_blank_pdf(tmp_path / "save-fail-history.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    assert window.execute_document_command(StubDocumentCommand("Rotate")) is True

    assert window._save_current_document() is False
    assert document.session.is_modified is True
    assert document.command_history.is_dirty is True
    assert window.undo_action.isEnabled() is True
    assert reported


def test_main_window_command_history_is_per_tab(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    first = create_blank_pdf(tmp_path / "first-history.pdf", 1)
    second = create_blank_pdf(tmp_path / "second-history.pdf", 1)
    window.open_document(first)
    window.open_document(second)

    assert window.execute_document_command(StubDocumentCommand("Second tab edit")) is True
    assert window.undo_action.text() == "元に戻す: Second tab edit"

    window._tabs.setCurrentIndex(0)
    assert window.undo_action.isEnabled() is False

    window.execute_document_command(StubDocumentCommand("First tab edit"))
    assert window.undo_action.text() == "元に戻す: First tab edit"

    window._tabs.setCurrentIndex(1)
    assert window.undo_action.text() == "元に戻す: Second tab edit"


def test_main_window_recovered_modified_session_stays_dirty_with_empty_history(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    document_path = create_blank_pdf(tmp_path / "recovered-history.pdf", 1)
    session = create_workspace_manager(tmp_path).create_session(document_path)
    session.mark_modified("Recovered edit")
    session.mark_recovered(SourceStatus.MODIFIED)

    assert window.restore_session(session) is RestoreSessionResult.ATTACHED

    document = window._documents[0]
    assert document.session.is_modified is True
    assert document.command_history.is_dirty is True
    assert document.command_history.can_undo is False
    assert document.command_history.can_redo is False
    assert window.undo_action.isEnabled() is False
    assert window.redo_action.isEnabled() is False


def test_main_window_failed_execute_undo_redo_leave_history_state_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    document_path = create_blank_pdf(tmp_path / "failed-history.pdf", 1)
    window.open_document(document_path)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window,
        "_report_error",
        lambda title, message: errors.append((title, message)),
    )

    assert window.execute_document_command(StubDocumentCommand("Boom", fail_execute=True)) is False

    document = window._documents[0]
    assert document.session.is_modified is False
    assert document.session.operation_history == []
    assert document.command_history.can_undo is False
    assert errors

    assert window.execute_document_command(StubDocumentCommand("Works")) is True
    assert window._undo_current_command() is True
    assert window._redo_current_command() is True

    failing_undo_window = MainWindow(
        create_settings(tmp_path / "undo"),
        workspace_manager=create_workspace_manager(tmp_path / "undo"),
    )
    qtbot.addWidget(failing_undo_window)
    show_window(qtbot, failing_undo_window)
    second_document_path = create_blank_pdf(tmp_path / "undo-fail.pdf", 1)
    failing_undo_window.open_document(second_document_path)
    monkeypatch.setattr(
        failing_undo_window,
        "_report_error",
        lambda title, message: errors.append((title, message)),
    )
    assert (
        failing_undo_window.execute_document_command(
            StubDocumentCommand("Undo fail", fail_undo=True)
        )
        is True
    )
    assert failing_undo_window._undo_current_command() is False
    assert failing_undo_window._documents[0].command_history.can_undo is True

    redo_window = MainWindow(
        create_settings(tmp_path / "redo"),
        workspace_manager=create_workspace_manager(tmp_path / "redo"),
    )
    qtbot.addWidget(redo_window)
    show_window(qtbot, redo_window)
    third_document_path = create_blank_pdf(tmp_path / "redo-fail.pdf", 1)
    redo_window.open_document(third_document_path)
    monkeypatch.setattr(
        redo_window,
        "_report_error",
        lambda title, message: errors.append((title, message)),
    )
    assert (
        redo_window.execute_document_command(StubDocumentCommand("Redo fail", fail_redo=True))
        is True
    )
    assert redo_window._undo_current_command() is True
    assert redo_window._redo_current_command() is False
    assert redo_window._documents[0].command_history.can_redo is True


def test_main_window_undo_redo_shortcuts_are_standard(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    window = MainWindow(create_settings(tmp_path))
    qtbot.addWidget(window)

    assert window.undo_action.shortcut().matches(QKeySequence.StandardKey.Undo)
    assert window.redo_action.shortcut().matches(QKeySequence.StandardKey.Redo)


def test_main_window_save_persists_metadata_after_clean_state_synchronization(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
        save_service=FakeSaveService(),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    document_path = create_blank_pdf(tmp_path / "metadata-order.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    assert window.execute_document_command(StubDocumentCommand("Rotate")) is True
    snapshots: list[tuple[bool, bool, bool]] = []

    def record_metadata(session: DocumentSession, *, required: bool = False) -> bool:
        snapshots.append(
            (
                document.command_history.is_dirty,
                session.is_modified,
                document.command_history.can_undo,
            )
        )
        return True

    monkeypatch.setattr(window, "_persist_recovery_metadata", record_metadata)

    assert window._save_current_document() is True

    assert snapshots[-1] == (False, False, True)
    assert document.command_history.can_undo is True
    assert document.command_history.is_dirty is False
    assert document.session.is_modified is False


def test_main_window_disables_undo_redo_while_saving(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    document_path = create_blank_pdf(tmp_path / "saving-actions.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    assert window.execute_document_command(StubDocumentCommand("Rotate")) is True

    document.session.is_saving = True
    window._update_actions()

    assert window.undo_action.isEnabled() is False
    assert window.redo_action.isEnabled() is False


def test_main_window_rejects_direct_command_changes_while_saving(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    recovery_service = FakeRecoveryService()
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
        recovery_service=recovery_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    document_path = create_blank_pdf(tmp_path / "saving-guards.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    assert window.execute_document_command(StubDocumentCommand("Rotate")) is True
    assert window._undo_current_command() is True
    recovery_service.write_calls.clear()
    document.session.is_saving = True
    baseline_history = list(document.session.operation_history)
    baseline_dirty = document.command_history.is_dirty

    execute_command = CountingDocumentCommand("Blocked execute")
    assert window.execute_document_command(execute_command) is False
    assert execute_command.execute_calls == 0

    assert window._undo_current_command() is False
    assert window._redo_current_command() is False
    assert document.command_history.is_dirty is baseline_dirty
    assert list(document.session.operation_history) == baseline_history
    assert recovery_service.write_calls == []
    assert document.command_history.undo_description is None
    assert document.command_history.redo_description == "Rotate"


def test_main_window_undo_action_prefers_line_edit_history(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: QtBot,
    tmp_path: Path,
) -> None:
    patch_pdf_open(monkeypatch)
    recovery_service = FakeRecoveryService()
    window = MainWindow(
        create_settings(tmp_path),
        workspace_manager=create_workspace_manager(tmp_path),
        recovery_service=recovery_service,
    )
    qtbot.addWidget(window)
    show_window(qtbot, window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Discard,
    )
    document_path = create_blank_pdf(tmp_path / "line-edit-undo.pdf", 1)
    window.open_document(document_path)
    document = window._documents[0]
    command = CountingDocumentCommand("Document edit")
    assert window.execute_document_command(command) is True
    recovery_service.write_calls.clear()
    assert window.open_search_bar() is True
    line_edit = window._search_bar.search_input
    line_edit.clear()
    line_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
    qtbot.keyClicks(line_edit, "Alpha")
    assert line_edit.text() == "Alpha"

    assert window._trigger_undo() is True
    assert line_edit.text() == ""
    assert document.command_history.can_undo is True
    assert command.undo_calls == 0
    assert command.redo_calls == 0
    assert document.session.is_modified is True
    assert document.session.operation_history == ["Document edit"]
    assert recovery_service.write_calls == []

    assert window._trigger_redo() is True
    assert line_edit.text() == "Alpha"
    assert document.command_history.can_undo is True
    assert command.undo_calls == 0
    assert command.redo_calls == 0
    assert document.session.operation_history == ["Document edit"]
    assert recovery_service.write_calls == []
