from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget


@dataclass(frozen=True, slots=True)
class SourceChangeBannerState:
    status_text: str
    message_text: str
    source_path_text: str
    visible: bool


class SourceChangeBanner(QWidget):
    save_as_requested = Signal()
    recheck_requested = Signal()
    dismiss_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sourceChangeBanner")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.setSpacing(12)

        self._message = QLabel("", self)
        self._message.setObjectName("sourceChangeBannerMessage")
        self._message.setWordWrap(True)
        self._message.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(self._message, 1)

        self.save_as_button = QPushButton("別名で保存", self)
        self.save_as_button.setObjectName("sourceChangeBannerSaveAsButton")
        self.save_as_button.clicked.connect(self.save_as_requested.emit)
        layout.addWidget(self.save_as_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.recheck_button = QPushButton("再確認", self)
        self.recheck_button.setObjectName("sourceChangeBannerRecheckButton")
        self.recheck_button.clicked.connect(self.recheck_requested.emit)
        layout.addWidget(self.recheck_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.later_button = QPushButton("後で", self)
        self.later_button.setObjectName("sourceChangeBannerLaterButton")
        self.later_button.clicked.connect(self.dismiss_requested.emit)
        layout.addWidget(self.later_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.hide()

    @property
    def message_label(self) -> QLabel:
        return self._message

    def set_state(self, state: SourceChangeBannerState) -> None:
        self._message.setText(state.message_text)
        self._message.setToolTip(state.source_path_text)
        self.setToolTip(state.source_path_text)
        self.setProperty("sourceStatus", state.status_text)
        self.style().unpolish(self)
        self.style().polish(self)
        self.setVisible(state.visible)
