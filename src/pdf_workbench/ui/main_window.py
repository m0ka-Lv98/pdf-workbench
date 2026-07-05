from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QToolBar,
)

from pdf_workbench.domain.document_session import DocumentSession
from pdf_workbench.ui.pdf_view import PdfView

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PDF Workbench")
        self.resize(1100, 800)

        self._session: DocumentSession | None = None
        self._view = PdfView(self)
        self.setCentralWidget(self._view)
        self.setStatusBar(QStatusBar(self))

        self._create_actions()
        self._create_menu()
        self._create_toolbar()
        self._update_status()

    def _create_actions(self) -> None:
        self.open_action = QAction("開く", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.open_action.triggered.connect(self._choose_document)

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

    def _create_menu(self) -> None:
        file_menu = self.menuBar().addMenu("ファイル")
        file_menu.addAction(self.open_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        view_menu = self.menuBar().addMenu("表示")
        view_menu.addAction(self.previous_action)
        view_menu.addAction(self.next_action)
        view_menu.addSeparator()
        view_menu.addAction(self.zoom_in_action)
        view_menu.addAction(self.zoom_out_action)

    def _create_toolbar(self) -> None:
        toolbar = QToolBar("メイン", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addAction(self.open_action)
        toolbar.addSeparator()
        toolbar.addAction(self.previous_action)
        toolbar.addAction(self.next_action)
        toolbar.addSeparator()
        toolbar.addAction(self.zoom_out_action)
        toolbar.addAction(self.zoom_in_action)

    def _choose_document(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "PDFを開く", "", "PDF files (*.pdf)")
        if filename:
            self.open_document(Path(filename))

    def open_document(self, path: Path) -> None:
        try:
            session = DocumentSession(path)
            self._view.open_document(session.source_path)
        except Exception as exc:
            logger.exception("Failed to open PDF: %s", path)
            QMessageBox.critical(self, "PDFを開けません", str(exc))
            return

        self._session = session
        self.setWindowTitle(f"{session.source_path.name} - PDF Workbench")
        self._update_status()

    def _previous_page(self) -> None:
        self._view.set_page(self._view.page_index - 1)
        self._update_status()

    def _next_page(self) -> None:
        self._view.set_page(self._view.page_index + 1)
        self._update_status()

    def _change_zoom(self, multiplier: float) -> None:
        if self._session is None:
            return
        self._session.zoom_factor = max(0.25, min(self._session.zoom_factor * multiplier, 5.0))
        self._view.set_zoom(1.5 * self._session.zoom_factor)
        self._update_status()

    def _update_status(self) -> None:
        if self._session is None:
            self.statusBar().showMessage("準備完了")
            return
        self.statusBar().showMessage(
            f"{self._view.page_index + 1} / {self._view.page_count} ページ  "
            f"ズーム {self._session.zoom_factor:.0%}"
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._session is not None and self._session.is_modified:
            result = QMessageBox.question(
                self,
                "未保存の変更",
                "未保存の変更があります。終了しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
