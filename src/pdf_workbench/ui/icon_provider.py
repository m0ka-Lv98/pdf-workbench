from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from typing import ClassVar

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from pdf_workbench.ui.theme import ColorScheme


class IconName(StrEnum):
    OPEN = "open"
    SEARCH = "search"
    CHEVRON_LEFT = "chevron-left"
    CHEVRON_RIGHT = "chevron-right"
    CHEVRON_DOWN = "chevron-down"
    ZOOM_OUT = "zoom-out"
    ZOOM_IN = "zoom-in"
    ROTATE_CLOCKWISE = "rotate-clockwise"
    CLOSE = "close"
    DOCUMENT = "document"
    HISTORY = "history"
    STATUS_SUCCESS = "status-success"


class IconTone(StrEnum):
    DEFAULT = "default"
    MUTED = "muted"
    ACCENT = "accent"
    INVERSE = "inverse"


@dataclass(frozen=True, slots=True)
class _Palette:
    default: str
    muted: str
    accent: str
    inverse: str
    disabled: str

    def color_for_tone(self, tone: IconTone) -> str:
        if tone is IconTone.MUTED:
            return self.muted
        if tone is IconTone.ACCENT:
            return self.accent
        if tone is IconTone.INVERSE:
            return self.inverse
        return self.default


_PALETTES = {
    ColorScheme.LIGHT: _Palette(
        default="#1F2328",
        muted="#667085",
        accent="#2563EB",
        inverse="#FFFFFF",
        disabled="#98A2B3",
    ),
    ColorScheme.DARK: _Palette(
        default="#F2F4F7",
        muted="#A4A7AE",
        accent="#60A5FA",
        inverse="#FFFFFF",
        disabled="#6B7280",
    ),
}


class IconProvider:
    _icon_cache: ClassVar[dict[tuple[str, str, int, str], QIcon]] = {}
    _svg_cache: ClassVar[dict[str, str]] = {}

    @classmethod
    def load_svg(cls, name: IconName) -> str:
        cached = cls._svg_cache.get(name.value)
        if cached is not None:
            return cached
        resource = files("pdf_workbench.ui.icons").joinpath(f"{name.value}.svg")
        if not resource.is_file():
            raise FileNotFoundError(f"missing icon resource: {resource}")
        contents = resource.read_text(encoding="utf-8")
        cls._svg_cache[name.value] = contents
        return contents

    @classmethod
    def icon(
        cls,
        name: IconName,
        *,
        tone: IconTone = IconTone.DEFAULT,
        size: int = 18,
    ) -> QIcon:
        scheme = cls.current_scheme()
        cache_key = (name.value, tone.value, size)
        cached = cls._icon_cache.get((*cache_key, scheme.value))
        if cached is not None:
            return cached
        palette = _PALETTES[scheme]
        normal = cls._render_icon(name, size, palette.color_for_tone(tone))
        disabled = cls._render_icon(name, size, palette.disabled)
        icon = QIcon()
        icon.addPixmap(normal, QIcon.Mode.Normal, QIcon.State.Off)
        icon.addPixmap(normal, QIcon.Mode.Active, QIcon.State.Off)
        icon.addPixmap(normal, QIcon.Mode.Selected, QIcon.State.Off)
        icon.addPixmap(disabled, QIcon.Mode.Disabled, QIcon.State.Off)
        cls._icon_cache[(*cache_key, scheme.value)] = icon
        return icon

    @classmethod
    def current_scheme(cls) -> ColorScheme:
        app = QApplication.instance()
        if app is None:
            return ColorScheme.LIGHT
        value = app.property("colorScheme")
        if value == ColorScheme.DARK.value:
            return ColorScheme.DARK
        return ColorScheme.LIGHT

    @classmethod
    def invalidate_cache(cls) -> None:
        cls._icon_cache.clear()

    @classmethod
    def _render_icon(cls, name: IconName, size: int, color: str) -> QPixmap:
        logical_size = QSize(size, size)
        dpr = 1.0
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            dpr = max(1.0, screen.devicePixelRatio())
        pixmap = QPixmap(int(logical_size.width() * dpr), int(logical_size.height() * dpr))
        pixmap.fill(Qt.GlobalColor.transparent)
        pixmap.setDevicePixelRatio(dpr)
        svg_bytes = cls.load_svg(name).replace("currentColor", color).encode()
        renderer = QSvgRenderer(QByteArray(svg_bytes))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        renderer.render(painter)
        painter.end()
        return pixmap


def is_icon_valid(icon: QIcon) -> bool:
    if icon.isNull():
        return False
    pixmap = icon.pixmap(18, 18)
    return not pixmap.isNull() and pixmap.size() == QSize(18, 18)
