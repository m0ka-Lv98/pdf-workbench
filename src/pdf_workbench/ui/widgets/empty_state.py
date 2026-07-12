from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from pdf_workbench.ui.icon_provider import IconName, IconProvider


class EmptyState(QWidget):
    open_requested = Signal()
    recent_file_requested = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        self.setAccessibleName("Empty state")

        root = QVBoxLayout(self)
        root.setContentsMargins(40, 28, 40, 28)
        root.setSpacing(24)
        root.addStretch(1)

        self._icon_label = QLabel(self)
        self._icon_label.setObjectName("emptyStateIcon")
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._title_label = QLabel("PDFを開いて作業を始めましょう", self)
        self._title_label.setObjectName("emptyStateTitle")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._subtitle_label = QLabel(
            "PDFを開くかドラッグ&ドロップして、検索や選択をすぐに利用できます。",
            self,
        )
        self._subtitle_label.setObjectName("emptyStateSubtitle")
        self._subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle_label.setWordWrap(True)

        self.open_button = QPushButton("PDFを開く", self)
        self.open_button.setObjectName("openPdfButton")
        self.open_button.setAccessibleName("Open PDF")
        self.open_button.setToolTip("PDFを開く")
        self.open_button.setProperty("variant", "primary")
        self.open_button.clicked.connect(self.open_requested.emit)

        self._recent_hint = QLabel("最近使ったファイル", self)
        self._recent_hint.setObjectName("emptyStateRecentHint")

        self._recent_message = QLabel("最近使ったファイルはありません", self)
        self._recent_message.setObjectName("emptyStateRecentMessage")
        self._recent_message.setVisible(True)

        self._recent_container = QVBoxLayout()
        self._recent_container.setSpacing(8)

        open_row = QHBoxLayout()
        open_row.addStretch(1)
        open_row.addWidget(self.open_button)
        open_row.addStretch(1)

        self._recent_block = QWidget(self)
        self._recent_block.setObjectName("emptyStateRecentBlock")
        recent_layout = QVBoxLayout(self._recent_block)
        recent_layout.setContentsMargins(0, 0, 0, 0)
        recent_layout.setSpacing(8)
        recent_layout.addWidget(self._recent_hint)
        recent_layout.addWidget(self._recent_message)
        recent_layout.addLayout(self._recent_container)

        root.addWidget(self._icon_label)
        root.addWidget(self._title_label)
        root.addWidget(self._subtitle_label)
        root.addLayout(open_row)
        root.addWidget(self._recent_block, alignment=Qt.AlignmentFlag.AlignHCenter)
        root.addStretch(2)

        self.refresh_theme_assets()

    def refresh_theme_assets(self) -> None:
        pixmap = IconProvider.icon(IconName.DOCUMENT, size=48).pixmap(48, 48)
        self._icon_label.setPixmap(pixmap)

    def set_recent_files(self, paths: list[Path]) -> None:
        while self._recent_container.count():
            item = self._recent_container.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        visible_paths = paths[:5]
        self._recent_message.setVisible(not visible_paths)
        for path in visible_paths:
            button = QPushButton(path.name, self)
            button.setObjectName(f"recentFileButton_{path.name}")
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setToolTip(str(path))
            button.setAccessibleName(path.name)
            button.setProperty("variant", "recent")
            button.setIcon(IconProvider.icon(IconName.HISTORY, size=16))
            button.clicked.connect(
                lambda checked=False, file_path=path: self.recent_file_requested.emit(file_path)
            )
            self._recent_container.addWidget(button)
