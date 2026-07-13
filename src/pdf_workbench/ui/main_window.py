from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from functools import partial
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QObject, QRect, QSettings, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QKeyEvent,
    QKeySequence,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdf_workbench.core.settings import configure_qsettings
from pdf_workbench.domain.command_history import (
    CommandChange,
    CommandExecutionError,
    CommandHistory,
    CommandRedoError,
    CommandUndoError,
    DocumentCommand,
)
from pdf_workbench.domain.document_session import DocumentSession, SourceStatus
from pdf_workbench.services.pdf_renderer import PdfRenderService
from pdf_workbench.services.pdf_save_service import (
    PdfSaveError,
    PdfSaveService,
    TargetChangedError,
    TargetSnapshot,
)
from pdf_workbench.services.session_recovery import (
    RecoveryMetadataError,
    SessionRecoveryService,
)
from pdf_workbench.services.session_workspace import (
    SessionWorkspaceManager,
    WorkspaceCreationError,
)
from pdf_workbench.services.source_change_monitor import (
    SourceChangeMonitor,
    SourceCheckResult,
)
from pdf_workbench.ui.icon_provider import IconName, IconProvider, IconTone
from pdf_workbench.ui.pdf_view import PdfView
from pdf_workbench.ui.widgets.document_toolbar import DocumentToolbar, ToolbarState
from pdf_workbench.ui.widgets.empty_state import EmptyState
from pdf_workbench.ui.widgets.search_bar import SearchBar, SearchBarState
from pdf_workbench.ui.widgets.source_change_banner import (
    SourceChangeBanner,
    SourceChangeBannerState,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DocumentTab:
    session: DocumentSession
    view: PdfView
    command_history: CommandHistory


class RestoreSessionResult(StrEnum):
    ATTACHED = "attached"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class SaveIntent(StrEnum):
    SAVE = "save"
    SAVE_AS = "save_as"


class MainWindow(QMainWindow):
    _RECENT_FILES_KEY = "main_window/recent_files"
    _GEOMETRY_KEY = "main_window/geometry"
    _MAX_RECENT_FILES = 10
    _BASE_RENDER_SCALE = 1.5
    _READY_STATUS_TEXT = "準備完了"

    def __init__(
        self,
        settings: QSettings | None = None,
        *,
        render_service: PdfRenderService | None = None,
        workspace_manager: SessionWorkspaceManager | None = None,
        save_service: PdfSaveService | None = None,
        recovery_service: SessionRecoveryService | None = None,
        source_change_monitor: SourceChangeMonitor | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings if settings is not None else configure_qsettings()
        self._render_service = (
            render_service if render_service is not None else PdfRenderService(self)
        )
        self._workspace_manager = (
            workspace_manager if workspace_manager is not None else SessionWorkspaceManager()
        )
        self._save_service = save_service if save_service is not None else PdfSaveService()
        self._recovery_service = (
            recovery_service
            if recovery_service is not None
            else SessionRecoveryService(self._workspace_manager)
        )
        self._source_change_monitor = (
            source_change_monitor
            if source_change_monitor is not None
            else SourceChangeMonitor(parent=self)
        )
        self._documents: list[DocumentTab] = []
        self._metadata_timers: dict[str, QTimer] = {}
        self._dismissed_source_banner_revisions: dict[str, int] = {}
        self._recent_files: list[Path] = self._load_recent_files()
        self._build_sha = os.environ.get("PDF_WORKBENCH_BUILD_SHA", "").strip()
        self._toolbar_widget = DocumentToolbar(self)
        self._search_bar = SearchBar(self)
        self._main_toolbar: QWidget | None = None
        self._main_toolbar_layout: QHBoxLayout | None = None
        self._search_toolbar: QWidget | None = None
        self._search_surface: QWidget | None = None
        self._workspace_overlay_host: QWidget | None = None
        self._empty_state = EmptyState(self)
        self._source_change_banner = SourceChangeBanner(self)
        self._search_row_height = 52
        self._status_reset_timer = QTimer(self)
        self._status_reset_timer.setSingleShot(True)
        self._status_reset_timer.timeout.connect(self._reset_status_message)

        self.setObjectName("mainWindow")
        self.setWindowTitle("PDF Workbench")
        self.resize(1100, 800)
        self.setAcceptDrops(True)

        self._tabs = QTabWidget(self)
        self._tabs.setObjectName("documentTabs")
        self._tabs.setDocumentMode(True)
        self._tabs.setTabsClosable(False)
        tab_bar = self._tabs.tabBar()
        tab_bar.setObjectName("documentTabBar")
        tab_bar.setElideMode(Qt.TextElideMode.ElideMiddle)
        tab_bar.setUsesScrollButtons(True)
        tab_bar.setDrawBase(False)
        tab_bar.setFixedHeight(40)
        self._tabs.currentChanged.connect(self._on_current_tab_changed)
        self._stack = QStackedWidget(self)
        self._stack.setObjectName("mainStack")
        self._stack.addWidget(self._empty_state)
        self._stack.addWidget(self._tabs)
        self._stack.currentChanged.connect(lambda _index: self._update_overlay_geometry())
        self._central_container = QWidget(self)
        self._central_container.setObjectName("centralContainer")
        self._central_layout = QVBoxLayout(self._central_container)
        self._central_layout.setContentsMargins(0, 0, 0, 0)
        self._central_layout.setSpacing(0)
        self.setCentralWidget(self._central_container)
        status_bar = QStatusBar(self)
        status_bar.setObjectName("mainStatusBar")
        status_bar.setSizeGripEnabled(False)
        self.setStatusBar(status_bar)
        self._status_left = QWidget(self)
        self._status_left.setObjectName("statusLeftContainer")
        status_left_layout = QHBoxLayout(self._status_left)
        status_left_layout.setContentsMargins(16, 0, 0, 0)
        status_left_layout.setSpacing(6)
        self._status_icon = QLabel("", self._status_left)
        self._status_icon.setObjectName("statusStateIcon")
        self._status_message = QLabel(self._READY_STATUS_TEXT, self._status_left)
        self._status_message.setObjectName("statusMessageLabel")
        status_left_layout.addWidget(self._status_icon)
        status_left_layout.addWidget(self._status_message)
        status_left_layout.addStretch(1)
        status_bar.addWidget(self._status_left, 1)
        self._status_right = QWidget(self)
        self._status_right.setObjectName("statusRightContainer")
        status_right_layout = QHBoxLayout(self._status_right)
        status_right_layout.setContentsMargins(0, 0, 16, 0)
        status_right_layout.setSpacing(0)
        self._status_summary = QLabel("", self._status_right)
        self._status_summary.setObjectName("statusSummaryLabel")
        status_right_layout.addStretch(1)
        status_right_layout.addWidget(self._status_summary)
        status_bar.addPermanentWidget(self._status_right)
        self._empty_state.open_requested.connect(self._choose_document)
        self._empty_state.recent_file_requested.connect(self.open_document)
        self._toolbar_widget.open_requested.connect(self._choose_document)
        self._toolbar_widget.search_requested.connect(self.open_search_bar)
        self._toolbar_widget.previous_requested.connect(self._previous_page)
        self._toolbar_widget.next_requested.connect(self._next_page)
        self._toolbar_widget.rotate_requested.connect(self._rotate_page)
        self._toolbar_widget.page_requested.connect(self._set_page_from_toolbar)
        self._toolbar_widget.zoom_requested.connect(self._set_zoom_from_toolbar)
        self._source_change_banner.save_as_requested.connect(self._save_current_document_as)
        self._source_change_banner.recheck_requested.connect(self._recheck_current_source_status)
        self._source_change_banner.dismiss_requested.connect(self._dismiss_current_source_banner)
        self._search_bar.search_requested.connect(self._search_text_changed)
        self._search_bar.next_requested.connect(self._next_match)
        self._search_bar.previous_requested.connect(self._previous_match)
        self._search_bar.close_requested.connect(self._close_search)
        app = QApplication.instance()
        focus_changed = getattr(app, "focusChanged", None)
        if focus_changed is not None:
            focus_changed.connect(self._on_focus_changed)
        if app is not None:
            app.installEventFilter(self)
        self._source_change_monitor.source_status_changed.connect(self._on_source_status_changed)

        self._create_actions()
        self._create_menu()
        self._create_toolbar()
        self._create_search_bar()
        self._restore_window_state()
        self._refresh_recent_file_actions()
        self._update_window_title()
        self.refresh_theme_assets()
        self._update_actions()
        self._update_status()
        self._apply_search_inset()

    def _create_actions(self) -> None:
        self.open_action = QAction("開く", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self._choose_document)

        self.close_action = QAction("閉じる", self)
        self.close_action.setShortcut(QKeySequence.StandardKey.Close)
        self.close_action.triggered.connect(self.close_current_document)

        self.exit_action = QAction("終了", self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        self.exit_action.triggered.connect(self.close)

        self.previous_action = QAction("前のページ", self)
        self.previous_action.setShortcut(Qt.Key.Key_PageUp)
        self.previous_action.triggered.connect(self._previous_page)

        self.next_action = QAction("次のページ", self)
        self.next_action.setShortcut(Qt.Key.Key_PageDown)
        self.next_action.triggered.connect(self._next_page)

        self.zoom_in_action = QAction("拡大", self)
        self.zoom_in_action.setShortcut(QKeySequence.StandardKey.ZoomIn)
        self.zoom_in_action.triggered.connect(lambda: self._change_zoom(1.2))

        self.zoom_out_action = QAction("縮小", self)
        self.zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self.zoom_out_action.triggered.connect(lambda: self._change_zoom(1 / 1.2))

        self.find_action = QAction("検索", self)
        find_shortcuts = list(QKeySequence.keyBindings(QKeySequence.StandardKey.Find))
        if not find_shortcuts:
            fallback = "Meta+F" if sys.platform == "darwin" else "Ctrl+F"
            find_shortcuts = [QKeySequence(fallback)]
        self.find_action.setShortcuts(find_shortcuts)
        self.find_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.find_action.triggered.connect(self.open_search_bar)

        self.find_next_action = QAction("次の検索結果", self)
        self.find_next_action.setShortcut(QKeySequence("F3"))
        self.find_next_action.triggered.connect(self._next_match)

        self.find_previous_action = QAction("前の検索結果", self)
        self.find_previous_action.setShortcut(QKeySequence("Shift+F3"))
        self.find_previous_action.triggered.connect(self._previous_match)

        self.save_action = QAction("保存", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.save_action.triggered.connect(self._save_current_document)

        self.undo_action = QAction("元に戻す", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.undo_action.triggered.connect(self._undo_current_command)

        self.redo_action = QAction("やり直す", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.redo_action.triggered.connect(self._redo_current_command)

        self.save_as_action = QAction("名前を付けて保存", self)
        self.save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.save_as_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.save_as_action.triggered.connect(self._save_current_document_as)

        self.copy_action = QAction("コピー", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self._copy_selection)

        for action in (
            self.open_action,
            self.undo_action,
            self.redo_action,
            self.save_action,
            self.save_as_action,
            self.close_action,
            self.exit_action,
            self.previous_action,
            self.next_action,
            self.zoom_in_action,
            self.zoom_out_action,
            self.find_action,
            self.find_next_action,
            self.find_previous_action,
            self.copy_action,
        ):
            self.addAction(action)

    def _create_menu(self) -> None:
        file_menu = self.menuBar().addMenu("ファイル")
        file_menu.addAction(self.open_action)
        file_menu.addAction(self.save_action)
        file_menu.addAction(self.save_as_action)
        file_menu.addSeparator()
        file_menu.addAction(self.close_action)
        self.recent_files_menu = file_menu.addMenu("最近使ったファイル")
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        view_menu = self.menuBar().addMenu("表示")
        view_menu.addAction(self.previous_action)
        view_menu.addAction(self.next_action)
        view_menu.addSeparator()
        view_menu.addAction(self.zoom_in_action)
        view_menu.addAction(self.zoom_out_action)

        edit_menu = self.menuBar().addMenu("編集")
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.find_action)
        edit_menu.addAction(self.find_next_action)
        edit_menu.addAction(self.find_previous_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.copy_action)

    def _create_toolbar(self) -> None:
        toolbar = QWidget(self._central_container)
        toolbar.setObjectName("mainToolbar")
        toolbar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._main_toolbar_layout = QHBoxLayout(toolbar)
        self._main_toolbar_layout.setContentsMargins(20, 0, 20, 0)
        self._main_toolbar_layout.setSpacing(0)
        self._main_toolbar_layout.addWidget(self._toolbar_widget)
        self._central_layout.addWidget(toolbar)
        self._main_toolbar = toolbar

    def _create_search_bar(self) -> None:
        self._workspace_overlay_host = self._stack
        self._search_toolbar = QWidget(self._workspace_overlay_host)
        self._search_toolbar.setObjectName("searchToolbar")
        self._search_toolbar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        layout = QHBoxLayout(self._search_toolbar)
        layout.setContentsMargins(24, 12, 24, 0)
        layout.setSpacing(0)
        layout.addStretch(1)
        self._search_surface = QWidget(self._search_toolbar)
        self._search_surface.setObjectName("searchSurface")
        self._search_surface.setMaximumWidth(620)
        self._search_surface.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )
        surface_layout = QHBoxLayout(self._search_surface)
        surface_layout.setContentsMargins(0, 0, 0, 0)
        surface_layout.setSpacing(0)
        surface_layout.addWidget(self._search_bar)
        self._search_surface.setFixedHeight(40)
        layout.addWidget(self._search_surface)
        self._source_change_banner.hide()
        self._central_layout.addWidget(self._source_change_banner)
        self._central_layout.addWidget(self._stack, 1)
        self._search_toolbar.hide()
        self._search_toolbar.updateGeometry()

    def _choose_document(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "PDFを開く", "", "PDF files (*.pdf)")
        if filename:
            self.open_document(Path(filename))

    def open_document(self, path: Path) -> None:
        normalized_path = path.expanduser().resolve()
        existing_index = self._find_document_index(normalized_path)
        if existing_index is not None:
            self._tabs.setCurrentIndex(existing_index)
            self._remember_recent_file(normalized_path)
            self._set_status_message(
                f"{normalized_path.name} はすでに開いています",
                timeout_ms=3000,
            )
            return

        session: DocumentSession | None = None
        try:
            session = self._workspace_manager.create_session(normalized_path)
            self._persist_recovery_metadata(session, required=True)
            self._attach_session(session, cleanup_on_failure=True)
        except WorkspaceCreationError as exc:
            logger.exception("Failed to create working copy: %s", path)
            self._report_error("PDFを開けません", str(exc))
            return
        except RecoveryMetadataError as exc:
            if session is not None:
                self._workspace_manager.cleanup_session(session)
            logger.exception("Failed to persist recovery metadata: %s", path)
            self._report_error("PDFを開けません", str(exc))
            return
        except Exception as exc:
            logger.exception("Failed to open PDF: %s", path)
            self._report_error("PDFを開けません", str(exc))
            return

    def restore_session(self, session: DocumentSession) -> RestoreSessionResult:
        existing_index = self._find_document_index(session.source_path)
        if existing_index is not None:
            self._tabs.setCurrentIndex(existing_index)
            self._workspace_manager.release_session_lock(session.session_id)
            self._set_status_message(
                "同じ元ファイルの復旧候補は保持したまま、既存タブを表示しました。",
                timeout_ms=4000,
            )
            return RestoreSessionResult.DUPLICATE
        try:
            self._attach_session(session, cleanup_on_failure=False)
        except Exception as exc:
            self._cancel_metadata_timer(session.session_id)
            self._workspace_manager.release_session_lock(session.session_id)
            logger.exception("Failed to restore interrupted session: %s", session.session_id)
            self._report_error("復旧に失敗しました", str(exc))
            return RestoreSessionResult.FAILED
        return RestoreSessionResult.ATTACHED

    def close_document_at(self, index: int) -> bool:
        if not 0 <= index < len(self._documents):
            return False

        document = self._documents[index]
        if document.session.is_modified:
            result = QMessageBox.question(
                self,
                "未保存の変更",
                f"{document.session.display_path.name} には未保存の変更があります。",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if result == QMessageBox.StandardButton.Cancel:
                return False
            if result == QMessageBox.StandardButton.Save and not self._save_document(document):
                return False
            if result not in (
                QMessageBox.StandardButton.Save,
                QMessageBox.StandardButton.Discard,
            ):
                return False

        widget = self._tabs.widget(index)
        self._cancel_metadata_timer(document.session.session_id)
        self._source_change_monitor.unregister_session(document.session.session_id)
        self._dismissed_source_banner_revisions.pop(document.session.session_id, None)
        self._tabs.removeTab(index)
        self._documents.pop(index)
        if widget is not None:
            if isinstance(widget, PdfView):
                widget.close_document()
            widget.deleteLater()
        self._workspace_manager.cleanup_session(document.session)
        if not self._documents:
            self._stack.setCurrentWidget(self._empty_state)
            if self._search_toolbar is not None:
                self._search_toolbar.hide()
            self._source_change_banner.hide()

        self._update_window_title()
        self._update_actions()
        self._update_status()
        self._update_overlay_geometry()
        self._apply_search_inset()
        return True

    def close_current_document(self) -> bool:
        return self.close_document_at(self._tabs.currentIndex())

    def _previous_page(self) -> None:
        document = self._current_document()
        if document is None:
            return
        document.view.set_page(document.view.page_index - 1)
        document.session.set_navigation_state(
            page_index=document.view.page_index,
            zoom_factor=document.session.zoom_factor,
        )
        self._schedule_recovery_metadata_persist(document.session)
        self._sync_toolbar(document)
        self._update_status()

    def _next_page(self) -> None:
        document = self._current_document()
        if document is None:
            return
        document.view.set_page(document.view.page_index + 1)
        document.session.set_navigation_state(
            page_index=document.view.page_index,
            zoom_factor=document.session.zoom_factor,
        )
        self._schedule_recovery_metadata_persist(document.session)
        self._sync_toolbar(document)
        self._update_status()

    def _change_zoom(self, multiplier: float) -> None:
        document = self._current_document()
        if document is None:
            return
        document.session.set_navigation_state(
            page_index=document.session.current_page_index,
            zoom_factor=max(
                0.25,
                min(document.session.zoom_factor * multiplier, 5.0),
            ),
        )
        document.view.set_zoom(self._BASE_RENDER_SCALE * document.session.zoom_factor)
        self._schedule_recovery_metadata_persist(document.session)
        self._sync_toolbar(document)
        self._update_status()

    def _rotate_page(self) -> None:
        document = self._current_document()
        if document is None:
            return
        rotation = (document.view.rotation + 90) % 360
        document.view.set_rotation(rotation)
        self._sync_toolbar(document)
        self._update_status()

    def open_search_bar(self) -> bool:
        document = self._current_document()
        if document is None:
            return False
        self.activateWindow()
        self.raise_()
        if self._search_toolbar is not None:
            self._search_toolbar.show()
            self._search_toolbar.raise_()
        if self._search_surface is not None:
            self._search_surface.setMinimumWidth(min(420, max(320, self.width() - 280)))
            self._search_surface.setMaximumWidth(max(420, min(620, self.width() - 64)))
        self._search_bar.show()
        self._search_bar.cancel_pending_search()
        self._search_bar.set_state(self._search_state_for(document))
        self._search_bar.focus_search()
        QApplication.processEvents()
        self._update_overlay_geometry()
        self._apply_search_inset()
        return self._search_ui_is_ready()

    def _prompt_search(self) -> None:
        self.open_search_bar()

    def _next_match(self) -> None:
        document = self._current_document()
        if document is None:
            return
        if not document.view.next_match():
            self._set_status_message("検索結果がありません", timeout_ms=3000)

    def _previous_match(self) -> None:
        document = self._current_document()
        if document is None:
            return
        if not document.view.previous_match():
            self._set_status_message("検索結果がありません", timeout_ms=3000)

    def _copy_selection(self) -> None:
        focus_widget = QApplication.focusWidget()
        if isinstance(focus_widget, QLineEdit) and focus_widget.hasSelectedText():
            focus_widget.copy()
            return
        document = self._current_document()
        if document is None:
            return
        if not document.view.copy_selected_text():
            self._set_status_message("コピーするテキストが選択されていません", timeout_ms=3000)

    def execute_document_command(self, command: DocumentCommand) -> bool:
        document = self._current_document()
        if document is None:
            return False
        try:
            change = document.command_history.execute(command)
        except CommandExecutionError as exc:
            logger.exception("Failed to execute command: %s", exc.command.description)
            self._report_error("編集に失敗しました", str(exc.cause))
            return False
        self._finalize_successful_command(
            document,
            description=command.description,
            change=change,
        )
        return True

    def _undo_current_command(self) -> bool:
        document = self._current_document()
        if document is None or not document.command_history.can_undo:
            return False
        description = document.command_history.undo_description
        if description is None:
            return False
        try:
            change = document.command_history.undo()
        except CommandUndoError as exc:
            logger.exception("Failed to undo command: %s", exc.command.description)
            self._report_error("元に戻せませんでした", str(exc.cause))
            return False
        self._finalize_successful_command(
            document,
            description=f"Undo: {description}",
            change=change,
        )
        return True

    def _redo_current_command(self) -> bool:
        document = self._current_document()
        if document is None or not document.command_history.can_redo:
            return False
        description = document.command_history.redo_description
        if description is None:
            return False
        try:
            change = document.command_history.redo()
        except CommandRedoError as exc:
            logger.exception("Failed to redo command: %s", exc.command.description)
            self._report_error("やり直せませんでした", str(exc.cause))
            return False
        self._finalize_successful_command(
            document,
            description=f"Redo: {description}",
            change=change,
        )
        return True

    def _save_current_document(self) -> bool:
        document = self._current_document()
        if document is None:
            return False
        return self._save_document(document)

    def _save_current_document_as(self) -> bool:
        document = self._current_document()
        if document is None:
            return False
        target_path = self._choose_save_as_path(document)
        if target_path is None:
            return False
        return self._save_document(document, target_path=target_path)

    def _save_document(
        self,
        document: DocumentTab,
        *,
        target_path: Path | None = None,
    ) -> bool:
        session = document.session
        if session.is_saving:
            return False
        intent = SaveIntent.SAVE if target_path is None else SaveIntent.SAVE_AS
        source_check_result: SourceCheckResult | None = None
        if intent is SaveIntent.SAVE:
            source_check_result = self._source_change_monitor.check_session_now(session)
        if (
            intent is SaveIntent.SAVE
            and source_check_result is not None
            and source_check_result.status is not SourceStatus.UNCHANGED
        ) or (intent is SaveIntent.SAVE and session.requires_save_as):
            self._set_status_message(
                "元のPDFが見つからないか変更されているため、別名で保存してください。",
                timeout_ms=5000,
            )
            return self._save_current_document_as()
        destination = (
            session.source_path if target_path is None else target_path.expanduser().resolve()
        )
        if destination.suffix.lower() != ".pdf":
            destination = destination.with_suffix(".pdf")
        if intent is SaveIntent.SAVE_AS and destination == session.source_path:
            source_check_result = self._source_change_monitor.check_session_now(session)
        if source_check_result is not None and not self._source_check_is_applied(
            session, source_check_result
        ):
            self._apply_source_check_result(session, source_check_result, notify=False)
        if self._workspace_manager.contains_managed_path(destination):
            self._report_error(
                "保存できません",
                "アプリの一時作業フォルダ内には保存できません。別の保存先を選択してください。",
            )
            return False
        if self._find_save_conflict(destination, session) is not None:
            self._report_error(
                "保存できません",
                "保存先が別のタブで開かれています。元のPDFは変更されていません。",
            )
            return False

        target_snapshot = self._prepare_target_snapshot(
            document,
            destination,
            intent=intent,
            source_check_result=source_check_result,
        )
        if target_snapshot is None:
            return False

        session.is_saving = True
        self._set_status_message("保存しています…")
        self._update_actions()
        QApplication.processEvents()
        saved = False
        try:
            self._save_service.save_atomic(
                session,
                destination,
                expected_page_count=document.view.page_count,
                target_snapshot=target_snapshot,
            )
            saved = True
        except TargetChangedError as exc:
            self._handle_target_changed_during_save(document, destination, exc)
        except PdfSaveError as exc:
            logger.exception("Failed to save PDF: %s", destination)
            self._report_error(
                "保存に失敗しました",
                f"{exc}\n\n元のPDFは変更されていません。",
            )
        finally:
            session.is_saving = False

        if not saved:
            self._update_actions()
            self._update_status()
            return False

        self._persist_recovery_metadata(session)
        document.command_history.mark_clean()
        session.set_modified(document.command_history.is_dirty)
        index = self._find_document_index(session.source_path)
        if index is not None:
            self._tabs.setTabText(index, self._tab_title(document))
            self._tabs.setTabToolTip(index, self._tab_tooltip(document))
        self._remember_recent_file(session.source_path)
        self._source_change_monitor.refresh_baseline(session)
        self._dismissed_source_banner_revisions.pop(session.session_id, None)
        self._refresh_source_change_banner()
        self._update_window_title()
        self._update_actions()
        self._update_status()
        self._set_status_message("保存しました", timeout_ms=3000)
        return True

    def _choose_save_as_path(self, document: DocumentTab) -> Path | None:
        initial_path = document.session.display_path
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "名前を付けて保存",
            str(initial_path),
            "PDF files (*.pdf)",
        )
        if not filename:
            return None
        target_path = Path(filename).expanduser().resolve()
        if target_path.suffix.lower() != ".pdf":
            target_path = target_path.with_suffix(".pdf")
        return target_path

    def _find_save_conflict(
        self,
        target_path: Path,
        current_session: DocumentSession,
    ) -> DocumentTab | None:
        resolved_target = target_path.expanduser().resolve()
        for document in self._documents:
            if document.session is current_session:
                continue
            if document.session.source_path == resolved_target:
                return document
        return None

    def _search_text_changed(self, query: str) -> None:
        document = self._current_document()
        if document is None:
            return
        document.view.search(query)
        self._sync_search_bar(document)

    def _close_search(self) -> None:
        document = self._current_document()
        self._search_bar.cancel_pending_search()
        if self._search_toolbar is not None:
            self._search_toolbar.hide()
        self._apply_search_inset()
        if document is None:
            return
        document.view.search("")
        self._sync_search_bar(document)

    def _recheck_current_source_status(self) -> None:
        document = self._current_document()
        if document is None:
            return
        self._source_change_monitor.check_session_now(document.session)

    def _dismiss_current_source_banner(self) -> None:
        document = self._current_document()
        if document is None:
            return
        self._dismissed_source_banner_revisions[document.session.session_id] = (
            document.session.source_status_revision
        )
        self._refresh_source_change_banner()

    def _set_page_from_toolbar(self, page_index: int) -> None:
        document = self._current_document()
        if document is None:
            return
        document.view.set_page(page_index)
        document.session.set_navigation_state(
            page_index=document.view.page_index,
            zoom_factor=document.session.zoom_factor,
        )
        self._schedule_recovery_metadata_persist(document.session)
        self._sync_toolbar(document)
        self._update_status()

    def _set_zoom_from_toolbar(self, zoom_factor: float) -> None:
        document = self._current_document()
        if document is None:
            return
        document.session.set_navigation_state(
            page_index=document.session.current_page_index,
            zoom_factor=max(0.25, min(zoom_factor, 5.0)),
        )
        document.view.set_zoom(self._BASE_RENDER_SCALE * document.session.zoom_factor)
        self._schedule_recovery_metadata_persist(document.session)
        self._sync_toolbar(document)
        self._update_status()

    def _update_status(self) -> None:
        document = self._current_document()
        if document is None:
            self._set_status_message("")
            self._status_summary.setText("")
            self._toolbar_widget.setState(ToolbarState(False, 0, 0, 1.0))
            return
        page_count = document.view.page_count
        current_page = 0 if page_count == 0 else document.view.page_index + 1
        self._status_summary.setText(
            f"{current_page} / {page_count} ページ  •  {document.session.zoom_factor:.0%}"
        )
        self._sync_toolbar(document)

    def closeEvent(self, event: QCloseEvent) -> None:
        for index in range(len(self._documents) - 1, -1, -1):
            if not self.close_document_at(index):
                event.ignore()
                return
        self._source_change_monitor.shutdown()
        shutdown_succeeded = self._render_service.shutdown()
        if not shutdown_succeeded:
            self._set_status_message("PDFレンダラーの終了を待ち切れませんでした", error=True)
            event.ignore()
            return
        self._workspace_manager.release_all_locks()
        self._save_window_state()
        event.accept()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime_data = event.mimeData()
        if self._has_pdf_urls(mime_data):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        mime_data = event.mimeData()
        for path in self._extract_pdf_paths(mime_data):
            self.open_document(path)
        event.acceptProposedAction()

    def _current_document(self) -> DocumentTab | None:
        index = self._tabs.currentIndex()
        if not 0 <= index < len(self._documents):
            return None
        return self._documents[index]

    def _find_document_index(self, path: Path) -> int | None:
        for index, document in enumerate(self._documents):
            if document.session.source_path == path:
                return index
        return None

    def _tab_title(self, document: DocumentTab) -> str:
        recovered_suffix = " [復元]" if document.session.recovered_from_interrupted_session else ""
        source_suffix = (
            " [外部変更]" if document.session.source_status is not SourceStatus.UNCHANGED else ""
        )
        modified_suffix = " *" if document.session.is_modified else ""
        return (
            f"{document.session.display_path.name}"
            f"{recovered_suffix}{source_suffix}{modified_suffix}"
        )

    def _update_window_title(self) -> None:
        suffix = ""
        if self._build_sha:
            suffix = f" [{self._build_sha}]"
        document = self._current_document()
        if document is None:
            self.setWindowTitle(f"PDF Workbench{suffix}")
            return
        self.setWindowTitle(f"{self._tab_title(document)} - PDF Workbench{suffix}")

    def _update_actions(self) -> None:
        has_document = self._current_document() is not None
        focus_widget = QApplication.focusWidget()
        text_selected = False
        if focus_widget is not None and hasattr(focus_widget, "hasSelectedText"):
            try:
                text_selected = bool(focus_widget.hasSelectedText())
            except Exception:
                text_selected = False
        document = self._current_document()
        document_selection = bool(document and document.view.selected_text)
        self.close_action.setEnabled(has_document)
        self._update_undo_redo_actions(document)
        self.save_action.setEnabled(
            bool(
                document
                and not document.session.is_saving
                and (document.session.is_modified or document.session.requires_save_as)
            )
        )
        self.save_as_action.setEnabled(bool(document and not document.session.is_saving))
        self.previous_action.setEnabled(has_document)
        self.next_action.setEnabled(has_document)
        self.zoom_in_action.setEnabled(has_document)
        self.zoom_out_action.setEnabled(has_document)
        self.find_action.setEnabled(has_document)
        self.find_next_action.setEnabled(has_document)
        self.find_previous_action.setEnabled(has_document)
        self.copy_action.setEnabled(text_selected or document_selection)
        if not has_document and self._search_toolbar is not None:
            self._search_bar.cancel_pending_search()
            self._search_toolbar.hide()
        self._update_overlay_geometry()
        self._apply_search_inset()

    def _on_current_tab_changed(self, _index: int) -> None:
        document = self._current_document()
        if document is not None and self._is_search_open():
            self._search_bar.cancel_pending_search()
            self._sync_search_bar(document)
        self._refresh_source_change_banner()
        self._update_window_title()
        self._update_actions()
        self._update_status()
        self._update_overlay_geometry()
        self._apply_search_inset()

    def _on_focus_changed(self, _old: QWidget | None, _new: QWidget | None) -> None:
        self._update_actions()

    @staticmethod
    def _is_find_shortcut_event(event: QKeyEvent) -> bool:
        if event.key() != Qt.Key.Key_F:
            return False
        expected_modifier = (
            Qt.KeyboardModifier.MetaModifier
            if sys.platform == "darwin"
            else Qt.KeyboardModifier.ControlModifier
        )
        return bool(event.modifiers() & expected_modifier)

    def _shortcut_action_for_event(self, event: QKeyEvent) -> QAction | None:
        if event.matches(QKeySequence.StandardKey.Save):
            return self.save_action
        if event.matches(QKeySequence.StandardKey.SaveAs):
            return self.save_as_action
        if self._is_find_shortcut_event(event):
            return self.find_action
        return None

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isinstance(watched, QWidget) or not isinstance(event, QKeyEvent):
            return super().eventFilter(watched, event)

        action = self._shortcut_action_for_event(event)
        if action is None or event.type() not in (
            QEvent.Type.ShortcutOverride,
            QEvent.Type.KeyPress,
        ):
            return super().eventFilter(watched, event)

        if action is self.find_action:
            focus_widget = QApplication.focusWidget()
            if (
                focus_widget is not None
                and focus_widget is not self._search_bar.search_input
                and isinstance(focus_widget, QLineEdit)
            ):
                return super().eventFilter(watched, event)

        if (
            self._current_document() is None
            or not self.isAncestorOf(watched)
            or not action.isEnabled()
        ):
            return super().eventFilter(watched, event)

        if event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True

        action.trigger()
        return True

    def _on_view_search_state_changed(self, view: PdfView) -> None:
        document = self._current_document()
        if document is not None and document.view is view:
            self._sync_search_bar(document)
        self._update_status()

    def _on_source_status_changed(self, session_id: str, result: object) -> None:
        if not isinstance(result, SourceCheckResult):
            return
        document = next(
            (item for item in self._documents if item.session.session_id == session_id),
            None,
        )
        if document is None:
            return
        changed = self._apply_source_check_result(document.session, result)
        if not changed:
            return
        self._persist_recovery_metadata(document.session)
        index = self._find_document_index(document.session.source_path)
        if index is not None:
            self._tabs.setTabText(index, self._tab_title(document))
            self._tabs.setTabToolTip(index, self._tab_tooltip(document))
        self._update_actions()
        self._update_window_title()
        self._refresh_source_change_banner()

    def _apply_source_check_result(
        self,
        session: DocumentSession,
        result: SourceCheckResult,
        *,
        notify: bool = True,
    ) -> bool:
        previous_status = session.source_status
        previous_revision = session.source_status_revision
        changed = session.apply_source_check(result)
        if session.source_status_revision != previous_revision:
            self._dismissed_source_banner_revisions.pop(session.session_id, None)
        if notify and changed and session.source_status is not previous_status:
            self._show_source_status_message(session.source_status)
        return changed

    @staticmethod
    def _source_check_is_applied(
        session: DocumentSession,
        result: SourceCheckResult,
    ) -> bool:
        return (
            session.source_status is result.status
            and session.last_observed_source_fingerprint == result.current_fingerprint
            and session.last_source_error_message == result.error_message
        )

    def _show_source_status_message(self, status: SourceStatus) -> None:
        if status is SourceStatus.MODIFIED:
            self._set_status_message("元のPDFが外部で変更されました", timeout_ms=5000)
        elif status is SourceStatus.MISSING:
            self._set_status_message("元のPDFが見つかりません", timeout_ms=5000)
        elif status is SourceStatus.UNREADABLE:
            self._set_status_message("元のPDFの状態を確認できません", timeout_ms=5000)

    def _refresh_source_change_banner(self) -> None:
        document = self._current_document()
        if document is None:
            self._source_change_banner.hide()
            return
        session = document.session
        if session.source_status is SourceStatus.UNCHANGED:
            self._source_change_banner.hide()
            return
        dismissed_revision = self._dismissed_source_banner_revisions.get(session.session_id)
        if dismissed_revision == session.source_status_revision:
            self._source_change_banner.hide()
            return
        if session.source_status is SourceStatus.MODIFIED:
            message = (
                "元のPDFが別のアプリで変更されました。現在のタブは作業コピーを表示しています。"
                "通常の上書き保存は停止されています。"
            )
        elif session.source_status is SourceStatus.MISSING:
            message = "元のPDFが削除または移動されました。作業コピーは保持されています。"
        else:
            message = (
                "元のPDFの状態を確認できません。安全のため通常の上書き保存は停止されています。"
            )
        self._source_change_banner.set_state(
            SourceChangeBannerState(
                status_text=session.source_status.value,
                message_text=message,
                source_path_text=str(session.source_path),
                visible=True,
            )
        )

    def _remember_recent_file(self, path: Path) -> None:
        normalized = path.resolve()
        self._recent_files = [item for item in self._recent_files if item != normalized]
        self._recent_files.insert(0, normalized)
        self._recent_files = self._recent_files[: self._MAX_RECENT_FILES]
        self._settings.setValue(
            self._RECENT_FILES_KEY,
            json.dumps([str(item) for item in self._recent_files], ensure_ascii=True),
        )
        self._refresh_recent_file_actions()

    def _load_recent_files(self) -> list[Path]:
        raw_value = self._settings.value(self._RECENT_FILES_KEY, "")
        if not isinstance(raw_value, str) or not raw_value:
            return []
        try:
            values = json.loads(raw_value)
        except json.JSONDecodeError:
            logger.warning("Failed to parse recent file list from settings")
            return []
        return [Path(item) for item in values if isinstance(item, str)]

    def _refresh_recent_file_actions(self) -> None:
        self.recent_files_menu.clear()
        existing_paths = [path for path in self._recent_files if path.exists()]
        if existing_paths != self._recent_files:
            self._recent_files = existing_paths
            self._settings.setValue(
                self._RECENT_FILES_KEY,
                json.dumps([str(item) for item in self._recent_files], ensure_ascii=True),
            )
        self._empty_state.set_recent_files(existing_paths[:5])
        if not existing_paths:
            action = self.recent_files_menu.addAction("最近使ったファイルはありません")
            action.setEnabled(False)
            return

        for path in existing_paths:
            action = self.recent_files_menu.addAction(str(path))
            action.triggered.connect(
                lambda checked=False, file_path=path: self.open_document(file_path)
            )

    def _attach_session(
        self,
        session: DocumentSession,
        *,
        cleanup_on_failure: bool,
    ) -> None:
        view: PdfView | None = None
        try:
            view = PdfView(self._render_service, self)
            view.set_zoom(self._BASE_RENDER_SCALE * session.zoom_factor)
            restored_page_index = session.current_page_index
            page_restored = False

            def apply_restored_page() -> None:
                nonlocal page_restored
                if page_restored:
                    return
                if view is None or view.page_count <= 0:
                    return
                page_restored = True
                target_page_index = min(restored_page_index, view.page_count - 1)
                view.set_page(target_page_index)
                session.set_navigation_state(
                    page_index=target_page_index,
                    zoom_factor=session.zoom_factor,
                )
                self._schedule_recovery_metadata_persist(session)

            view.document_loaded.connect(
                apply_restored_page,
                Qt.ConnectionType.SingleShotConnection,
            )
            view.open_document(session.document_path)
            if view.page_count > 0:
                apply_restored_page()
        except Exception:
            if view is not None:
                try:
                    view.close_document()
                except Exception:
                    logger.exception(
                        "Failed to close view after session attach error: %s",
                        session.session_id,
                    )
                view.deleteLater()
            if cleanup_on_failure:
                self._workspace_manager.cleanup_session(session)
            else:
                self._workspace_manager.release_session_lock(session.session_id)
            raise

        view.state_changed.connect(self._update_status)
        view.search_state_changed.connect(lambda: self._on_view_search_state_changed(view))
        view.selection_changed.connect(self._update_actions)
        view.error_occurred.connect(lambda message: self._set_status_message(message, error=True))
        document = DocumentTab(
            session=session,
            view=view,
            command_history=CommandHistory(initially_dirty=session.is_modified),
        )
        tab_index: int | None = None
        try:
            self._documents.append(document)
            tab_index = self._tabs.addTab(
                view,
                IconProvider.icon(IconName.DOCUMENT, tone=IconTone.MUTED, size=16),
                self._tab_title(document),
            )
            self._tabs.setTabToolTip(tab_index, self._tab_tooltip(document))
            self._install_tab_close_button(view)
            self._tabs.setCurrentIndex(tab_index)
            self._stack.setCurrentWidget(self._tabs)
            self._source_change_monitor.register_session(session)
        except Exception:
            self._cancel_metadata_timer(session.session_id)
            with suppress(Exception):
                self._source_change_monitor.unregister_session(session.session_id)
            if tab_index is not None:
                self._tabs.removeTab(tab_index)
            with suppress(ValueError):
                self._documents.remove(document)
            try:
                view.close_document()
            except Exception:
                logger.exception(
                    "Failed to close view after source monitor registration error: %s",
                    session.session_id,
                )
            view.deleteLater()
            if cleanup_on_failure:
                self._workspace_manager.cleanup_session(session)
            else:
                self._workspace_manager.release_session_lock(session.session_id)
            self._stack.setCurrentWidget(self._tabs if self._documents else self._empty_state)
            self._refresh_source_change_banner()
            self._update_window_title()
            self._update_actions()
            self._update_status()
            self._update_overlay_geometry()
            self._apply_search_inset()
            raise

        self._remember_recent_file(session.source_path)
        self._refresh_source_change_banner()
        self._update_window_title()
        self._update_actions()
        self._update_status()
        self._update_overlay_geometry()
        self._apply_search_inset()

    def _persist_recovery_metadata(
        self,
        session: DocumentSession,
        *,
        required: bool = False,
    ) -> bool:
        try:
            self._recovery_service.write_metadata(session)
        except RecoveryMetadataError as exc:
            logger.warning(
                "Failed to persist recovery metadata: session_id=%s error=%s",
                session.session_id,
                exc,
            )
            if required:
                raise
            return False
        return True

    def _schedule_recovery_metadata_persist(self, session: DocumentSession) -> None:
        timer = self._metadata_timers.get(session.session_id)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(500)
            timer.timeout.connect(
                lambda session_id=session.session_id: self._flush_session_metadata(session_id)
            )
            self._metadata_timers[session.session_id] = timer
        timer.start()

    def _flush_session_metadata(self, session_id: str) -> None:
        session = next(
            (
                document.session
                for document in self._documents
                if document.session.session_id == session_id
            ),
            None,
        )
        if session is None:
            return
        self._persist_recovery_metadata(session)

    def _cancel_metadata_timer(self, session_id: str) -> None:
        timer = self._metadata_timers.pop(session_id, None)
        if timer is None:
            return
        timer.stop()
        timer.deleteLater()

    def _mark_document_modified(
        self,
        document: DocumentTab,
        description: str,
    ) -> None:
        document.session.is_modified = True
        document.session.record_operation(description)
        self._persist_recovery_metadata(document.session)
        index = self._find_document_index(document.session.source_path)
        if index is not None:
            self._tabs.setTabText(index, self._tab_title(document))
        self._update_actions()
        self._update_window_title()
        self._update_status()

    def _finalize_successful_command(
        self,
        document: DocumentTab,
        *,
        description: str,
        change: CommandChange,
    ) -> None:
        document.session.set_modified(document.command_history.is_dirty)
        document.session.record_operation(description)
        self._persist_recovery_metadata(document.session)
        self._apply_command_change(document, change)
        index = self._find_document_index(document.session.source_path)
        if index is not None:
            self._tabs.setTabText(index, self._tab_title(document))
            self._tabs.setTabToolTip(index, self._tab_tooltip(document))
        self._update_actions()
        self._update_window_title()
        self._update_status()

    def _apply_command_change(self, document: DocumentTab, change: CommandChange) -> None:
        # Issue #9 only wires the invalidation seam. Concrete editing commands will decide
        # whether page-level or whole-document refresh is necessary in later milestones.
        _ = (document, change)

    def _update_undo_redo_actions(self, document: DocumentTab | None) -> None:
        if document is None:
            self.undo_action.setEnabled(False)
            self.redo_action.setEnabled(False)
            self.undo_action.setText("元に戻す")
            self.redo_action.setText("やり直す")
            return
        undo_description = document.command_history.undo_description
        redo_description = document.command_history.redo_description
        self.undo_action.setEnabled(document.command_history.can_undo)
        self.redo_action.setEnabled(document.command_history.can_redo)
        self.undo_action.setText(
            f"元に戻す: {undo_description}" if undo_description is not None else "元に戻す"
        )
        self.redo_action.setText(
            f"やり直す: {redo_description}" if redo_description is not None else "やり直す"
        )

    def _tab_tooltip(self, document: DocumentTab) -> str:
        lines = [str(document.session.source_path)]
        if document.session.recovered_from_interrupted_session:
            lines.append("復旧されたセッション")
        if document.session.source_status is SourceStatus.MODIFIED:
            lines.append("元のPDFが外部で変更されています")
            lines.append("現在のタブは作業コピーを表示しています")
        elif document.session.source_status is SourceStatus.MISSING:
            lines.append("元のPDFが削除または移動されています")
            lines.append("現在のタブは作業コピーを表示しています")
        elif document.session.source_status is SourceStatus.UNREADABLE:
            lines.append("元のPDFの状態を確認できません")
            lines.append("現在のタブは作業コピーを表示しています")
        if document.session.source_change_detected_at is not None:
            lines.append(
                "検知時刻: "
                + document.session.source_change_detected_at.astimezone(UTC).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            )
        return "\n".join(lines)

    def _save_window_state(self) -> None:
        self._settings.setValue(self._GEOMETRY_KEY, self.saveGeometry())
        self._settings.sync()

    def _restore_window_state(self) -> None:
        geometry = self._settings.value(self._GEOMETRY_KEY)
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _sync_toolbar(self, document: DocumentTab) -> None:
        self._toolbar_widget.setState(
            ToolbarState(
                has_document=True,
                page_index=document.view.page_index,
                page_count=document.view.page_count,
                zoom_factor=document.session.zoom_factor,
            )
        )
        self._sync_search_bar(document)

    def _sync_search_bar(self, document: DocumentTab) -> None:
        state = document.view.search_state
        if not self._is_search_open():
            return
        self._search_bar.set_state(
            SearchBarState(
                query=state.query,
                current_index=state.current_index,
                total_count=state.total_count,
                progress_text=self._search_progress_text(state),
            )
        )

    def _search_state_for(self, document: DocumentTab) -> SearchBarState:
        state = document.view.search_state
        return SearchBarState(
            query=state.query,
            current_index=state.current_index,
            total_count=state.total_count,
            progress_text=self._search_progress_text(state),
        )

    def _prepare_target_snapshot(
        self,
        document: DocumentTab,
        destination: Path,
        *,
        intent: SaveIntent,
        source_check_result: SourceCheckResult | None = None,
    ) -> TargetSnapshot | None:
        session = document.session
        if intent is SaveIntent.SAVE:
            if source_check_result is None:
                raise ValueError("source_check_result is required for SaveIntent.SAVE")
            if source_check_result.status is not SourceStatus.UNCHANGED:
                raise ValueError("SaveIntent.SAVE requires an unchanged source check result")
            if source_check_result.current_fingerprint != session.source_fingerprint:
                raise ValueError("SaveIntent.SAVE requires the baseline source fingerprint")
            return TargetSnapshot(exists=True, fingerprint=session.source_fingerprint)

        if destination == session.source_path:
            if source_check_result is None:
                raise ValueError(
                    "source_check_result is required when Save As targets the current source path"
                )
            if source_check_result.status is SourceStatus.UNCHANGED:
                return TargetSnapshot(exists=True, fingerprint=session.source_fingerprint)
            if source_check_result.status is SourceStatus.MODIFIED:
                if source_check_result.current_fingerprint is None:
                    self._report_error(
                        "保存できません",
                        "元のPDFの最新状態を確認できなかったため、この場所への上書きは中止しました。別の保存先を選択してください。",
                    )
                    return None
                result = QMessageBox.warning(
                    self,
                    "外部変更を上書きしますか",
                    "元のPDFは別のアプリで変更されています。現在の作業コピーで上書きすると、外部アプリの変更内容は失われます。上書きしますか？",
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if result != QMessageBox.StandardButton.Ok:
                    return None
                return TargetSnapshot(
                    exists=True,
                    fingerprint=source_check_result.current_fingerprint,
                )
            if source_check_result.status is SourceStatus.MISSING:
                result = QMessageBox.warning(
                    self,
                    "元のPDFを再作成しますか",
                    "元のPDFは削除または移動されています。この場所にPDFを再作成しますか？",
                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if result != QMessageBox.StandardButton.Ok:
                    return None
                return TargetSnapshot(exists=False, fingerprint=None)
            if source_check_result.status is SourceStatus.UNREADABLE:
                self._report_error(
                    "保存できません",
                    "元のPDFの状態を確認できないため、この場所への上書きはできません。別の保存先を選択してください。",
                )
                return None
        elif destination.exists():
            result = QMessageBox.question(
                self,
                "上書き確認",
                f"{destination.name} を上書きしますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                return None
        try:
            return TargetSnapshot.capture(destination)
        except OSError as exc:
            self._report_error("保存できません", f"保存先の状態を確認できませんでした。\n\n{exc}")
            return None

    def _handle_target_changed_during_save(
        self,
        document: DocumentTab,
        destination: Path,
        error: TargetChangedError,
    ) -> None:
        logger.warning("Target changed during save: %s (%s)", destination, error)
        self._source_change_monitor.check_session_now(document.session)
        self._refresh_source_change_banner()
        self._report_error(
            "保存に失敗しました",
            "保存中に保存先が別のプロセスで変更されたため、上書きを中止しました。元のPDFは変更していません。",
        )

    def _is_search_open(self) -> bool:
        return self._search_toolbar is not None and self._search_toolbar.isVisible()

    def _search_ui_is_ready(self) -> bool:
        if self._search_toolbar is None:
            return False
        if not self._search_toolbar.isVisible():
            return False
        if not self._search_bar.isVisible():
            return False
        if not self._search_bar.search_input.isVisible():
            return False
        if (
            self._search_toolbar.geometry().width() <= 0
            or self._search_toolbar.geometry().height() <= 0
        ):
            return False
        if self._search_bar.geometry().width() <= 0 or self._search_bar.geometry().height() <= 0:
            return False
        if (
            self._search_bar.search_input.geometry().width() <= 0
            or self._search_bar.search_input.geometry().height() <= 0
        ):
            return False
        if self._search_surface is None:
            return False
        if self._search_surface.geometry().height() < self._search_bar.sizeHint().height():
            return False
        return self._search_widgets_fit_surface()

    def refresh_theme_assets(self) -> None:
        self._toolbar_widget.refresh_theme_assets()
        self._search_bar.refresh_theme_assets()
        self._empty_state.refresh_theme_assets()
        self._refresh_tab_close_buttons()
        self._set_status_icon(error=self._status_icon.property("error") is True)

    @staticmethod
    def _search_progress_text(state: object) -> str:
        from pdf_workbench.ui.pdf_view import PdfSearchState

        if not isinstance(state, PdfSearchState):
            raise TypeError("state must be PdfSearchState")
        if not state.indexing_completed:
            text = f"索引作成中 {state.indexed_pages} / {state.total_pages}"
            if state.failed_pages:
                text += f"\uff08{state.failed_pages}ページ失敗\uff09"
            return text
        if state.text_pages_with_content == 0 and state.total_pages > 0:
            if state.image_only_pages == state.total_pages:
                text = "OCRが必要な画像PDF"
            else:
                text = "テキストレイヤーがありません"
            if state.failed_pages:
                text += f"\uff08{state.failed_pages}ページ失敗\uff09"
            return text
        if state.total_count == 0 and state.query:
            text = "0件"
            if state.failed_pages:
                text += f"\uff08{state.failed_pages}ページ失敗\uff09"
            return text
        if state.failed_pages:
            return f"索引完了\uff08{state.failed_pages}ページ失敗\uff09"
        return ""

    def _report_error(self, title: str, message: str) -> None:
        self._set_status_message(message, error=True)
        QMessageBox.critical(self, title, message)

    @staticmethod
    def _has_pdf_urls(mime_data: QMimeData) -> bool:
        return any(
            path.suffix.lower() == ".pdf" for path in MainWindow._extract_pdf_paths(mime_data)
        )

    @staticmethod
    def _extract_pdf_paths(mime_data: QMimeData) -> list[Path]:
        paths: list[Path] = []
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.suffix.lower() == ".pdf":
                paths.append(path)
        return paths

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self._main_toolbar_layout is not None:
            horizontal_margin = 12 if self.width() <= 800 else 20
            self._main_toolbar_layout.setContentsMargins(horizontal_margin, 0, horizontal_margin, 0)
        self._update_overlay_geometry()
        self._apply_search_inset()

    def _update_overlay_geometry(self) -> None:
        if self._search_toolbar is None or self._workspace_overlay_host is None:
            return
        if self._stack.currentWidget() is not self._tabs:
            self._search_toolbar.hide()
            return
        host = self._workspace_overlay_host
        top_margin = self._tabs.tabBar().height() + 14
        self._search_toolbar.setGeometry(0, top_margin, host.width(), self._search_row_height)
        if self._search_surface is not None:
            max_surface_width = min(620, max(420, host.width() - 72))
            self._search_surface.setMaximumWidth(max_surface_width)
            self._search_surface.setFixedHeight(40)
        self._search_toolbar.raise_()

    def _search_widgets_fit_surface(self) -> bool:
        if self._search_surface is None:
            return False
        surface_rect = self._search_surface.rect()
        if not surface_rect.contains(self._search_bar.geometry()):
            return False
        child_widgets = (
            self._search_bar.search_input_surface,
            self._search_bar.previous_button,
            self._search_bar.next_button,
            self._search_bar.close_button,
            self._search_bar.counter_label,
        )
        for widget in child_widgets:
            top_left = widget.mapTo(self._search_surface, widget.rect().topLeft())
            bottom_right = widget.mapTo(self._search_surface, widget.rect().bottomRight())
            child_rect = QRect(top_left, bottom_right)
            if not surface_rect.contains(child_rect):
                return False
        return True

    def _apply_search_inset(self) -> None:
        document = self._current_document()
        if document is None:
            return
        inset = 24
        if self._is_search_open():
            inset += self._search_row_height + 12
        document.view.set_search_overlay_inset(inset)

    def _set_status_message(
        self,
        message: str,
        *,
        error: bool = False,
        timeout_ms: int | None = None,
    ) -> None:
        self._status_reset_timer.stop()
        self._status_icon.setProperty("error", error)
        self._status_icon.style().unpolish(self._status_icon)
        self._status_icon.style().polish(self._status_icon)
        self._set_status_icon(error=error)
        self._status_message.setText(message or self._READY_STATUS_TEXT)
        self.statusBar().clearMessage()
        effective_timeout = timeout_ms
        if error and effective_timeout is None:
            effective_timeout = 5000
        if effective_timeout is not None and message:
            self._status_reset_timer.start(effective_timeout)

    def _reset_status_message(self) -> None:
        self._status_icon.setProperty("error", False)
        self._status_icon.style().unpolish(self._status_icon)
        self._status_icon.style().polish(self._status_icon)
        self._set_status_icon(error=False)
        self._status_message.setText(self._READY_STATUS_TEXT)
        self.statusBar().clearMessage()

    def _set_status_icon(self, *, error: bool) -> None:
        if error:
            icon = IconProvider.icon(IconName.STATUS_ERROR, tone=IconTone.DEFAULT, size=16)
        else:
            icon = IconProvider.icon(IconName.STATUS_SUCCESS, tone=IconTone.SUCCESS, size=16)
        self._status_icon.setPixmap(icon.pixmap(16, 16))

    def _install_tab_close_button(self, view: PdfView) -> None:
        tab_bar = self._tabs.tabBar()
        button = QToolButton(tab_bar)
        button.setObjectName("tabCloseButton")
        button.setAccessibleName("タブを閉じる")
        button.setToolTip("閉じる")
        button.setAutoRaise(True)
        button.setCursor(Qt.CursorShape.ArrowCursor)
        button.setFixedSize(24, 24)
        button.setIcon(IconProvider.icon(IconName.CLOSE, tone=IconTone.MUTED, size=16))
        button.clicked.connect(partial(self._close_document_for_view, view))
        index = self._tabs.indexOf(view)
        if index >= 0:
            tab_bar.setTabButton(index, QTabBar.ButtonPosition.RightSide, button)

    def _refresh_tab_close_buttons(self) -> None:
        tab_bar = self._tabs.tabBar()
        for index in range(self._tabs.count()):
            button = tab_bar.tabButton(index, QTabBar.ButtonPosition.RightSide)
            if isinstance(button, QToolButton):
                button.setIcon(IconProvider.icon(IconName.CLOSE, tone=IconTone.MUTED, size=16))

    def _close_document_for_view(self, view: PdfView) -> None:
        index = self._tabs.indexOf(view)
        if index >= 0:
            self.close_document_at(index)
