from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
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
from pdf_workbench.domain.document_session import DocumentSession
from pdf_workbench.services.pdf_renderer import PdfRenderService
from pdf_workbench.services.pdf_save_service import PdfSaveError, PdfSaveService
from pdf_workbench.services.session_workspace import (
    SessionWorkspaceManager,
    WorkspaceCreationError,
)
from pdf_workbench.ui.icon_provider import IconName, IconProvider, IconTone
from pdf_workbench.ui.pdf_view import PdfView
from pdf_workbench.ui.widgets.document_toolbar import DocumentToolbar, ToolbarState
from pdf_workbench.ui.widgets.empty_state import EmptyState
from pdf_workbench.ui.widgets.search_bar import SearchBar, SearchBarState

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DocumentTab:
    session: DocumentSession
    view: PdfView


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
        self._documents: list[DocumentTab] = []
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

        self.save_as_action = QAction("名前を付けて保存", self)
        self.save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.save_as_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.save_as_action.triggered.connect(self._save_current_document_as)

        self.copy_action = QAction("コピー", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self._copy_selection)

        for action in (
            self.open_action,
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
        view: PdfView | None = None
        try:
            session = self._workspace_manager.create_session(normalized_path)
            view = PdfView(self._render_service, self)
            view.set_zoom(self._BASE_RENDER_SCALE * session.zoom_factor)
            view.open_document(session.document_path)
        except WorkspaceCreationError as exc:
            logger.exception("Failed to create working copy: %s", path)
            self._report_error("PDFを開けません", str(exc))
            return
        except Exception as exc:
            if view is not None:
                try:
                    view.close_document()
                except Exception:
                    logger.exception("Failed to close view after open error: %s", path)
                view.deleteLater()
            if session is not None:
                self._workspace_manager.cleanup_session(session)
            logger.exception("Failed to open PDF: %s", path)
            self._report_error("PDFを開けません", str(exc))
            return

        assert session is not None
        assert view is not None
        view.state_changed.connect(self._update_status)
        view.search_state_changed.connect(lambda: self._on_view_search_state_changed(view))
        view.selection_changed.connect(self._update_actions)
        view.error_occurred.connect(lambda message: self._set_status_message(message, error=True))
        document = DocumentTab(session=session, view=view)
        self._documents.append(document)
        tab_index = self._tabs.addTab(
            view,
            IconProvider.icon(IconName.DOCUMENT, tone=IconTone.MUTED, size=16),
            self._tab_title(document),
        )
        self._install_tab_close_button(view)
        self._tabs.setCurrentIndex(tab_index)
        self._stack.setCurrentWidget(self._tabs)
        self._remember_recent_file(session.source_path)
        self._update_window_title()
        self._update_actions()
        self._update_status()
        self._update_overlay_geometry()
        self._apply_search_inset()

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
        document.session.current_page_index = document.view.page_index
        self._sync_toolbar(document)
        self._update_status()

    def _next_page(self) -> None:
        document = self._current_document()
        if document is None:
            return
        document.view.set_page(document.view.page_index + 1)
        document.session.current_page_index = document.view.page_index
        self._sync_toolbar(document)
        self._update_status()

    def _change_zoom(self, multiplier: float) -> None:
        document = self._current_document()
        if document is None:
            return
        document.session.zoom_factor = max(
            0.25,
            min(document.session.zoom_factor * multiplier, 5.0),
        )
        document.view.set_zoom(self._BASE_RENDER_SCALE * document.session.zoom_factor)
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
        destination = (
            session.source_path if target_path is None else target_path.expanduser().resolve()
        )
        if destination.suffix.lower() != ".pdf":
            destination = destination.with_suffix(".pdf")
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
            )
            saved = True
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

        index = self._find_document_index(session.source_path)
        if index is not None:
            self._tabs.setTabText(index, self._tab_title(document))
        self._remember_recent_file(session.source_path)
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
        if target_path.exists() and target_path != document.session.source_path:
            result = QMessageBox.question(
                self,
                "上書き確認",
                f"{target_path.name} を上書きしますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                return None
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

    def _set_page_from_toolbar(self, page_index: int) -> None:
        document = self._current_document()
        if document is None:
            return
        document.view.set_page(page_index)
        document.session.current_page_index = document.view.page_index
        self._sync_toolbar(document)
        self._update_status()

    def _set_zoom_from_toolbar(self, zoom_factor: float) -> None:
        document = self._current_document()
        if document is None:
            return
        document.session.zoom_factor = max(0.25, min(zoom_factor, 5.0))
        document.view.set_zoom(self._BASE_RENDER_SCALE * document.session.zoom_factor)
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
        shutdown_succeeded = self._render_service.shutdown()
        if not shutdown_succeeded:
            self._set_status_message("PDFレンダラーの終了を待ち切れませんでした", error=True)
            event.ignore()
            return
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
        suffix = " *" if document.session.is_modified else ""
        return f"{document.session.display_path.name}{suffix}"

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
        self.save_action.setEnabled(
            bool(document and document.session.is_modified and not document.session.is_saving)
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
