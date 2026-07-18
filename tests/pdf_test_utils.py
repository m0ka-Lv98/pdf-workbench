from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageDraw
from pypdf import PdfWriter
from PySide6.QtCore import QMarginsF, QSizeF
from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPainter, QPdfWriter
from PySide6.QtWidgets import QApplication


def copy_pdf_fixture(name: str, destination: Path) -> Path:
    fixture_path = Path(__file__).with_name("fixtures") / name
    shutil.copyfile(fixture_path, destination)
    return destination


def create_blank_pdf(path: Path, page_count: int) -> Path:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def create_simple_text_pdf(path: Path, pages: list[str]) -> Path:
    objects: dict[int, bytes] = {
        1: b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        2: f"2 0 obj << /Type /Pages /Count {len(pages)} /Kids [".encode("ascii"),
    }
    page_object_numbers = [3 + index * 2 for index in range(len(pages))]
    content_object_numbers = [4 + index * 2 for index in range(len(pages))]
    objects[2] += b" ".join(f"{number} 0 R".encode("ascii") for number in page_object_numbers)
    objects[2] += b"] >> endobj\n"
    for page_number, content_number, text in zip(
        page_object_numbers,
        content_object_numbers,
        pages,
        strict=True,
    ):
        content = f"BT /F1 18 Tf 40 100 Td ({text}) Tj ET".encode("latin-1")
        objects[page_number] = (
            f"{page_number} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            f"/Resources << /Font << /F1 100 0 R >> >> /Contents {content_number} 0 R "
            f">> endobj\n".encode("ascii")
        )
        objects[content_number] = (
            f"{content_number} 0 obj << /Length {len(content)} >> stream\n".encode("ascii")
            + content
            + b"\nendstream\nendobj\n"
        )
    objects[100] = b"100 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    max_object_number = max(objects)
    for object_number in range(1, max_object_number + 1):
        offsets.append(len(pdf))
        pdf.extend(
            objects.get(
                object_number,
                f"{object_number} 0 obj << >> endobj\n".encode("ascii"),
            )
        )
    xref_start = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n"
        ).encode("ascii")
    )
    path.write_bytes(pdf)
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
