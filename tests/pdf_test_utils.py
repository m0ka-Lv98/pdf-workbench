from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw
from pypdf import PdfWriter
from PySide6.QtCore import QMarginsF, QSizeF
from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication


def create_blank_pdf(path: Path, page_count: int) -> Path:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def create_qt_text_pdf(path: Path, pages: list[str]) -> Path:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("QApplication must exist before creating Qt PDF fixtures")

    writer = QPdfWriter(str(path))
    writer.setResolution(72)
    writer.setPageSize(QPageSize(QSizeF(200, 200), QPageSize.Unit.Point))
    writer.setPageMargins(QMarginsF(0.0, 0.0, 0.0, 0.0), QPageLayout.Unit.Point)

    painter = QPainter(writer)
    font = QFont()
    font.setPointSize(18)
    painter.setFont(font)

    for index, text in enumerate(pages):
        if index:
            writer.newPage()
        painter.drawText(40, 100, text)

    painter.end()
    return path


def create_image_only_pdf(path: Path, label: str = "scan") -> Path:
    image = Image.new("RGB", (200, 200), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 80), label, fill="black")
    image.save(path, "PDF")
    return path
