from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QObject, QSettings, QSize, Qt
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QWidget,
)

from pdf_workbench.core.settings import configure_qsettings
from pdf_workbench.domain.document_session import DocumentSession
from pdf_workbench.services.pdf_renderer import PdfRenderService
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

    def __init__(
        self,
        settings: QSettings | None = None,
        *,
        render_service: PdfRenderService | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings if settings is not None else configure_qsettings()
        self._render_service = (
            render_service if render_service is not None else PdfRenderService(self)
        )
        self._documents: list[DocumentTab] = []
        self._recent_files: list[Path] = self._load_recent_files()
        self._build_sha = os.environ.get("PDF_WORKBENCH_BUILD_SHA", "").strip()
        self._toolbar_widget = DocumentToolbar(self)
        self._search_bar = SearchBar(self)
        self._main_toolbar: QToolBar | None = None
        self._search_toolbar: QToolBar | None = None
        self._empty_state = EmptyState(self)

        self.setObjectName("mainWindow")
        self.setWindowTitle("PDF Workbench")
        self.resize(1100, 800)
        self.setAcceptDrops(True)

        self._tabs = QTabWidget(self)
        self._tabs.setObjectName("documentTabs")
        self._tabs.setDocumentMode(True)
        self._tabs.setTabsClosable(True)
        tab_bar = self._tabs.tabBar()
        tab_bar.setElideMode(Qt.TextElideMode.ElideMiddle)
        tab_bar.setUsesScrollButtons(True)
        self._tabs.currentChanged.connect(self._on_current_tab_changed)
        self._tabs.tabCloseRequested.connect(self.close_document_at)
        self._stack = QStackedWidget(self)
        self._stack.setObjectName("mainStack")
        self._stack.addWidget(self._empty_state)
        self._stack.addWidget(self._tabs)
        self.setCentralWidget(self._stack)
        status_bar = QStatusBar(self)
        status_bar.setObjectName("mainStatusBar")
        self.setStatusBar(status_bar)

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
        self._update_actions()
        self._update_status()

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

        self.copy_action = QAction("コピー", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self._copy_selection)

        for action in (
            self.open_action,
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
        toolbar = QToolBar("メイン", self)
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setAllowedAreas(Qt.ToolBarArea.TopToolBarArea)
        self.addToolBar(toolbar)
        toolbar.addWidget(self._toolbar_widget)
        self._main_toolbar = toolbar

    def _create_search_bar(self) -> None:
        self._search_toolbar = QToolBar("検索", self)
        self._search_toolbar.setObjectName("searchToolbar")
        self._search_toolbar.setMovable(False)
        self._search_toolbar.setFloatable(False)
        self._search_toolbar.setAllowedAreas(Qt.ToolBarArea.TopToolBarArea)
        self._search_toolbar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._search_toolbar)
        self._search_toolbar.hide()
        self._search_toolbar.addWidget(self._search_bar)
        self._search_toolbar.toggleViewAction().setVisible(False)

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
            self.statusBar().showMessage(
                f"{normalized_path.name} はすでに開いています",
                5000,
            )
            return

        try:
            session = DocumentSession(normalized_path)
            view = PdfView(self._render_service, self)
            view.set_zoom(self._BASE_RENDER_SCALE * session.zoom_factor)
            view.open_document(session.source_path)
        except Exception as exc:
            logger.exception("Failed to open PDF: %s", path)
            self._report_error("PDFを開けません", str(exc))
            return

        view.state_changed.connect(self._update_status)
        view.search_state_changed.connect(lambda: self._on_view_search_state_changed(view))
        view.selection_changed.connect(self._update_actions)
        view.error_occurred.connect(lambda message: self.statusBar().showMessage(message, 5000))
        document = DocumentTab(session=session, view=view)
        self._documents.append(document)
        tab_index = self._tabs.addTab(view, self._tab_title(document))
        self._tabs.setCurrentIndex(tab_index)
        self._stack.setCurrentWidget(self._tabs)
        self._remember_recent_file(session.source_path)
        self._update_window_title()
        self._update_actions()
        self._update_status()

    def close_document_at(self, index: int) -> bool:
        if not 0 <= index < len(self._documents):
            return False

        document = self._documents[index]
        if document.session.is_modified:
            result = QMessageBox.question(
                self,
                "未保存の変更",
                f"{document.session.source_path.name} には未保存の変更があります。閉じますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                return False

        widget = self._tabs.widget(index)
        self._tabs.removeTab(index)
        self._documents.pop(index)
        if widget is not None:
            if isinstance(widget, PdfView):
                widget.close_document()
            widget.deleteLater()
        if not self._documents:
            self._stack.setCurrentWidget(self._empty_state)

        self._update_window_title()
        self._update_actions()
        self._update_status()
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
            available_width = max(480, self.width() - 24)
            self._search_toolbar.setMinimumWidth(available_width)
            self._search_toolbar.resize(available_width, self._search_toolbar.sizeHint().height())
            self._search_toolbar.show()
            self._search_toolbar.raise_()
        self._search_bar.show()
        self._search_bar.setMinimumWidth(max(420, self.width() - 48))
        self._search_bar.cancel_pending_search()
        self._search_bar.set_state(self._search_state_for(document))
        self._search_bar.focus_search()
        if self._search_toolbar is not None:
            self._search_toolbar.adjustSize()
        self._search_bar.adjustSize()
        self._search_bar.search_input.adjustSize()
        QApplication.processEvents()
        return self._search_ui_is_ready()

    def _prompt_search(self) -> None:
        self.open_search_bar()

    def _next_match(self) -> None:
        document = self._current_document()
        if document is None:
            return
        if not document.view.next_match():
            self.statusBar().showMessage("検索結果がありません", 5000)

    def _previous_match(self) -> None:
        document = self._current_document()
        if document is None:
            return
        if not document.view.previous_match():
            self.statusBar().showMessage("検索結果がありません", 5000)

    def _copy_selection(self) -> None:
        focus_widget = QApplication.focusWidget()
        if isinstance(focus_widget, QLineEdit) and focus_widget.hasSelectedText():
            focus_widget.copy()
            return
        document = self._current_document()
        if document is None:
            return
        if not document.view.copy_selected_text():
            self.statusBar().showMessage("コピーするテキストが選択されていません", 5000)

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
            self.statusBar().showMessage("準備完了")
            self._toolbar_widget.setState(ToolbarState(False, 0, 0, 1.0))
            return
        page_count = document.view.page_count
        current_page = 0 if page_count == 0 else document.view.page_index + 1
        self.statusBar().showMessage(
            f"{document.session.source_path.name}  "
            f"{current_page} / {page_count} ページ  "
            f"ズーム {document.session.zoom_factor:.0%}"
        )
        self._sync_toolbar(document)

    def closeEvent(self, event: QCloseEvent) -> None:
        for index in range(len(self._documents) - 1, -1, -1):
            if not self.close_document_at(index):
                event.ignore()
                return
        shutdown_succeeded = self._render_service.shutdown()
        if not shutdown_succeeded:
            self.statusBar().showMessage("PDFレンダラーの終了を待ち切れませんでした", 5000)
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
        return f"{document.session.source_path.name}{suffix}"

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

    def _on_current_tab_changed(self, _index: int) -> None:
        document = self._current_document()
        if document is not None and self._is_search_open():
            self._search_bar.cancel_pending_search()
            self._sync_search_bar(document)
        self._update_window_title()
        self._update_actions()
        self._update_status()

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

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            isinstance(watched, QWidget)
            and isinstance(event, QKeyEvent)
            and event.type() in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress)
            and self._is_find_shortcut_event(event)
        ):
            focus_widget = QApplication.focusWidget()
            if (
                focus_widget is not None
                and focus_widget is not self._search_bar.search_input
                and isinstance(focus_widget, QLineEdit)
            ):
                return super().eventFilter(watched, event)
            if self._current_document() is not None and self.isAncestorOf(watched):
                self.find_action.trigger()
                return True
        return super().eventFilter(watched, event)

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
            QApplication.platformName() != "offscreen"
            and not self._search_bar.search_input.hasFocus()
        ):
            return False
        if (
            self._search_toolbar.geometry().width() <= 0
            or self._search_toolbar.geometry().height() <= 0
        ):
            return False
        if self._search_bar.geometry().width() <= 0 or self._search_bar.geometry().height() <= 0:
            return False
        return not (
            self._search_bar.search_input.geometry().width() <= 0
            or self._search_bar.search_input.geometry().height() <= 0
        )

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
            text = "検索結果 0 件"
            if state.failed_pages:
                text += f"\uff08{state.failed_pages}ページ失敗\uff09"
            return text
        if state.failed_pages:
            return f"索引完了\uff08{state.failed_pages}ページ失敗\uff09"
        if state.query:
            return f"検索結果 {state.total_count} 件"
        text = "索引完了"
        if state.failed_pages:
            text += f"\uff08{state.failed_pages}ページ失敗\uff09"
        return text

    def _report_error(self, title: str, message: str) -> None:
        self.statusBar().showMessage(message, 5000)
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
