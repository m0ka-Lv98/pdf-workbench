from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import unicodedata
from collections.abc import Callable, Iterable
from hashlib import sha256
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL.Image import Image
from PySide6.QtCore import QMarginsF, QPointF, QSizeF
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "tests" / "fixtures" / "compatibility"
DEFAULT_LICENSE_DIR = DEFAULT_OUTPUT_DIR / "licenses"
DEFAULT_LICENSE_SOURCE = Path("/usr/local/Caskroom/font-noto-sans-cjk-jp/2.004/LICENSE")
FONT_ENV_VAR = "PDF_WORKBENCH_JP_FONT"
FONT_SOURCE_URL = "https://github.com/notofonts/noto-cjk/tree/main/Sans"
FONT_LICENSE = "SIL Open Font License 1.1"
FONT_VERSION = "2.004"
TEXT_FIXTURE_FONT = "Noto Sans CJK JP"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where compatibility fixtures and manifest will be written.",
    )
    parser.add_argument(
        "--font-path",
        type=Path,
        default=None,
        help=f"Path to a redistributable Japanese font. Defaults to ${FONT_ENV_VAR}.",
    )
    parser.add_argument(
        "--license-source",
        type=Path,
        default=DEFAULT_LICENSE_SOURCE,
        help="Path to the upstream font license text to copy into the fixture directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    font_path = resolve_font_path(args.font_path)
    output_dir = args.output_dir.expanduser().resolve()
    temp_root = Path(tempfile.mkdtemp(prefix="pdf-workbench-compatibility-", dir=output_dir.parent))
    try:
        compatibility_dir = temp_root / "compatibility"
        compatibility_dir.mkdir(parents=True, exist_ok=True)
        (compatibility_dir / "licenses").mkdir(parents=True, exist_ok=True)
        copy_license(args.license_source, compatibility_dir / "licenses" / "OFL-1.1.txt")

        QApplication.instance() or QApplication([])
        font_family = load_font_family(font_path)
        fixture_specs = build_fixtures(compatibility_dir, font_path, font_family)
        manifest = build_manifest(compatibility_dir, fixture_specs, font_path)
        (compatibility_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        validate_outputs(compatibility_dir, fixture_specs)
        replace_output_dir(compatibility_dir, output_dir)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
    return 0


def resolve_font_path(cli_path: Path | None) -> Path:
    if cli_path is not None:
        candidate = cli_path.expanduser().resolve()
    else:
        env_value = os.environ.get(FONT_ENV_VAR, "").strip()
        if not env_value:
            raise RuntimeError(
                f"Japanese font path is required. Pass --font-path or set {FONT_ENV_VAR}."
            )
        candidate = Path(env_value).expanduser().resolve()
    if not candidate.exists():
        raise RuntimeError(f"Japanese font not found: {candidate}")
    return candidate


def copy_license(source: Path, destination: Path) -> None:
    if not source.exists():
        raise RuntimeError(f"Font license file not found: {source}")
    shutil.copyfile(source, destination)


def load_font_family(font_path: Path) -> str:
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    if font_id < 0:
        raise RuntimeError(f"Failed to load font: {font_path}")
    families = QFontDatabase.applicationFontFamilies(font_id)
    if not families:
        raise RuntimeError(f"No font family exported from: {font_path}")
    return families[0]


def build_fixtures(
    compatibility_dir: Path,
    font_path: Path,
    font_family: str,
) -> dict[str, dict[str, object]]:
    del font_path
    specs: dict[str, dict[str, object]] = {}
    specs["digital-basic.pdf"] = create_digital_basic_pdf(
        compatibility_dir / "digital-basic.pdf",
        font_family,
    )
    specs["english-text.pdf"] = create_english_text_pdf(
        compatibility_dir / "english-text.pdf",
        font_family,
    )
    specs["japanese-text.pdf"] = create_japanese_text_pdf(
        compatibility_dir / "japanese-text.pdf",
        font_family,
    )
    specs["page-boxes.pdf"] = create_page_boxes_pdf(
        compatibility_dir / "page-boxes.pdf",
        font_family,
    )
    specs["rotations.pdf"] = create_rotations_pdf(
        compatibility_dir / "rotations.pdf",
        font_family,
    )
    specs["annotations.pdf"] = create_annotations_pdf(
        compatibility_dir / "annotations.pdf",
        font_family,
    )
    return specs


def create_pdf(
    path: Path,
    pages: Iterable[tuple[tuple[float, float], Callable[[QPainter, str, float, float], None]]],
    font_family: str,
) -> None:
    writer = QPdfWriter(str(path))
    writer.setResolution(72)
    painter = QPainter(writer)
    try:
        for page_index, (page_size, callback) in enumerate(pages):
            width, height = page_size
            writer.setPageSize(QPageSize(QSizeF(width, height), QPageSize.Unit.Point))
            writer.setPageMargins(QMarginsF(0.0, 0.0, 0.0, 0.0), QPageLayout.Unit.Point)
            if page_index:
                writer.newPage()
            callback(painter, font_family, width, height)
    finally:
        painter.end()


def draw_text(
    painter: QPainter,
    font_family: str,
    x: float,
    y: float,
    text: str,
    *,
    size: int = 16,
) -> None:
    font = QFont(font_family, size)
    painter.setFont(font)
    painter.drawText(int(x), int(y), text)


def create_digital_basic_pdf(path: Path, font_family: str) -> dict[str, object]:
    def page_one(painter: QPainter, family: str, width: float, height: float) -> None:
        painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
        painter.setPen(QPen(QColor("#2563eb"), 4))
        painter.drawRect(40, 40, 200, 120)
        painter.fillRect(300, 80, 180, 90, QColor("#fde68a"))
        painter.drawLine(60, 240, 520, 700)
        draw_text(
            painter,
            family,
            50,
            730,
            "Digital Basic Page 1",
            size=20,
        )
        draw_text(painter, family, 60, 220, "Marker A1", size=14)
        draw_text(painter, family, 320, 210, "Block B1", size=14)

    def page_two(painter: QPainter, family: str, width: float, height: float) -> None:
        painter.fillRect(0, 0, int(width), int(height), QColor("#f8fafc"))
        painter.setPen(QPen(QColor("#dc2626"), 5))
        polygon = QPolygonF(
            [
                QPointF(80, 120),
                QPointF(220, 90),
                QPointF(260, 220),
                QPointF(120, 260),
            ]
        )
        painter.drawPolygon(polygon)
        painter.fillRect(340, 520, 120, 140, QColor("#86efac"))
        painter.drawLine(100, 620, 500, 620)
        draw_text(
            painter,
            family,
            50,
            730,
            "Digital Basic Page 2",
            size=20,
        )
        draw_text(painter, family, 90, 330, "Marker A2", size=14)
        draw_text(painter, family, 330, 500, "Block B2", size=14)

    create_pdf(
        path,
        [
            ((612.0, 792.0), page_one),
            ((612.0, 792.0), page_two),
        ],
        font_family,
    )
    return {
        "purpose": "normal digitally generated PDF",
        "text_contains": [],
        "content_bearing": True,
    }


ENGLISH_TEXT = "PDF Workbench English compatibility fixture"
JAPANESE_TEXT = "PDFワークベンチ 日本語互換性テスト"


def create_english_text_pdf(path: Path, font_family: str) -> dict[str, object]:
    def page(painter: QPainter, family: str, width: float, height: float) -> None:
        painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
        draw_text(painter, family, 40, 180, ENGLISH_TEXT, size=18)
        draw_text(painter, family, 40, 220, "Search text should round-trip intact.", size=14)

    create_pdf(path, [((400.0, 300.0), page)], font_family)
    return {
        "purpose": "extractable English text",
        "text_contains": [ENGLISH_TEXT],
        "content_bearing": True,
    }


def create_japanese_text_pdf(path: Path, font_family: str) -> dict[str, object]:
    def page(painter: QPainter, family: str, width: float, height: float) -> None:
        painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
        draw_text(painter, family, 40, 180, JAPANESE_TEXT, size=18)
        draw_text(painter, family, 40, 220, "東京", size=16)

    create_pdf(path, [((400.0, 300.0), page)], font_family)
    return {
        "purpose": "extractable horizontal Japanese text",
        "text_contains": [JAPANESE_TEXT],
        "content_bearing": True,
    }


def create_page_boxes_pdf(path: Path, font_family: str) -> dict[str, object]:
    def page_one(painter: QPainter, family: str, width: float, height: float) -> None:
        painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
        painter.setPen(QPen(QColor("#0f172a"), 3))
        painter.drawRect(60, 90, 200, 140)
        draw_text(painter, family, 60, 700, "Page boxes 1", size=18)

    def page_two(painter: QPainter, family: str, width: float, height: float) -> None:
        painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
        painter.setPen(QPen(QColor("#7c3aed"), 3))
        painter.drawEllipse(120, 160, 160, 120)
        draw_text(painter, family, 100, 690, "Page boxes 2", size=18)

    create_pdf(
        path,
        [
            ((612.0, 792.0), page_one),
            ((600.0, 800.0), page_two),
        ],
        font_family,
    )
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page_one_pdf = pdf.pages[0]
        page_one_pdf.MediaBox = pikepdf.Array([0, 0, 612, 792])
        page_one_pdf.CropBox = pikepdf.Array([36, 72, 576, 720])
        page_two_pdf = pdf.pages[1]
        page_two_pdf.MediaBox = pikepdf.Array([-20, -10, 580, 790])
        page_two_pdf.CropBox = pikepdf.Array([50, 100, 450, 700])
        pdf.save(path)
    return {
        "purpose": "MediaBox and CropBox coverage",
        "text_contains": [],
        "content_bearing": True,
    }


def create_rotations_pdf(path: Path, font_family: str) -> dict[str, object]:
    rotations = (0, 90, 180, 270)

    def make_page(rotation: int) -> tuple[tuple[float, float], callable]:
        def page(painter: QPainter, family: str, width: float, height: float) -> None:
            painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
            painter.setPen(QPen(QColor("#111827"), 3))
            painter.drawLine(180, 260, 180, 90)
            painter.drawLine(180, 90, 150, 125)
            painter.drawLine(180, 90, 210, 125)
            painter.fillRect(260, 180, 70, 120, QColor("#f59e0b"))
            draw_text(painter, family, 40, 340, f"Rotation {rotation}", size=20)
            draw_text(painter, family, 40, 370, f"Page marker R{rotation}", size=14)

        return (420.0, 420.0), page

    create_pdf(path, [make_page(rotation) for rotation in rotations], font_family)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for index, rotation in enumerate(rotations):
            pdf.pages[index].Rotate = rotation
        pdf.save(path)
    return {
        "purpose": "intrinsic rotation coverage",
        "text_contains": [],
        "content_bearing": True,
    }


def create_annotations_pdf(path: Path, font_family: str) -> dict[str, object]:
    def page(painter: QPainter, family: str, width: float, height: float) -> None:
        painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
        draw_text(painter, family, 50, 320, "Annotation fixture", size=20)
        painter.setPen(QPen(QColor("#1d4ed8"), 2))
        painter.drawRect(100, 130, 180, 90)

    create_pdf(path, [((400.0, 400.0), page)], font_family)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page_obj = pdf.pages[0]
        text_annot = pikepdf.Dictionary(
            Type=pikepdf.Name("/Annot"),
            Subtype=pikepdf.Name("/Text"),
            Rect=pikepdf.Array([60, 250, 90, 280]),
            Contents=pikepdf.String("Text note"),
            Name=pikepdf.Name("/Comment"),
            Open=False,
        )
        square_annot = pikepdf.Dictionary(
            Type=pikepdf.Name("/Annot"),
            Subtype=pikepdf.Name("/Square"),
            Rect=pikepdf.Array([100, 130, 280, 220]),
            Contents=pikepdf.String("Square annotation"),
            C=pikepdf.Array([1, 0.75, 0]),
        )
        page_obj.Annots = pikepdf.Array(
            [pdf.make_indirect(text_annot), pdf.make_indirect(square_annot)]
        )
        pdf.save(path)
    return {
        "purpose": "annotation preservation coverage",
        "text_contains": [],
        "content_bearing": True,
    }


def build_manifest(
    compatibility_dir: Path,
    fixture_specs: dict[str, dict[str, object]],
    font_path: Path,
) -> dict[str, object]:
    fixtures: list[dict[str, object]] = []
    for name, spec in fixture_specs.items():
        path = compatibility_dir / name
        fixtures.append(
            {
                "file": name,
                "sha256": file_sha256(path),
                "purpose": spec["purpose"],
                "provenance": {
                    "kind": "generated-in-repository",
                    "generator": "scripts/generate_compatibility_fixtures.py",
                    "license": "MIT",
                    "font": {
                        "family": TEXT_FIXTURE_FONT,
                        "version": FONT_VERSION,
                        "source": FONT_SOURCE_URL,
                        "license": FONT_LICENSE,
                        "sha256": file_sha256(font_path),
                    },
                },
                "expected": {
                    "page_count": len(list_structure_pages(path)),
                    "pages": list_structure_pages(path),
                    "text_contains": spec["text_contains"],
                    "content_bearing": spec["content_bearing"],
                },
            }
        )
    return {
        "schema_version": 1,
        "fixtures": fixtures,
    }


def list_structure_pages(path: Path) -> list[dict[str, object]]:
    with pikepdf.open(path) as pdf:
        pages: list[dict[str, object]] = []
        for page in pdf.pages:
            media_box = normalize_box(page.MediaBox)
            crop_box_obj = page.get("/CropBox", None)
            crop_box = normalize_box(crop_box_obj) if crop_box_obj is not None else media_box
            rotation = int(page.get("/Rotate", 0)) % 360
            annots = []
            for annot in page.get("/Annots", []):
                annot_obj = annot.get_object() if hasattr(annot, "get_object") else annot
                annots.append(str(annot_obj.get("/Subtype", "")))
            pages.append(
                {
                    "media_box": media_box,
                    "crop_box": crop_box,
                    "rotation": rotation,
                    "annotation_subtypes": annots,
                }
            )
        return pages


def normalize_box(box: object) -> list[float]:
    if not isinstance(box, pikepdf.Array) or len(box) != 4:
        raise RuntimeError(f"Invalid box: {box!r}")
    values = [float(item) for item in box]
    return values


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 64), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_outputs(compatibility_dir: Path, fixture_specs: dict[str, dict[str, object]]) -> None:
    for name, spec in fixture_specs.items():
        path = compatibility_dir / name
        with pikepdf.open(path) as pdf:
            if len(pdf.pages) != len(list_structure_pages(path)):
                raise RuntimeError(f"Unexpected page count for {name}")
        document = pdfium.PdfDocument(str(path))
        try:
            if len(document) <= 0:
                raise RuntimeError(f"PDFium could not open pages for {name}")
            for index in range(len(document)):
                page = document[index]
                bitmap = None
                pil_image: Image | None = None
                try:
                    bitmap = page.render(scale=0.35)
                    pil_image = bitmap.to_pil().convert("RGBA")
                    if pil_image.width <= 0 or pil_image.height <= 0:
                        raise RuntimeError(f"Rendered image is empty for {name} page {index}")
                finally:
                    if pil_image is not None:
                        pil_image.close()
                    if bitmap is not None:
                        bitmap.close()
                    page.close()
            expected_texts = spec.get("text_contains", [])
            if isinstance(expected_texts, list) and expected_texts:
                text_page = document[0].get_textpage()
                try:
                    actual_text = normalize_extracted_text(text_page.get_text_range())
                finally:
                    text_page.close()
                    document[0].close()
                for expected_text in expected_texts:
                    expected_normalized = normalize_extracted_text(str(expected_text))
                    if expected_normalized not in actual_text:
                        raise RuntimeError(
                            "Missing expected text in "
                            f"{name}: {expected_normalized!r} not in {actual_text!r}"
                        )
        finally:
            document.close()


def normalize_extracted_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.replace("\x00", ""))
    collapsed = " ".join(normalized.split())
    return collapsed.strip()


def replace_output_dir(source_dir: Path, output_dir: Path) -> None:
    backup_dir = output_dir.with_name(output_dir.name + ".bak")
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    if output_dir.exists():
        os.replace(output_dir, backup_dir)
    try:
        os.replace(source_dir, output_dir)
    except Exception:
        if backup_dir.exists() and not output_dir.exists():
            os.replace(backup_dir, output_dir)
        raise
    else:
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
