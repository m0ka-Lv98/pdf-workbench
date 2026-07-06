from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget


class EmptyState(QWidget):
    open_requested = Signal()
    recent_file_requested = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        self.setAccessibleName("Empty state")
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)

        title = QLabel("PDF Workbench", self)
        title.setObjectName("emptyStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle = QLabel(
            "PDFをドラッグ&ドロップするか、下のボタンから開いてください。",
            self,
        )
        subtitle.setObjectName("emptyStateSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setProperty("muted", True)

        self.open_button = QPushButton("PDFを開く", self)
        self.open_button.setObjectName("openPdfButton")
        self.open_button.setAccessibleName("Open PDF")
        self.open_button.setToolTip("PDFを開く")
        self.open_button.setProperty("variant", "primary")
        self.open_button.clicked.connect(self.open_requested.emit)

        self._recent_container = QVBoxLayout()
        self._recent_message = QLabel("最近使ったファイルはありません", self)
        self._recent_message.setObjectName("emptyStateRecentMessage")
        self._recent_message.setProperty("muted", True)
        self._recent_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._recent_message.setVisible(True)

        recent_frame = QFrame(self)
        recent_layout = QVBoxLayout(recent_frame)
        recent_layout.setSpacing(6)
        recent_layout.addWidget(self._recent_message)
        recent_layout.addLayout(self._recent_container)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(self.open_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(recent_frame)

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
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setToolTip(str(path))
            button.setAccessibleName(path.name)
            button.setProperty("variant", "outline")
            button.clicked.connect(
                lambda checked=False, file_path=path: self.recent_file_requested.emit(file_path)
            )
            self._recent_container.addWidget(button)
