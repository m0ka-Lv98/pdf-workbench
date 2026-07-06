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
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(12)

        title = QLabel("PDF Workbench", self)
        title.setObjectName("emptyStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle = QLabel("PDFをドラッグ&ドロップするか、下のボタンから開いてください。", self)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setProperty("muted", True)

        self.open_button = QPushButton("PDFを開く", self)
        self.open_button.setProperty("variant", "primary")
        self.open_button.clicked.connect(self.open_requested.emit)

        self.recent_container = QVBoxLayout()
        recent_frame = QFrame(self)
        recent_layout = QVBoxLayout(recent_frame)
        recent_layout.setSpacing(6)
        recent_layout.addLayout(self.recent_container)

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(self.open_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(recent_frame)

    def set_recent_files(self, paths: list[Path]) -> None:
        while self.recent_container.count():
            item = self.recent_container.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for path in paths:
            button = QPushButton(path.name, self)
            button.setToolTip(str(path))
            button.clicked.connect(
                lambda checked=False, file_path=path: self.recent_file_requested.emit(file_path)
            )
            self.recent_container.addWidget(button)
