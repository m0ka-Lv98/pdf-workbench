from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from functools import partial
from pathlib import Path
from threading import Event

from PySide6.QtCore import (
    QEvent,
    QEventLoop,
    QMimeData,
    QObject,
    QPoint,
    QRect,
    QSettings,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
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
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
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
from pdf_workbench.domain.image_to_pdf import ImageSourceRevision, ImageToPdfPlan
from pdf_workbench.domain.page_commands import (
    CropPagesCommand,
    DeletePagesCommand,
    DuplicatePagesCommand,
    InsertPagesCommand,
    ReorderPagesCommand,
    ReplacePagesCommand,
    RotatePagesCommand,
)
from pdf_workbench.domain.page_crop import build_page_crop_plan
from pdf_workbench.domain.page_extraction import (
    PageExtractionPlan,
    build_selected_page_extraction_plan,
)
from pdf_workbench.domain.page_insertion import build_page_insertion_plan
from pdf_workbench.domain.page_reorder import (
    PageReorderNoOpError,
    PageReorderPlan,
    build_page_reorder_plan,
)
from pdf_workbench.domain.page_replacement import build_page_replacement_plan
from pdf_workbench.domain.page_split import PageSplitPlan
from pdf_workbench.domain.pdf_merge import PdfMergeBookmarkPolicy, PdfMergePlan
from pdf_workbench.services.image_to_pdf import (
    ImageToPdfCancelled,
    ImageToPdfProgress,
    ImageToPdfResult,
    ImageToPdfService,
)
from pdf_workbench.services.pdf_merge import (
    PdfMergeCancelled,
    PdfMergeProgress,
    PdfMergeResult,
    PdfMergeService,
)
from pdf_workbench.services.pdf_page_export import (
    PdfPageExportError,
    PdfPageExportService,
)
from pdf_workbench.services.pdf_page_mutation import PdfPageMutationService, SourcePdfRevision
from pdf_workbench.services.pdf_page_split import (
    PageSplitBatchResult,
    PageSplitOutputStatus,
    PageSplitProgress,
    PdfPageSplitService,
)
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
from pdf_workbench.services.source_change_monitor import SourceChangeMonitor, SourceCheckResult
from pdf_workbench.ui.icon_provider import IconName, IconProvider, IconTone
from pdf_workbench.ui.pdf_view import PdfView, PdfViewMutationSnapshot
from pdf_workbench.ui.widgets.crop_pages_dialog import (
    CropPagesDialog,
    CropPagesDialogResult,
)
from pdf_workbench.ui.widgets.document_toolbar import DocumentToolbar, ToolbarState
from pdf_workbench.ui.widgets.empty_state import EmptyState
from pdf_workbench.ui.widgets.extract_pages_dialog import (
    ExtractPagesDialog,
    ExtractPagesDialogResult,
)
from pdf_workbench.ui.widgets.image_to_pdf_dialog import (
    ImageToPdfDialog,
    ImageToPdfDialogResult,
)
from pdf_workbench.ui.widgets.insert_pages_dialog import (
    InsertPagesDialog,
    InsertPagesDialogResult,
)
from pdf_workbench.ui.widgets.merge_pdfs_dialog import MergePdfsDialog, MergePdfsDialogResult
from pdf_workbench.ui.widgets.replace_pages_dialog import (
    ReplacePagesDialog,
    ReplacePagesDialogResult,
)
from pdf_workbench.ui.widgets.search_bar import SearchBar, SearchBarState
from pdf_workbench.ui.widgets.source_change_banner import (
    SourceChangeBanner,
    SourceChangeBannerState,
)
from pdf_workbench.ui.widgets.split_pdf_dialog import SplitPdfDialog, SplitPdfDialogResult

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DocumentTab:
    session: DocumentSession
    view: PdfView
    command_history: CommandHistory
    mutation_in_progress: bool = False
    mutation_operation_id: int = 0


class RestoreSessionResult(StrEnum):
    ATTACHED = "attached"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class SaveIntent(StrEnum):
    SAVE = "save"
    SAVE_AS = "save_as"


@dataclass(frozen=True, slots=True)
class InsertPagesDocumentContext:
    session_id: str
    working_copy_path: Path
    source_path: Path
    page_count: int


@dataclass(frozen=True, slots=True)
class ReplacePagesDocumentContext:
    session_id: str
    working_copy_path: Path
    source_path: Path
    page_count: int
    selected_page_indexes: tuple[int, ...]
    current_page_index: int


@dataclass(frozen=True, slots=True)
class CropPagesDocumentContext:
    session_id: str
    working_copy_path: Path
    source_path: Path
    page_count: int
    selected_page_indexes: tuple[int, ...]
    current_page_index: int


@dataclass(frozen=True, slots=True)
class ExtractPagesDocumentContext:
    session_id: str
    working_copy_path: Path
    source_path: Path
    page_count: int
    selected_page_indexes: tuple[int, ...]
    current_page_index: int
    source_revision: SourcePdfRevision


@dataclass(frozen=True, slots=True)
class SplitPdfDocumentContext:
    session_id: str
    working_copy_path: Path
    source_path: Path
    page_count: int
    source_revision: SourcePdfRevision


class SplitPdfWorker(QObject):
    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        service: PdfPageSplitService,
        source_path: Path,
        output_directory: Path,
        plan: PageSplitPlan,
        working_copy_path: Path,
        expected_source_revision: SourcePdfRevision,
        overwrite: bool,
        is_managed_path: Callable[[Path], bool],
        cancel_event: Event,
    ) -> None:
        super().__init__()
        self._service = service
        self._source_path = source_path
        self._output_directory = output_directory
        self._plan = plan
        self._working_copy_path = working_copy_path
        self._expected_source_revision = expected_source_revision
        self._overwrite = overwrite
        self._is_managed_path = is_managed_path
        self._cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.split_pdf(
                self._source_path,
                self._output_directory,
                self._plan,
                working_copy_path=self._working_copy_path,
                expected_source_revision=self._expected_source_revision,
                overwrite=self._overwrite,
                is_managed_path=self._is_managed_path,
                should_cancel=self._cancel_event.is_set,
                progress_callback=self.progress.emit,
            )
        except Exception as exc:
            logger.exception("Failed to split PDF")
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)
        finally:
            self.finished.emit()


class MergePdfWorker(QObject):
    progress = Signal(object)
    succeeded = Signal(object)
    cancelled = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        service: PdfMergeService,
        plan: PdfMergePlan,
        overwrite: bool,
        expected_source_revisions: dict[Path, SourcePdfRevision],
        expected_target_snapshot: TargetSnapshot,
        is_managed_path: Callable[[Path], bool],
        cancel_event: Event,
    ) -> None:
        super().__init__()
        self._service = service
        self._plan = plan
        self._overwrite = overwrite
        self._expected_source_revisions = expected_source_revisions
        self._expected_target_snapshot = expected_target_snapshot
        self._is_managed_path = is_managed_path
        self._cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.merge_pdfs(
                self._plan,
                overwrite=self._overwrite,
                expected_source_revisions=self._expected_source_revisions,
                expected_target_snapshot=self._expected_target_snapshot,
                is_managed_path=self._is_managed_path,
                should_cancel=self._cancel_event.is_set,
                progress_callback=self.progress.emit,
            )
        except PdfMergeCancelled as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            logger.exception("Failed to merge PDFs")
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)
        finally:
            self.finished.emit()


class ImageToPdfWorker(QObject):
    progress = Signal(object)
    succeeded = Signal(object)
    cancelled = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        service: ImageToPdfService,
        plan: ImageToPdfPlan,
        overwrite: bool,
        expected_source_revisions: dict[Path, ImageSourceRevision],
        expected_target_snapshot: TargetSnapshot,
        is_managed_path: Callable[[Path], bool],
        cancel_event: Event,
    ) -> None:
        super().__init__()
        self._service = service
        self._plan = plan
        self._overwrite = overwrite
        self._expected_source_revisions = expected_source_revisions
        self._expected_target_snapshot = expected_target_snapshot
        self._is_managed_path = is_managed_path
        self._cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            result = self._service.create_pdf(
                self._plan,
                overwrite=self._overwrite,
                expected_source_revisions=self._expected_source_revisions,
                expected_target_snapshot=self._expected_target_snapshot,
                is_managed_path=self._is_managed_path,
                should_cancel=self._cancel_event.is_set,
                progress_callback=self.progress.emit,
            )
        except ImageToPdfCancelled as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            logger.exception("Failed to create PDF from images")
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)
        finally:
            self.finished.emit()


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
        page_export_service: PdfPageExportService | None = None,
        page_split_service: PdfPageSplitService | None = None,
        pdf_merge_service: PdfMergeService | None = None,
        image_to_pdf_service: ImageToPdfService | None = None,
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
        self._page_mutation_service = PdfPageMutationService()
        self._page_export_service = (
            page_export_service if page_export_service is not None else PdfPageExportService()
        )
        self._page_split_service = (
            page_split_service if page_split_service is not None else PdfPageSplitService()
        )
        self._pdf_merge_service = (
            pdf_merge_service if pdf_merge_service is not None else PdfMergeService()
        )
        self._image_to_pdf_service = (
            image_to_pdf_service if image_to_pdf_service is not None else ImageToPdfService()
        )
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
        self._split_in_progress_session_id: str | None = None
        self._split_worker_thread: QThread | None = None
        self._split_worker: SplitPdfWorker | None = None
        self._split_progress_dialog: QProgressDialog | None = None
        self._split_cancel_event: Event | None = None
        self._merge_worker_thread: QThread | None = None
        self._merge_worker: MergePdfWorker | None = None
        self._merge_progress_dialog: QProgressDialog | None = None
        self._merge_cancel_event: Event | None = None
        self._image_to_pdf_worker_thread: QThread | None = None
        self._image_to_pdf_worker: ImageToPdfWorker | None = None
        self._image_to_pdf_progress_dialog: QProgressDialog | None = None
        self._image_to_pdf_cancel_event: Event | None = None
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
        self._toolbar_widget.delete_requested.connect(self._delete_selected_pages)
        self._toolbar_widget.duplicate_requested.connect(self._duplicate_selected_pages)
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
        self.undo_action.triggered.connect(self._trigger_undo)

        self.redo_action = QAction("やり直す", self)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.redo_action.triggered.connect(self._trigger_redo)

        self.save_as_action = QAction("名前を付けて保存", self)
        self.save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.save_as_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.save_as_action.triggered.connect(self._save_current_document_as)

        self.delete_pages_action = QAction("選択したページを削除", self)
        self.delete_pages_action.triggered.connect(self._delete_selected_pages)

        self.duplicate_pages_action = QAction("選択したページを複製", self)
        self.duplicate_pages_action.triggered.connect(self._duplicate_selected_pages)

        self.crop_pages_action = QAction("選択ページをトリミング…", self)
        self.crop_pages_action.triggered.connect(self._crop_selected_pages)

        self.extract_selected_pages_action = QAction("選択ページを抽出…", self)
        self.extract_selected_pages_action.triggered.connect(self._extract_selected_pages)

        self.extract_page_range_action = QAction("ページ範囲を抽出…", self)
        self.extract_page_range_action.triggered.connect(self._extract_page_range)

        self.split_pdf_action = QAction("PDFを分割…", self)
        self.split_pdf_action.triggered.connect(self._split_pdf)

        self.merge_pdfs_action = QAction("PDFを結合…", self)
        self.merge_pdfs_action.triggered.connect(self._merge_pdfs)

        self.image_to_pdf_action = QAction("画像からPDFを作成…", self)
        self.image_to_pdf_action.triggered.connect(self._create_pdf_from_images)

        self.insert_pages_action = QAction("別のPDFからページを挿入…", self)
        self.insert_pages_action.triggered.connect(self._insert_pages_from_pdf)

        self.replace_pages_action = QAction("選択ページを別のPDFで置換…", self)
        self.replace_pages_action.triggered.connect(self._replace_selected_pages_from_pdf)

        self.copy_action = QAction("コピー", self)
        self.copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        self.copy_action.triggered.connect(self._copy_selection)

        for action in (
            self.open_action,
            self.undo_action,
            self.redo_action,
            self.crop_pages_action,
            self.delete_pages_action,
            self.duplicate_pages_action,
            self.extract_selected_pages_action,
            self.extract_page_range_action,
            self.split_pdf_action,
            self.merge_pdfs_action,
            self.image_to_pdf_action,
            self.insert_pages_action,
            self.replace_pages_action,
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
        file_menu.addAction(self.split_pdf_action)
        file_menu.addAction(self.merge_pdfs_action)
        file_menu.addAction(self.image_to_pdf_action)
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
        edit_menu.addAction(self.crop_pages_action)
        edit_menu.addAction(self.delete_pages_action)
        edit_menu.addAction(self.duplicate_pages_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.extract_selected_pages_action)
        edit_menu.addAction(self.extract_page_range_action)
        edit_menu.addAction(self.split_pdf_action)
        edit_menu.addAction(self.merge_pdfs_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.insert_pages_action)
        edit_menu.addAction(self.replace_pages_action)
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
        if (
            document.mutation_in_progress
            or self._split_in_progress_session_id == document.session.session_id
        ):
            return False
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
        document.command_history.dispose()
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
        document = self._current_document()
        if document is not None and (
            document.mutation_in_progress
            or self._split_in_progress_session_id == document.session.session_id
        ):
            return False
        return self.close_document_at(self._tabs.currentIndex())

    def _previous_page(self) -> None:
        document = self._current_document()
        if document is None:
            return
        document.view.set_page(document.view.page_index - 1)

    def _next_page(self) -> None:
        document = self._current_document()
        if document is None:
            return
        document.view.set_page(document.view.page_index + 1)

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
        if document is None or document.mutation_in_progress:
            return
        page_indexes = document.view.selected_page_indexes
        if not page_indexes:
            return
        self.execute_document_command(
            RotatePagesCommand(
                document.session.document_path,
                page_indexes,
                self._page_mutation_service,
            )
        )

    def _duplicate_selected_pages(self) -> None:
        document = self._current_document()
        if document is None or document.session.is_saving or document.mutation_in_progress:
            return
        page_indexes = tuple(sorted(set(document.view.selected_page_indexes)))
        if not page_indexes:
            return
        self.execute_document_command(
            DuplicatePagesCommand(
                document.session.document_path,
                page_indexes,
                self._page_mutation_service,
            )
        )

    def _crop_selected_pages(self) -> None:
        document = self._current_document()
        if document is None or document.session.is_saving or document.mutation_in_progress:
            return
        selected_page_indexes = tuple(sorted(set(document.view.selected_page_indexes)))
        if not selected_page_indexes:
            return
        context = self._capture_crop_pages_context(document)
        try:
            expected_target_snapshot = self._page_mutation_service.snapshot_document_structure(
                document.session.document_path,
            )
            crop_states = self._page_mutation_service.read_crop_states(
                document.session.document_path,
                selected_page_indexes,
            )
        except Exception as exc:
            logger.exception("Failed to prepare CropBox edit")
            self._report_error("ページのトリミングに失敗しました", str(exc))
            return
        result = self._choose_crop_pages_options(
            document,
            selected_page_count=len(selected_page_indexes),
        )
        if result is None:
            return
        target_document = self._resolve_crop_pages_document(context)
        if target_document is None:
            return
        try:
            plan = build_page_crop_plan(
                crop_states,
                margins=result.margins,
                reset_to_media_box=result.reset_to_media_box,
            )
        except (TypeError, ValueError) as exc:
            self._report_error("ページのトリミングに失敗しました", str(exc))
            return
        self.execute_document_command(
            CropPagesCommand(
                target_document.session.document_path,
                plan,
                self._page_mutation_service,
                current_page_index_before=target_document.view.page_index,
                selected_page_indexes_before=target_document.view.selected_page_indexes,
                expected_target_snapshot=expected_target_snapshot,
            )
        )

    def _extract_selected_pages(self) -> None:
        document = self._current_document()
        if document is None or document.session.is_saving or document.mutation_in_progress:
            return
        if not document.view.selected_page_indexes:
            return
        context = self._capture_extract_pages_context(document)
        if context is None:
            return
        result = ExtractPagesDialogResult(
            plan=self._selected_extraction_plan(context),
            mode="selection",
        )
        self._run_page_extraction(context, result)

    def _extract_page_range(self) -> None:
        document = self._current_document()
        if document is None or document.session.is_saving or document.mutation_in_progress:
            return
        context = self._capture_extract_pages_context(document)
        if context is None:
            return
        result = self._choose_extract_pages_options(document, default_mode="range")
        if result is None:
            return
        self._run_page_extraction(context, result)

    def _run_page_extraction(
        self,
        context: ExtractPagesDocumentContext,
        result: ExtractPagesDialogResult,
    ) -> None:
        target_document = self._resolve_extract_pages_document(
            context,
            require_selection_match=result.mode == "selection",
        )
        if target_document is None:
            return
        target_path = self._choose_extract_target_path(target_document, result.plan)
        if target_path is None:
            return
        if target_path == context.source_path:
            self._report_error(
                "ページ抽出に失敗しました",
                "抽出元PDFと同じ場所には出力できません。別の保存先を選択してください。",
            )
            return
        if self._workspace_manager.contains_managed_path(target_path):
            self._report_error(
                "ページ抽出に失敗しました",
                "アプリの一時作業フォルダ内には出力できません。別の保存先を選択してください。",
            )
            return
        target_document = self._resolve_extract_pages_document(
            context,
            require_selection_match=result.mode == "selection",
        )
        if target_document is None:
            return
        try:
            target_snapshot = TargetSnapshot.capture(target_path)
        except OSError as exc:
            self._report_error("ページ抽出に失敗しました", str(exc))
            return
        session = target_document.session
        session.is_saving = True
        self._update_actions()
        self._set_status_message("ページを抽出しています…")
        try:
            export_result = self._page_export_service.extract_pages(
                session.document_path,
                target_path,
                result.plan,
                working_copy_path=session.document_path,
                expected_source_revision=context.source_revision,
                expected_target_snapshot=target_snapshot,
            )
        except (PdfPageExportError, TargetChangedError) as exc:
            logger.exception("Failed to extract pages")
            self._report_error(
                "ページ抽出に失敗しました",
                f"{exc}\n\n元のPDFと現在の作業コピーは変更されていません。",
            )
            return
        finally:
            session.is_saving = False
            self._update_actions()
            self._update_status()
        self._set_status_message(
            f"{export_result.exported_page_count}ページを抽出しました: "
            f"{export_result.target_path.name}",
            timeout_ms=5000,
        )

    def _split_pdf(self) -> None:
        document = self._current_document()
        if (
            document is None
            or document.session.is_saving
            or document.mutation_in_progress
            or self._background_pdf_operation_in_progress()
            or document.view.page_count < 2
        ):
            return
        context = self._capture_split_pdf_context(document)
        if context is None:
            return
        result = self._choose_split_pdf_options(document)
        if result is None:
            return
        target_document = self._resolve_split_pdf_document(context)
        if target_document is None:
            return
        self._start_split_worker(context, result)

    def _start_split_worker(
        self,
        context: SplitPdfDocumentContext,
        result: SplitPdfDialogResult,
    ) -> None:
        progress_dialog = QProgressDialog(
            "PDFを分割しています…",
            "キャンセル",
            0,
            result.plan.output_count,
            self,
        )
        progress_dialog.setWindowTitle("PDFを分割")
        progress_dialog.setMinimumDuration(0)
        progress_dialog.setValue(0)
        cancel_event = Event()
        thread = QThread(self)
        worker = SplitPdfWorker(
            service=self._page_split_service,
            source_path=context.working_copy_path,
            output_directory=result.output_directory,
            plan=result.plan,
            working_copy_path=context.working_copy_path,
            expected_source_revision=context.source_revision,
            overwrite=result.overwrite,
            is_managed_path=self._workspace_manager.contains_managed_path,
            cancel_event=cancel_event,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_split_progress)
        worker.succeeded.connect(self._on_split_succeeded)
        worker.failed.connect(self._on_split_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        progress_dialog.canceled.connect(self._request_split_cancel)
        thread.finished.connect(self._on_split_thread_finished)
        self._split_in_progress_session_id = context.session_id
        self._split_worker_thread = thread
        self._split_worker = worker
        self._split_progress_dialog = progress_dialog
        self._split_cancel_event = cancel_event
        self._set_status_message("PDFを分割しています…")
        self._update_actions()
        thread.start()

    @Slot()
    def _request_split_cancel(self) -> None:
        if self._split_cancel_event is None:
            return
        self._split_cancel_event.set()
        if self._split_progress_dialog is not None:
            self._split_progress_dialog.setLabelText("現在の出力完了後にキャンセルします…")
        self._set_status_message("PDF分割をキャンセルしています…")

    def _on_split_progress(self, progress: object) -> None:
        if not isinstance(progress, PageSplitProgress):
            return
        if self._split_progress_dialog is not None:
            self._split_progress_dialog.setLabelText(
                f"{progress.output_number} / {progress.output_count}: "
                f"{progress.chunk.display_range} -> {progress.target_path.name}"
            )
            if progress.status is not PageSplitOutputStatus.SKIPPED:
                self._split_progress_dialog.setValue(progress.output_number)
        self._set_status_message(
            f"PDFを分割しています… {progress.output_number} / {progress.output_count}"
        )

    def _on_split_succeeded(self, result: object) -> None:
        if not isinstance(result, PageSplitBatchResult):
            return
        if self._split_progress_dialog is not None:
            self._split_progress_dialog.setValue(len(result.outputs))
        self._show_split_result_summary(result)
        if result.failure_count or result.skipped_count or result.cancelled_count:
            self._set_status_message("PDF分割が一部完了しました", timeout_ms=5000)
        else:
            self._set_status_message(
                f"{result.success_count}個のPDFを作成しました",
                timeout_ms=5000,
            )

    def _on_split_failed(self, message: str) -> None:
        self._report_error(
            "PDF分割に失敗しました",
            f"{message}\n\n元のPDFと現在の作業コピーは変更されていません。",
        )

    def _on_split_thread_finished(self) -> None:
        if self._split_progress_dialog is not None:
            self._split_progress_dialog.close()
            self._split_progress_dialog.deleteLater()
        self._split_progress_dialog = None
        self._split_worker = None
        self._split_worker_thread = None
        self._split_cancel_event = None
        self._split_in_progress_session_id = None
        self._update_actions()
        self._update_status()

    def _show_split_result_summary(self, result: PageSplitBatchResult) -> None:
        if result.failure_count:
            headline = (
                f"{len(result.outputs)}個中{result.success_count}個を作成しました。"
                f"{result.failure_count}個は失敗しました"
            )
        elif result.cancelled_count:
            headline = (
                f"{len(result.outputs)}個中{result.success_count}個を作成し、"
                "残りをキャンセルしました"
            )
        else:
            headline = f"{result.success_count}個のPDFを作成しました"
        lines = [
            headline,
            "",
            f"出力先: {result.output_directory}",
            "",
            f"成功: {result.success_count}",
            f"失敗: {result.failure_count}",
            f"スキップ: {result.skipped_count}",
            f"キャンセル: {result.cancelled_count}",
            "",
        ]
        for output in result.outputs:
            status_text = {
                PageSplitOutputStatus.SUCCESS: "成功",
                PageSplitOutputStatus.FAILED: "失敗",
                PageSplitOutputStatus.SKIPPED: "スキップ",
                PageSplitOutputStatus.CANCELLED: "キャンセル",
            }[output.status]
            suffix = f" - {output.error_message}" if output.error_message else ""
            lines.append(
                f"{output.chunk.display_range}: {output.target_path.name} [{status_text}]{suffix}"
            )
        message = QMessageBox(self)
        message.setWindowTitle("PDF分割結果")
        message.setIcon(QMessageBox.Icon.Information)
        message.setText(headline)
        message.setDetailedText("\n".join(lines))
        message.exec()

    def _merge_pdfs(self) -> None:
        if self._background_pdf_operation_in_progress():
            return
        dialog = MergePdfsDialog(
            input_reader=self._pdf_merge_service.inspect_merge_input,
            target_snapshot_reader=TargetSnapshot.capture,
            is_managed_path=self._workspace_manager.contains_managed_path,
            default_output_directory=Path.home(),
            parent=self,
        )
        if dialog.exec() != int(QDialog.DialogCode.Accepted) or dialog.dialog_result is None:
            return
        self._start_merge_worker(dialog.dialog_result)

    def _start_merge_worker(self, result: MergePdfsDialogResult) -> None:
        progress_dialog = QProgressDialog(
            "PDFを結合しています…",
            "キャンセル",
            0,
            result.plan.total_page_count,
            self,
        )
        progress_dialog.setWindowTitle("PDFを結合")
        progress_dialog.setMinimumDuration(0)
        progress_dialog.setValue(0)
        cancel_event = Event()
        thread = QThread(self)
        worker = MergePdfWorker(
            service=self._pdf_merge_service,
            plan=result.plan,
            overwrite=result.overwrite,
            expected_source_revisions=result.expected_source_revisions,
            expected_target_snapshot=result.expected_target_snapshot,
            is_managed_path=self._workspace_manager.contains_managed_path,
            cancel_event=cancel_event,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_merge_progress)
        worker.succeeded.connect(self._on_merge_succeeded)
        worker.cancelled.connect(self._on_merge_cancelled)
        worker.failed.connect(self._on_merge_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        progress_dialog.canceled.connect(self._request_merge_cancel)
        thread.finished.connect(self._on_merge_thread_finished)
        self._merge_worker_thread = thread
        self._merge_worker = worker
        self._merge_progress_dialog = progress_dialog
        self._merge_cancel_event = cancel_event
        self._set_status_message("PDFを結合しています…")
        self._update_actions()
        thread.start()

    @Slot()
    def _request_merge_cancel(self) -> None:
        if self._merge_cancel_event is None:
            return
        self._merge_cancel_event.set()
        if self._merge_progress_dialog is not None:
            self._merge_progress_dialog.setLabelText("安全な中断点でキャンセルします…")
        self._set_status_message("PDF結合をキャンセルしています…")

    def _on_merge_progress(self, progress: object) -> None:
        if not isinstance(progress, PdfMergeProgress):
            return
        if self._merge_progress_dialog is not None:
            self._merge_progress_dialog.setLabelText(
                f"{progress.input_number} / {progress.input_count}: "
                f"{progress.filename} ({progress.stage.value})"
            )
            self._merge_progress_dialog.setValue(progress.completed_output_pages)
        self._set_status_message(
            f"PDFを結合しています… {progress.completed_output_pages} / "
            f"{progress.total_output_pages}"
        )

    def _on_merge_succeeded(self, result: object) -> None:
        if not isinstance(result, PdfMergeResult):
            return
        if self._merge_progress_dialog is not None:
            self._merge_progress_dialog.setValue(result.total_page_count)
        self._show_merge_result_summary(result)
        self._set_status_message(
            f"{result.input_count}個のPDFを結合しました: {result.target_path.name}",
            timeout_ms=5000,
        )

    def _on_merge_cancelled(self, message: str) -> None:
        self._report_error(
            "PDF結合をキャンセルしました",
            f"{message}\n\n入力PDFと出力先PDFは変更されていません。",
        )

    def _on_merge_failed(self, message: str) -> None:
        self._report_error(
            "PDF結合に失敗しました",
            f"{message}\n\n入力PDFと出力先PDFは変更されていません。",
        )

    def _on_merge_thread_finished(self) -> None:
        if self._merge_progress_dialog is not None:
            self._merge_progress_dialog.close()
            self._merge_progress_dialog.deleteLater()
        self._merge_progress_dialog = None
        self._merge_worker = None
        self._merge_worker_thread = None
        self._merge_cancel_event = None
        self._update_actions()
        self._update_status()

    def _show_merge_result_summary(self, result: PdfMergeResult) -> None:
        headline = f"{result.input_count}個のPDFを結合しました"
        metadata_text = result.metadata_source_label or "引き継がない"
        bookmarks_text = (
            "入力PDFごとに保持"
            if result.bookmark_policy is PdfMergeBookmarkPolicy.GROUPED_BY_SOURCE
            else "含めない"
        )
        lines = [
            headline,
            "",
            f"出力先: {result.target_path}",
            f"入力ファイル: {result.input_count}",
            f"合計ページ: {result.total_page_count}",
            f"Metadata: {metadata_text}",
            f"Bookmarks: {bookmarks_text}",
            "",
        ]
        for index, item in enumerate(result.inputs, start=1):
            lines.append(
                f"{index}. {item.label} — {item.page_count}ページ — 出力 {item.display_range}"
            )
        message = QMessageBox(self)
        message.setWindowTitle("PDF結合結果")
        message.setIcon(QMessageBox.Icon.Information)
        message.setText(headline)
        message.setDetailedText("\n".join(lines))
        message.exec()

    def _create_pdf_from_images(self) -> None:
        if self._background_pdf_operation_in_progress():
            return
        dialog = ImageToPdfDialog(
            input_reader=self._image_to_pdf_service.inspect_image_input,
            target_snapshot_reader=TargetSnapshot.capture,
            is_managed_path=self._workspace_manager.contains_managed_path,
            default_output_directory=Path.home(),
            parent=self,
        )
        if dialog.exec() != int(QDialog.DialogCode.Accepted) or dialog.dialog_result is None:
            return
        self._start_image_to_pdf_worker(dialog.dialog_result)

    def _start_image_to_pdf_worker(self, result: ImageToPdfDialogResult) -> None:
        progress_dialog = QProgressDialog(
            "画像からPDFを作成しています…",
            "キャンセル",
            0,
            result.plan.total_page_count,
            self,
        )
        progress_dialog.setWindowTitle("画像からPDFを作成")
        progress_dialog.setMinimumDuration(0)
        progress_dialog.setValue(0)
        cancel_event = Event()
        thread = QThread(self)
        worker = ImageToPdfWorker(
            service=self._image_to_pdf_service,
            plan=result.plan,
            overwrite=result.overwrite,
            expected_source_revisions=result.expected_source_revisions,
            expected_target_snapshot=result.expected_target_snapshot,
            is_managed_path=self._workspace_manager.contains_managed_path,
            cancel_event=cancel_event,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_image_to_pdf_progress)
        worker.succeeded.connect(self._on_image_to_pdf_succeeded)
        worker.cancelled.connect(self._on_image_to_pdf_cancelled)
        worker.failed.connect(self._on_image_to_pdf_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        progress_dialog.canceled.connect(self._request_image_to_pdf_cancel)
        thread.finished.connect(self._on_image_to_pdf_thread_finished)
        self._image_to_pdf_worker_thread = thread
        self._image_to_pdf_worker = worker
        self._image_to_pdf_progress_dialog = progress_dialog
        self._image_to_pdf_cancel_event = cancel_event
        self._set_status_message("画像からPDFを作成しています…")
        self._update_actions()
        thread.start()

    @Slot()
    def _request_image_to_pdf_cancel(self) -> None:
        if self._image_to_pdf_cancel_event is None:
            return
        self._image_to_pdf_cancel_event.set()
        if self._image_to_pdf_progress_dialog is not None:
            self._image_to_pdf_progress_dialog.setLabelText("安全な中断点でキャンセルします…")
        self._set_status_message("画像PDF作成をキャンセルしています…")

    def _on_image_to_pdf_progress(self, progress: object) -> None:
        if not isinstance(progress, ImageToPdfProgress):
            return
        if self._image_to_pdf_progress_dialog is not None:
            self._image_to_pdf_progress_dialog.setLabelText(
                f"{progress.input_number} / {progress.input_count}: "
                f"{progress.filename}\n"
                f"フレーム {progress.frame_number} / {progress.frame_count}"
            )
            self._image_to_pdf_progress_dialog.setValue(progress.completed_pages)
        self._set_status_message(
            f"画像PDFを作成しています… {progress.completed_pages} / {progress.total_pages}"
        )

    def _on_image_to_pdf_succeeded(self, result: object) -> None:
        if not isinstance(result, ImageToPdfResult):
            return
        if self._image_to_pdf_progress_dialog is not None:
            self._image_to_pdf_progress_dialog.setValue(result.total_page_count)
        self._show_image_to_pdf_result_summary(result)
        self._set_status_message(
            f"{result.input_count}個の画像からPDFを作成しました: {result.target_path.name}",
            timeout_ms=5000,
        )

    def _on_image_to_pdf_cancelled(self, message: str) -> None:
        self._report_error(
            "画像PDF作成をキャンセルしました",
            f"{message}\n\n入力画像と出力先PDFは変更されていません。",
        )

    def _on_image_to_pdf_failed(self, message: str) -> None:
        self._report_error(
            "画像PDF作成に失敗しました",
            f"{message}\n\n入力画像と出力先PDFは変更されていません。",
        )

    def _on_image_to_pdf_thread_finished(self) -> None:
        if self._image_to_pdf_progress_dialog is not None:
            self._image_to_pdf_progress_dialog.close()
            self._image_to_pdf_progress_dialog.deleteLater()
        self._image_to_pdf_progress_dialog = None
        self._image_to_pdf_worker = None
        self._image_to_pdf_worker_thread = None
        self._image_to_pdf_cancel_event = None
        self._update_actions()
        self._update_status()

    def _show_image_to_pdf_result_summary(self, result: ImageToPdfResult) -> None:
        headline = (
            f"{result.input_count}個の画像から{result.total_page_count}ページのPDFを作成しました"
        )
        lines = [
            headline,
            "",
            f"出力先: {result.target_path}",
            f"入力ファイル: {result.input_count}",
            f"出力ページ: {result.total_page_count}",
            f"Page size: {result.page_size_mode.value}",
            f"Scaling: {result.scaling_mode.value}",
            f"Transparency: {result.transparency_policy.value}",
            "",
        ]
        for index, item in enumerate(result.inputs, start=1):
            lines.append(
                f"{index}. {item.label} — {item.frame_count}ページ — 出力 {item.display_range}"
            )
        message = QMessageBox(self)
        message.setWindowTitle("画像PDF作成結果")
        message.setIcon(QMessageBox.Icon.Information)
        message.setText(headline)
        message.setDetailedText("\n".join(lines))
        message.exec()

    def _delete_selected_pages(self) -> None:
        document = self._current_document()
        if document is None or document.session.is_saving or document.mutation_in_progress:
            return
        page_indexes = tuple(sorted(set(document.view.selected_page_indexes)))
        if not page_indexes or len(page_indexes) >= document.view.page_count:
            return
        self.execute_document_command(
            DeletePagesCommand(
                document.session.document_path,
                page_indexes,
                document.view.page_index,
                self._page_mutation_service,
            )
        )

    def _insert_pages_from_pdf(self) -> None:
        document = self._current_document()
        if document is None or document.session.is_saving or document.mutation_in_progress:
            return
        context = self._capture_insert_pages_context(document)
        try:
            expected_target_snapshot = self._page_mutation_service.snapshot_document_structure(
                document.session.document_path,
            )
        except Exception as exc:
            logger.exception("Failed to snapshot target PDF before insertion")
            self._report_error("ページ挿入に失敗しました", str(exc))
            return
        source_path = self._choose_insert_source_path(document)
        if source_path is None:
            return
        try:
            source_page_count = self._page_mutation_service.read_page_count(source_path)
        except Exception as exc:
            logger.exception("Failed to inspect source PDF for insertion: %s", source_path)
            self._report_error("ページ挿入に失敗しました", str(exc))
            return
        if self._resolve_insert_pages_document(context) is None:
            return
        options = self._choose_insert_pages_options(
            document,
            source_path=source_path,
            source_page_count=source_page_count,
        )
        if options is None:
            return
        target_document = self._resolve_insert_pages_document(context)
        if target_document is None:
            return
        try:
            plan = build_page_insertion_plan(
                target_document.view.page_count,
                source_page_count,
                options.page_selection.page_indexes,
                options.insertion_slot,
            )
        except (TypeError, ValueError) as exc:
            self._report_error("ページ挿入に失敗しました", str(exc))
            return
        self.execute_document_command(
            InsertPagesCommand(
                target_document.session.document_path,
                source_path,
                plan,
                self._page_mutation_service,
                current_page_index_before=target_document.view.page_index,
                selected_page_indexes_before=target_document.view.selected_page_indexes,
                expected_target_snapshot=expected_target_snapshot,
            )
        )

    def _replace_selected_pages_from_pdf(self) -> None:
        document = self._current_document()
        if document is None or document.session.is_saving or document.mutation_in_progress:
            return
        selected_page_indexes = tuple(sorted(set(document.view.selected_page_indexes)))
        if not selected_page_indexes:
            return
        context = self._capture_replace_pages_context(document)
        try:
            expected_target_snapshot = self._page_mutation_service.snapshot_document_structure(
                document.session.document_path,
            )
        except Exception as exc:
            logger.exception("Failed to snapshot target PDF before replacement")
            self._report_error("ページ置換に失敗しました", str(exc))
            return
        source_path = self._choose_insert_source_path(document)
        if source_path is None:
            return
        try:
            source_revision = self._page_mutation_service.read_source_pdf_revision(source_path)
        except Exception as exc:
            logger.exception("Failed to inspect source PDF for replacement: %s", source_path)
            self._report_error("ページ置換に失敗しました", str(exc))
            return
        if self._resolve_replace_pages_document(context) is None:
            return
        options = self._choose_replace_pages_options(
            document,
            source_path=source_path,
            source_page_count=source_revision.page_count,
            target_page_indexes=selected_page_indexes,
        )
        if options is None:
            return
        target_document = self._resolve_replace_pages_document(context)
        if target_document is None:
            return
        if not self._confirm_source_revision_still_current(source_revision):
            return
        try:
            plan = build_page_replacement_plan(
                target_document.view.page_count,
                source_revision.page_count,
                selected_page_indexes,
                options.page_selection.page_indexes,
            )
        except (TypeError, ValueError) as exc:
            self._report_error("ページ置換に失敗しました", str(exc))
            return
        self.execute_document_command(
            ReplacePagesCommand(
                target_document.session.document_path,
                source_path,
                plan,
                self._page_mutation_service,
                current_page_index_before=target_document.view.page_index,
                selected_page_indexes_before=target_document.view.selected_page_indexes,
                expected_target_snapshot=expected_target_snapshot,
                expected_source_revision=source_revision,
            )
        )

    def _reorder_selected_pages(
        self,
        plan: object,
    ) -> None:
        document = self._current_document()
        if document is None or document.session.is_saving or document.mutation_in_progress:
            return
        if document.view.page_count <= 1:
            return
        if not isinstance(plan, PageReorderPlan):
            return
        self.execute_document_command(
            ReorderPagesCommand(
                document.session.document_path,
                plan,
                self._page_mutation_service,
            )
        )

    def _capture_insert_pages_context(self, document: DocumentTab) -> InsertPagesDocumentContext:
        return InsertPagesDocumentContext(
            session_id=document.session.session_id,
            working_copy_path=document.session.document_path,
            source_path=document.session.source_path,
            page_count=document.view.page_count,
        )

    def _capture_replace_pages_context(self, document: DocumentTab) -> ReplacePagesDocumentContext:
        return ReplacePagesDocumentContext(
            session_id=document.session.session_id,
            working_copy_path=document.session.document_path,
            source_path=document.session.source_path,
            page_count=document.view.page_count,
            selected_page_indexes=tuple(sorted(set(document.view.selected_page_indexes))),
            current_page_index=document.view.page_index,
        )

    def _capture_crop_pages_context(self, document: DocumentTab) -> CropPagesDocumentContext:
        return CropPagesDocumentContext(
            session_id=document.session.session_id,
            working_copy_path=document.session.document_path,
            source_path=document.session.source_path,
            page_count=document.view.page_count,
            selected_page_indexes=tuple(sorted(set(document.view.selected_page_indexes))),
            current_page_index=document.view.page_index,
        )

    def _capture_extract_pages_context(
        self,
        document: DocumentTab,
    ) -> ExtractPagesDocumentContext | None:
        try:
            source_revision = self._page_export_service.read_source_pdf_revision(
                document.session.document_path
            )
        except Exception as exc:
            logger.exception("Failed to inspect working copy before extraction")
            self._report_error("ページ抽出に失敗しました", str(exc))
            return None
        return ExtractPagesDocumentContext(
            session_id=document.session.session_id,
            working_copy_path=document.session.document_path,
            source_path=document.session.source_path,
            page_count=document.view.page_count,
            selected_page_indexes=tuple(sorted(set(document.view.selected_page_indexes))),
            current_page_index=document.view.page_index,
            source_revision=source_revision,
        )

    def _capture_split_pdf_context(
        self,
        document: DocumentTab,
    ) -> SplitPdfDocumentContext | None:
        try:
            source_revision = self._page_split_service.read_source_pdf_revision(
                document.session.document_path
            )
        except Exception as exc:
            logger.exception("Failed to inspect working copy before split")
            self._report_error("PDF分割に失敗しました", str(exc))
            return None
        return SplitPdfDocumentContext(
            session_id=document.session.session_id,
            working_copy_path=document.session.document_path,
            source_path=document.session.source_path,
            page_count=document.view.page_count,
            source_revision=source_revision,
        )

    def _resolve_insert_pages_document(
        self,
        context: InsertPagesDocumentContext,
    ) -> DocumentTab | None:
        document = self._current_document()
        if document is None:
            return None
        if document.session.session_id != context.session_id:
            return None
        if document.session.document_path != context.working_copy_path:
            return None
        if document.session.source_path != context.source_path:
            return None
        if document.view.page_count != context.page_count:
            return None
        if document.session.is_saving or document.mutation_in_progress:
            return None
        return document

    def _resolve_replace_pages_document(
        self,
        context: ReplacePagesDocumentContext,
    ) -> DocumentTab | None:
        document = self._resolve_insert_pages_document(
            InsertPagesDocumentContext(
                session_id=context.session_id,
                working_copy_path=context.working_copy_path,
                source_path=context.source_path,
                page_count=context.page_count,
            )
        )
        if document is None:
            return None
        if tuple(sorted(set(document.view.selected_page_indexes))) != context.selected_page_indexes:
            return None
        if document.view.page_index != context.current_page_index:
            return None
        return document

    def _resolve_crop_pages_document(
        self,
        context: CropPagesDocumentContext,
    ) -> DocumentTab | None:
        document = self._current_document()
        if document is None:
            return None
        if document.session.session_id != context.session_id:
            return None
        if document.session.document_path != context.working_copy_path:
            return None
        if document.session.source_path != context.source_path:
            return None
        if document.view.page_count != context.page_count:
            return None
        if document.session.is_saving or document.mutation_in_progress:
            return None
        if tuple(sorted(set(document.view.selected_page_indexes))) != context.selected_page_indexes:
            return None
        if document.view.page_index != context.current_page_index:
            return None
        return document

    def _resolve_extract_pages_document(
        self,
        context: ExtractPagesDocumentContext,
        *,
        require_selection_match: bool,
    ) -> DocumentTab | None:
        document = self._resolve_insert_pages_document(
            InsertPagesDocumentContext(
                session_id=context.session_id,
                working_copy_path=context.working_copy_path,
                source_path=context.source_path,
                page_count=context.page_count,
            )
        )
        if document is None:
            return None
        if document.view.page_index != context.current_page_index:
            return None
        if require_selection_match and (
            tuple(sorted(set(document.view.selected_page_indexes))) != context.selected_page_indexes
        ):
            return None
        try:
            current_revision = self._page_export_service.read_source_pdf_revision(
                document.session.document_path
            )
        except Exception as exc:
            logger.exception("Failed to revalidate working copy before extraction")
            self._report_error("ページ抽出に失敗しました", str(exc))
            return None
        if current_revision != context.source_revision:
            self._report_error("ページ抽出に失敗しました", "抽出元PDFが変更されました")
            return None
        return document

    def _resolve_split_pdf_document(
        self,
        context: SplitPdfDocumentContext,
    ) -> DocumentTab | None:
        document = self._resolve_insert_pages_document(
            InsertPagesDocumentContext(
                session_id=context.session_id,
                working_copy_path=context.working_copy_path,
                source_path=context.source_path,
                page_count=context.page_count,
            )
        )
        if document is None:
            return None
        if self._split_in_progress_session_id is not None:
            return None
        try:
            current_revision = self._page_split_service.read_source_pdf_revision(
                document.session.document_path
            )
        except Exception as exc:
            logger.exception("Failed to revalidate working copy before split")
            self._report_error("PDF分割に失敗しました", str(exc))
            return None
        if current_revision != context.source_revision:
            self._report_error("PDF分割に失敗しました", "分割元PDFが変更されました")
            return None
        return document

    @staticmethod
    def _selected_extraction_plan(context: ExtractPagesDocumentContext) -> PageExtractionPlan:
        return build_selected_page_extraction_plan(
            context.page_count,
            context.selected_page_indexes,
        )

    def _choose_insert_source_path(self, document: DocumentTab) -> Path | None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "挿入元PDFを選択",
            str(document.session.source_path.parent),
            "PDF files (*.pdf)",
        )
        if not filename:
            return None
        return Path(filename).expanduser().resolve()

    def _choose_insert_pages_options(
        self,
        document: DocumentTab,
        *,
        source_path: Path,
        source_page_count: int,
    ) -> InsertPagesDialogResult | None:
        insertion_targets, default_index = self._insertion_targets_for_document(document)
        dialog = InsertPagesDialog(
            source_path,
            source_page_count,
            insertion_targets,
            default_index=default_index,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return None
        return dialog.dialog_result

    def _choose_replace_pages_options(
        self,
        document: DocumentTab,
        *,
        source_path: Path,
        source_page_count: int,
        target_page_indexes: tuple[int, ...],
    ) -> ReplacePagesDialogResult | None:
        dialog = ReplacePagesDialog(
            source_path,
            source_page_count,
            tuple(page_index + 1 for page_index in target_page_indexes),
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return None
        return dialog.dialog_result

    def _choose_crop_pages_options(
        self,
        document: DocumentTab,
        *,
        selected_page_count: int,
    ) -> CropPagesDialogResult | None:
        dialog = CropPagesDialog(
            selected_page_count=selected_page_count,
            parent=document.view,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return None
        return dialog.dialog_result

    def _choose_extract_pages_options(
        self,
        document: DocumentTab,
        *,
        default_mode: str,
    ) -> ExtractPagesDialogResult | None:
        dialog = ExtractPagesDialog(
            page_count=document.view.page_count,
            selected_page_indexes=tuple(sorted(set(document.view.selected_page_indexes))),
            default_mode=default_mode,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return None
        return dialog.dialog_result

    def _choose_extract_target_path(
        self,
        document: DocumentTab,
        plan: PageExtractionPlan,
    ) -> Path | None:
        stem = document.session.display_path.stem
        initial_path = document.session.display_path.with_name(
            f"{stem}-extract-{plan.output_page_count}p.pdf"
        )
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "抽出PDFの保存先",
            str(initial_path),
            "PDF files (*.pdf)",
        )
        if not filename:
            return None
        target_path = Path(filename).expanduser().resolve()
        if target_path.suffix.lower() != ".pdf":
            target_path = target_path.with_suffix(".pdf")
        return target_path

    def _choose_split_pdf_options(self, document: DocumentTab) -> SplitPdfDialogResult | None:
        dialog = SplitPdfDialog(
            source_path=document.session.display_path,
            page_count=document.view.page_count,
            default_output_directory=document.session.display_path.parent,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return None
        return dialog.dialog_result

    def _confirm_source_revision_still_current(
        self,
        expected_revision: SourcePdfRevision,
    ) -> bool:
        try:
            current_revision = self._page_mutation_service.read_source_pdf_revision(
                expected_revision.resolved_path
            )
        except Exception as exc:
            logger.exception(
                "Failed to revalidate replacement source PDF: %s",
                expected_revision.resolved_path,
            )
            self._report_error("ページ置換に失敗しました", str(exc))
            return False
        if current_revision != expected_revision:
            self._report_error("ページ置換に失敗しました", "置換元PDFが変更されました")
            return False
        return True

    def _insertion_targets_for_document(
        self,
        document: DocumentTab,
    ) -> tuple[tuple[tuple[str, int], ...], int]:
        selection = tuple(sorted(set(document.view.selected_page_indexes)))
        current_page_index = document.view.page_index
        if selection:
            before_slot = selection[0]
            after_slot = selection[-1] + 1
            default_index = 2
        else:
            before_slot = current_page_index
            after_slot = current_page_index + 1
            default_index = 2
        targets = (
            ("先頭", 0),
            ("選択ページまたは現在ページの前", before_slot),
            ("選択ページまたは現在ページの後", after_slot),
            ("末尾", document.view.page_count),
        )
        return targets, default_index

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
        if document is None or document.session.is_saving or document.mutation_in_progress:
            return False
        if command.mutates_working_copy:
            return self._run_document_command_operation(
                document,
                command=command,
                operation=lambda: document.command_history.execute(command),
                failure_title="編集に失敗しました",
                success_description=command.description,
            )
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

    def _trigger_undo(self) -> bool:
        focus_widget = QApplication.focusWidget()
        if isinstance(focus_widget, QLineEdit) and focus_widget.isUndoAvailable():
            focus_widget.undo()
            return True
        return self._undo_current_command()

    def _trigger_redo(self) -> bool:
        focus_widget = QApplication.focusWidget()
        if isinstance(focus_widget, QLineEdit) and focus_widget.isRedoAvailable():
            focus_widget.redo()
            return True
        return self._redo_current_command()

    def _undo_current_command(self) -> bool:
        document = self._current_document()
        if (
            document is None
            or document.session.is_saving
            or document.mutation_in_progress
            or not document.command_history.can_undo
        ):
            return False
        description = document.command_history.undo_description
        undo_command = document.command_history.undo_command
        if description is None:
            return False
        if undo_command is not None and undo_command.mutates_working_copy:
            return self._run_document_command_operation(
                document,
                command=undo_command,
                operation=document.command_history.undo,
                failure_title="元に戻せませんでした",
                success_description=f"Undo: {description}",
            )
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
        if (
            document is None
            or document.session.is_saving
            or document.mutation_in_progress
            or not document.command_history.can_redo
        ):
            return False
        description = document.command_history.redo_description
        redo_command = document.command_history.redo_command
        if description is None:
            return False
        if redo_command is not None and redo_command.mutates_working_copy:
            return self._run_document_command_operation(
                document,
                command=redo_command,
                operation=document.command_history.redo,
                failure_title="やり直せませんでした",
                success_description=f"Redo: {description}",
            )
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
        if document is None or document.session.is_saving or document.mutation_in_progress:
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
        if session.is_saving or document.mutation_in_progress:
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

        document.command_history.mark_clean()
        session.set_modified(document.command_history.is_dirty)
        self._persist_recovery_metadata(session)
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
        if self._merge_worker_thread is not None:
            self._request_merge_cancel()
            self._merge_worker_thread.quit()
            if not self._merge_worker_thread.wait(5000):
                self._set_status_message("PDF結合の終了を待ち切れませんでした", error=True)
                event.ignore()
                return
        if self._image_to_pdf_worker_thread is not None:
            self._request_image_to_pdf_cancel()
            self._image_to_pdf_worker_thread.quit()
            if not self._image_to_pdf_worker_thread.wait(5000):
                self._set_status_message("画像PDF作成の終了を待ち切れませんでした", error=True)
                event.ignore()
                return
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
        mutation_blocked = bool(document and document.mutation_in_progress)
        background_pdf_operation = self._background_pdf_operation_in_progress()
        if document is not None:
            document.view.set_page_reordering_enabled(
                not document.session.is_saving and not document.mutation_in_progress
            )
        self.close_action.setEnabled(has_document and not mutation_blocked)
        self._update_undo_redo_actions(document)
        self.save_action.setEnabled(
            bool(
                document
                and not document.session.is_saving
                and not document.mutation_in_progress
                and (document.session.is_modified or document.session.requires_save_as)
            )
        )
        self.save_as_action.setEnabled(
            bool(document and not document.session.is_saving and not document.mutation_in_progress)
        )
        self.crop_pages_action.setEnabled(
            bool(
                document
                and document.view.selected_page_indexes
                and not document.session.is_saving
                and not document.mutation_in_progress
            )
        )
        self.delete_pages_action.setEnabled(
            bool(
                document
                and document.view.selected_page_indexes
                and len(document.view.selected_page_indexes) < document.view.page_count
                and not document.session.is_saving
                and not document.mutation_in_progress
            )
        )
        self.duplicate_pages_action.setEnabled(
            bool(
                document
                and document.view.selected_page_indexes
                and not document.session.is_saving
                and not document.mutation_in_progress
            )
        )
        self.extract_selected_pages_action.setEnabled(
            bool(
                document
                and document.view.selected_page_indexes
                and not document.session.is_saving
                and not document.mutation_in_progress
            )
        )
        self.extract_page_range_action.setEnabled(
            bool(document and not document.session.is_saving and not document.mutation_in_progress)
        )
        self.split_pdf_action.setEnabled(
            bool(
                document
                and document.view.page_count >= 2
                and not document.session.is_saving
                and not document.mutation_in_progress
                and not background_pdf_operation
            )
        )
        self.merge_pdfs_action.setEnabled(not background_pdf_operation)
        self.image_to_pdf_action.setEnabled(not background_pdf_operation)
        self.insert_pages_action.setEnabled(
            bool(document and not document.session.is_saving and not document.mutation_in_progress)
        )
        self.replace_pages_action.setEnabled(
            bool(
                document
                and document.view.selected_page_indexes
                and not document.session.is_saving
                and not document.mutation_in_progress
            )
        )
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

    def _background_pdf_operation_in_progress(self) -> bool:
        return bool(
            self._split_in_progress_session_id is not None
            or self._merge_worker_thread is not None
            or self._image_to_pdf_worker_thread is not None
        )

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

    def _on_view_current_page_changed(self, view: PdfView, page_index: int) -> None:
        document = next((item for item in self._documents if item.view is view), None)
        if document is None:
            return
        document.session.set_navigation_state(
            page_index=page_index,
            zoom_factor=document.session.zoom_factor,
        )
        self._schedule_recovery_metadata_persist(document.session)
        current_document = self._current_document()
        if current_document is not None and current_document.view is view:
            self._sync_toolbar(document)
            self._update_status()

    def _on_view_page_reorder_requested(
        self,
        view: PdfView,
        source_page_indexes: object,
        insertion_slot: int,
    ) -> None:
        current_document = self._current_document()
        if current_document is None or current_document.view is not view:
            return
        if not isinstance(source_page_indexes, tuple):
            return
        try:
            plan = build_page_reorder_plan(
                current_document.view.page_count,
                source_page_indexes,
                insertion_slot,
            )
        except PageReorderNoOpError:
            return
        except (TypeError, ValueError) as exc:
            self._report_error("ページの並べ替えに失敗しました", str(exc))
            return
        current_selection = tuple(sorted(set(current_document.view.selected_page_indexes)))
        if plan.source_page_indexes != current_selection:
            return
        self._reorder_selected_pages(plan)

    def _show_page_organizer_context_menu(self, view: PdfView, position: QPoint) -> None:
        current_document = self._current_document()
        if current_document is None or current_document.view is not view:
            return
        menu = QMenu(self)
        menu.addAction(self.crop_pages_action)
        menu.addSeparator()
        menu.addAction(self.extract_selected_pages_action)
        menu.addAction(self.extract_page_range_action)
        menu.addSeparator()
        menu.addAction(self.insert_pages_action)
        menu.addAction(self.replace_pages_action)
        menu.addSeparator()
        menu.addAction(self.duplicate_pages_action)
        menu.addAction(self.delete_pages_action)
        global_position = view.organizer_list_view.viewport().mapToGlobal(position)
        menu.exec(global_position)

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
            document_registered = False

            def apply_restored_page() -> None:
                nonlocal page_restored
                if page_restored:
                    return
                if view is None or view.page_count <= 0:
                    return
                if not document_registered:
                    QTimer.singleShot(0, apply_restored_page)
                    return
                page_restored = True
                target_page_index = min(restored_page_index, view.page_count - 1)
                view.set_page(target_page_index)

            view.document_loaded.connect(
                apply_restored_page,
                Qt.ConnectionType.SingleShotConnection,
            )
            view.current_page_changed.connect(
                lambda page_index: self._on_view_current_page_changed(view, page_index)
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
        view.page_reorder_requested.connect(
            lambda page_indexes, insertion_slot: self._on_view_page_reorder_requested(
                view,
                page_indexes,
                insertion_slot,
            )
        )
        view.organizer_list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.organizer_list_view.customContextMenuRequested.connect(
            lambda position: self._show_page_organizer_context_menu(view, position)
        )
        view.error_occurred.connect(lambda message: self._set_status_message(message, error=True))
        document = DocumentTab(
            session=session,
            view=view,
            command_history=CommandHistory(initially_dirty=session.is_modified),
        )
        tab_index: int | None = None
        try:
            self._documents.append(document)
            document_registered = True
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

        if view.page_count > 0:
            apply_restored_page()

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
        apply_change: bool = True,
    ) -> None:
        document.session.set_modified(document.command_history.is_dirty)
        document.session.record_operation(description)
        self._persist_recovery_metadata(document.session)
        if apply_change:
            self._apply_command_change(document, change)
        index = self._find_document_index(document.session.source_path)
        if index is not None:
            self._tabs.setTabText(index, self._tab_title(document))
            self._tabs.setTabToolTip(index, self._tab_tooltip(document))
        self._update_actions()
        self._update_window_title()
        self._update_status()

    def _apply_command_change(self, document: DocumentTab, change: CommandChange) -> None:
        if change.mutation_result is not None:
            document.view.transition_render_cache(
                change.mutation_result.old_revision,
                change.mutation_result.new_revision,
                affected_pages=change.mutation_result.affected_pages,
            )
        if not change.requires_reload:
            return
        restored_page_index = document.session.current_page_index
        restored_selection = document.view.selected_page_indexes
        reloaded = document.view.reload_document(
            restore_page_index=restored_page_index,
            restore_selected_page_indexes=restored_selection,
            clear_query=False,
        )
        if not reloaded:
            self._report_error(
                "再読み込みに失敗しました",
                "変更後のPDFを再読み込みできませんでした。タブを閉じずに状態を確認してください。",
            )

    def _run_document_command_operation(
        self,
        document: DocumentTab,
        *,
        command: DocumentCommand,
        operation: Callable[[], CommandChange],
        failure_title: str,
        success_description: str,
    ) -> bool:
        snapshot = document.view.suspend_for_working_copy_mutation()
        self._set_document_mutation_state(document, True)
        release_result = document.view.release_renderer_backend()
        if not release_result.success:
            document.view.resume_after_failed_working_copy_mutation(snapshot)
            self._set_document_mutation_state(document, False)
            self._report_error(
                failure_title,
                release_result.message or "PDFレンダラーの解放に失敗しました",
            )
            return False
        try:
            change = operation()
        except (CommandExecutionError, CommandUndoError, CommandRedoError) as exc:
            logger.exception("Mutating command failed: %s", exc.command.description)
            self._reload_after_mutation(document, snapshot)
            self._report_error(failure_title, str(exc.cause))
            return False
        reload_succeeded = self._reload_after_mutation(document, snapshot, change=change)
        self._finalize_successful_command(
            document,
            description=success_description,
            change=change,
            apply_change=False,
        )
        if not reload_succeeded:
            self._report_error(
                "再読み込みに失敗しました",
                "変更後のPDFを再読み込みできませんでした。タブを閉じずに状態を確認してください。",
            )
        return True

    def _reload_after_mutation(
        self,
        document: DocumentTab,
        snapshot: PdfViewMutationSnapshot,
        *,
        change: CommandChange | None = None,
        timeout_ms: int = 5000,
    ) -> bool:
        reload_snapshot = (
            self._transform_mutation_snapshot(snapshot, change) if change is not None else snapshot
        )
        if change is not None and change.mutation_result is not None:
            document.view.transition_render_cache(
                change.mutation_result.old_revision,
                change.mutation_result.new_revision,
                affected_pages=change.mutation_result.affected_pages,
                page_index_transition=change.mutation_result.page_index_transition,
            )
        event_loop = QEventLoop(self)
        success = False
        timed_out = False
        document.mutation_operation_id += 1
        operation_id = document.mutation_operation_id
        timeout = QTimer(self)
        timeout.setSingleShot(True)

        def handle_completed(reloaded: bool) -> None:
            nonlocal success
            if operation_id != document.mutation_operation_id:
                return
            success = reloaded
            timeout.stop()
            event_loop.quit()

        def handle_timeout() -> None:
            nonlocal timed_out
            if operation_id != document.mutation_operation_id:
                return
            timed_out = True
            event_loop.quit()

        document.view.mutation_reload_completed.connect(handle_completed)
        timeout.timeout.connect(handle_timeout)
        started = document.view.reload_after_working_copy_mutation(reload_snapshot)
        if not started:
            self._set_document_mutation_state(document, False)
            with suppress(RuntimeError, TypeError):
                document.view.mutation_reload_completed.disconnect(handle_completed)
            with suppress(RuntimeError, TypeError):
                timeout.timeout.disconnect(handle_timeout)
            return False
        timeout.start(timeout_ms)
        event_loop.exec()
        if timed_out:
            document.view.resume_after_failed_working_copy_mutation(snapshot)
            success = False
        self._set_document_mutation_state(document, False)
        with suppress(RuntimeError, TypeError):
            document.view.mutation_reload_completed.disconnect(handle_completed)
        with suppress(RuntimeError, TypeError):
            timeout.timeout.disconnect(handle_timeout)
        return success

    def _transform_mutation_snapshot(
        self,
        snapshot: PdfViewMutationSnapshot,
        change: CommandChange,
    ) -> PdfViewMutationSnapshot:
        mutation_result = change.mutation_result
        if mutation_result is None or mutation_result.page_index_transition is None:
            return snapshot
        transition = mutation_result.page_index_transition
        mapped_current_page = self._mapped_page_index(
            snapshot.current_page_index,
            transition.current_page_old_to_new,
        )
        if mapped_current_page is None:
            mapped_current_page = 0
        current_page_override = change.current_page_index_after
        if current_page_override is not None:
            if not 0 <= current_page_override < mutation_result.page_count:
                raise ValueError("current page override is outside the new page range")
            mapped_current_page = current_page_override
        if change.selected_page_indexes_after is not None:
            selected_page_indexes = tuple(
                page_index
                for page_index in change.selected_page_indexes_after
                if 0 <= page_index < mutation_result.page_count
            )
        else:
            selected_page_indexes = tuple(
                mapped_page_index
                for page_index in snapshot.selected_page_indexes
                if (
                    mapped_page_index := self._mapped_page_index(
                        page_index,
                        transition.current_page_old_to_new,
                    )
                )
                is not None
            )
        if not selected_page_indexes:
            selected_page_indexes = (mapped_current_page,)
        return PdfViewMutationSnapshot(
            current_page_index=mapped_current_page,
            selected_page_indexes=selected_page_indexes,
            logical_zoom=snapshot.logical_zoom,
            search_query=snapshot.search_query,
        )

    @staticmethod
    def _mapped_page_index(
        old_page_index: int,
        mapping: tuple[int | None, ...],
    ) -> int | None:
        if not 0 <= old_page_index < len(mapping):
            return None
        return mapping[old_page_index]

    def _set_document_mutation_state(self, document: DocumentTab, active: bool) -> None:
        document.mutation_in_progress = active
        document.view.set_page_reordering_enabled(not active and not document.session.is_saving)
        self._update_actions()
        if self._current_document() is document:
            self._sync_toolbar(document)

    def _update_undo_redo_actions(self, document: DocumentTab | None) -> None:
        if document is None:
            self.undo_action.setEnabled(False)
            self.redo_action.setEnabled(False)
            self.undo_action.setText("元に戻す")
            self.redo_action.setText("やり直す")
            return
        undo_description = document.command_history.undo_description
        redo_description = document.command_history.redo_description
        self.undo_action.setEnabled(
            not document.session.is_saving
            and not document.mutation_in_progress
            and document.command_history.can_undo
        )
        self.redo_action.setEnabled(
            not document.session.is_saving
            and not document.mutation_in_progress
            and document.command_history.can_redo
        )
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
        document.view.set_page_reordering_enabled(
            not document.session.is_saving and not document.mutation_in_progress
        )
        self._toolbar_widget.setState(
            ToolbarState(
                has_document=True,
                page_index=document.view.page_index,
                page_count=document.view.page_count,
                zoom_factor=document.session.zoom_factor,
                can_delete=bool(
                    document.view.selected_page_indexes
                    and len(document.view.selected_page_indexes) < document.view.page_count
                    and not document.session.is_saving
                    and not document.mutation_in_progress
                ),
                can_duplicate=bool(
                    document.view.selected_page_indexes
                    and not document.session.is_saving
                    and not document.mutation_in_progress
                ),
                can_rotate=bool(
                    document.view.selected_page_indexes
                    and not document.session.is_saving
                    and not document.mutation_in_progress
                ),
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
