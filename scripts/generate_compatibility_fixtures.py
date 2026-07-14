from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import pikepdf
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image, ImageChops
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
DEFAULT_LICENSE_SOURCE = Path("/usr/local/Caskroom/font-noto-sans-cjk-jp/2.004/LICENSE")
FONT_ENV_VAR = "PDF_WORKBENCH_JP_FONT"
FONT_SOURCE_URL = "https://github.com/notofonts/noto-cjk/tree/main/Sans"
FONT_LICENSE = "SIL Open Font License 1.1"
FONT_VERSION = "2.004"
FONT_FILE_NAME = "NotoSansCJKjp-Regular.otf"
TEXT_FIXTURE_FONT = "Noto Sans CJK JP"
RENDER_SCALE = 0.5
WHITE_RGB = (255, 255, 255)
SUBSET_FONT_NAME_PATTERN = re.compile(r"^/[A-Z]{6}\+")
README_TEXT = """# PDF compatibility corpus (Phase A)

このディレクトリは、将来の PDF 書き換え機能で使い回すための committed regression
corpus です。通常の `pytest` や CI 実行時に再生成せず、リポジトリに含まれる静的
fixture を正本として扱います。

## 目的

- PDF 構造、page geometry、intrinsic rotation、annotation subtype、
  抽出可能テキスト、PDFium 描画の回帰検知
- no-op round-trip 後の structure / rendering 比較
- Issue #19 Phase A のテスト基盤提供

Phase B の fixture はまだ含みません。画像 PDF、暗号化 PDF、破損 PDF、フォーム、
添付ファイル、線形化 PDF などは後続フェーズで追加します。

## 収録 fixture

- `digital-basic.pdf`
  - 通常のデジタル生成 PDF
  - 2 ページ、図形、塗り、線、固有の視覚マーカー
- `english-text.pdf`
  - PDFium で抽出可能な英語テキスト
  - 期待文字列: `PDF Workbench English compatibility fixture`
- `japanese-text.pdf`
  - PDFium で抽出可能な横書き日本語テキスト
  - 期待文字列: `PDFワークベンチ 日本語互換性テスト`
- `page-boxes.pdf`
  - MediaBox / CropBox の差異と non-zero origin
- `rotations.pdf`
  - `/Rotate` = `0 / 90 / 180 / 270`
- `annotations.pdf`
  - 標準 annotation subtype を含む構造 fixture

## 生成方法

生成スクリプト:

- `scripts/generate_compatibility_fixtures.py`

この corpus は第三者サイトから PDF を転載していません。すべてリポジトリ内の
生成スクリプトで作成しています。

通常テストでは fixture を再生成しません。fixture を更新する場合だけ、手動で次を実行します。

```bash
PDF_WORKBENCH_JP_FONT=/absolute/path/to/NotoSansCJKjp-Regular.otf \\
QT_QPA_PLATFORM=offscreen \\
python scripts/generate_compatibility_fixtures.py
```

または:

```bash
QT_QPA_PLATFORM=offscreen \\
python scripts/generate_compatibility_fixtures.py \\
  --font-path /absolute/path/to/NotoSansCJKjp-Regular.otf
```

要件:

- 日本語フォントは redistributable なものを明示指定する
- テスト中にネットワーク download を行わない
- full font binary をこのディレクトリへコミットしない
- 生成後は manifest と回帰テストを必ず更新・実行する

## 埋め込みフォント provenance

`japanese-text.pdf` などの日本語 fixture は、subset embedding された以下のフォントを利用します。

- family: `Noto Sans CJK JP`
- source file name: `NotoSansCJKjp-Regular.otf`
- version: `2.004`
- source: <https://github.com/notofonts/noto-cjk/tree/main/Sans>
- license: `SIL Open Font License 1.1`
- source file SHA-256:
  `68a3fc98800b2a27b371f2fb79991daf3633bd89309d4ffaa6946fd587f375b5`

ライセンス文は `licenses/OFL-1.1.txt` に保持します。

## ライセンス

- fixture PDFs: repository-generated, MIT
- embedded Japanese font subset: SIL Open Font License 1.1

## manifest

- `manifest.json` は corpus expectation の正本です
- 各 fixture の `sha256`、用途、provenance、page count、各ページの
  geometry / rotation / annotation subtype、annotation rectangle /
  appearance、text fixture の expected text を保持します
- Python テストへ同じ期待値を大量に重複ハードコードしない方針です

fixture を更新したら、生成スクリプトを再実行して `manifest.json` の SHA-256 と
expectation を更新してください。

更新後の確認:

```bash
ruff check .
ruff format --check .
mypy src/pdf_workbench
QT_QPA_PLATFORM=offscreen pytest --cov=pdf_workbench
QT_QPA_PLATFORM=offscreen pytest -q tests/test_pdf_compatibility_corpus.py
git diff --check
```
"""


@dataclass(frozen=True, slots=True)
class ExpectedAnnotation:
    subtype: str
    rect: tuple[float, float, float, float]
    has_appearance: bool


@dataclass(frozen=True, slots=True)
class ExpectedPage:
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    visible_box: tuple[float, float, float, float]
    rotation: int
    annotations: tuple[ExpectedAnnotation, ...] = ()


@dataclass(frozen=True, slots=True)
class FixtureExpectation:
    purpose: str
    pages: tuple[ExpectedPage, ...]
    text_contains: tuple[str, ...] = ()
    content_bearing: bool = True


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    expectation: FixtureExpectation
    generate: Callable[[Path, str], None]
    verify_japanese_subset: bool = False


@dataclass(frozen=True, slots=True)
class PdfAnnotationSnapshot:
    subtype: str
    rect: tuple[float, float, float, float]
    has_appearance: bool


@dataclass(frozen=True, slots=True)
class PdfStructurePageSnapshot:
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    rotation: int
    annotations: tuple[PdfAnnotationSnapshot, ...]


@dataclass(frozen=True, slots=True)
class PdfiumPageSnapshot:
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    visible_box: tuple[float, float, float, float]
    rotation: int
    rendered_size: tuple[int, int]


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
    verify_font_sha(font_path)
    output_dir = args.output_dir.expanduser().resolve()
    temp_root = Path(tempfile.mkdtemp(prefix="pdf-workbench-compatibility-", dir=output_dir.parent))
    try:
        compatibility_dir = temp_root / "compatibility"
        compatibility_dir.mkdir(parents=True, exist_ok=True)
        (compatibility_dir / "licenses").mkdir(parents=True, exist_ok=True)
        copy_license(args.license_source, compatibility_dir / "licenses" / "OFL-1.1.txt")
        write_readme(compatibility_dir / "README.md")

        QApplication.instance() or QApplication([])
        font_family = load_font_family(font_path)
        fixture_specs = build_fixture_specs()
        generate_fixtures(compatibility_dir, fixture_specs, font_family)
        validate_outputs(compatibility_dir, fixture_specs)
        manifest = build_manifest(fixture_specs, compatibility_dir, font_path)
        (compatibility_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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


def verify_font_sha(font_path: Path) -> None:
    actual = file_sha256(font_path)
    expected = "68a3fc98800b2a27b371f2fb79991daf3633bd89309d4ffaa6946fd587f375b5"
    if actual != expected:
        raise RuntimeError(
            f"Unexpected Japanese font SHA-256 for {font_path.name}: {actual} != {expected}"
        )


def copy_license(source: Path, destination: Path) -> None:
    if not source.exists():
        raise RuntimeError(f"Font license file not found: {source}")
    shutil.copyfile(source, destination)


def write_readme(path: Path) -> None:
    path.write_text(README_TEXT, encoding="utf-8")


def load_font_family(font_path: Path) -> str:
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    if font_id < 0:
        raise RuntimeError(f"Failed to load font: {font_path}")
    families = QFontDatabase.applicationFontFamilies(font_id)
    if not families:
        raise RuntimeError(f"No font family exported from: {font_path}")
    return families[0]


def build_fixture_specs() -> dict[str, FixtureSpec]:
    return {
        "digital-basic.pdf": FixtureSpec(
            expectation=FixtureExpectation(
                purpose="normal digitally generated PDF",
                pages=(
                    ExpectedPage(
                        media_box=(0.0, 0.0, 612.0, 792.0),
                        crop_box=(0.0, 0.0, 612.0, 792.0),
                        visible_box=(0.0, 0.0, 612.0, 792.0),
                        rotation=0,
                    ),
                    ExpectedPage(
                        media_box=(0.0, 0.0, 612.0, 792.0),
                        crop_box=(0.0, 0.0, 612.0, 792.0),
                        visible_box=(0.0, 0.0, 612.0, 792.0),
                        rotation=0,
                    ),
                ),
            ),
            generate=create_digital_basic_pdf,
        ),
        "english-text.pdf": FixtureSpec(
            expectation=FixtureExpectation(
                purpose="extractable English text",
                pages=(
                    ExpectedPage(
                        media_box=(0.0, 0.0, 400.0, 300.0),
                        crop_box=(0.0, 0.0, 400.0, 300.0),
                        visible_box=(0.0, 0.0, 400.0, 300.0),
                        rotation=0,
                    ),
                ),
                text_contains=("PDF Workbench English compatibility fixture",),
            ),
            generate=create_english_text_pdf,
        ),
        "japanese-text.pdf": FixtureSpec(
            expectation=FixtureExpectation(
                purpose="extractable horizontal Japanese text",
                pages=(
                    ExpectedPage(
                        media_box=(0.0, 0.0, 400.0, 300.0),
                        crop_box=(0.0, 0.0, 400.0, 300.0),
                        visible_box=(0.0, 0.0, 400.0, 300.0),
                        rotation=0,
                    ),
                ),
                text_contains=("PDFワークベンチ 日本語互換性テスト",),
            ),
            generate=create_japanese_text_pdf,
            verify_japanese_subset=True,
        ),
        "page-boxes.pdf": FixtureSpec(
            expectation=FixtureExpectation(
                purpose="MediaBox and CropBox coverage",
                pages=(
                    ExpectedPage(
                        media_box=(0.0, 0.0, 612.0, 792.0),
                        crop_box=(36.0, 72.0, 576.0, 720.0),
                        visible_box=(36.0, 72.0, 576.0, 720.0),
                        rotation=0,
                    ),
                    ExpectedPage(
                        media_box=(-20.0, -10.0, 580.0, 790.0),
                        crop_box=(50.0, 100.0, 450.0, 700.0),
                        visible_box=(50.0, 100.0, 450.0, 700.0),
                        rotation=0,
                    ),
                ),
            ),
            generate=create_page_boxes_pdf,
        ),
        "rotations.pdf": FixtureSpec(
            expectation=FixtureExpectation(
                purpose="intrinsic rotation coverage",
                pages=(
                    ExpectedPage(
                        media_box=(0.0, 0.0, 420.0, 300.0),
                        crop_box=(0.0, 0.0, 420.0, 300.0),
                        visible_box=(0.0, 0.0, 420.0, 300.0),
                        rotation=0,
                    ),
                    ExpectedPage(
                        media_box=(0.0, 0.0, 420.0, 300.0),
                        crop_box=(0.0, 0.0, 420.0, 300.0),
                        visible_box=(0.0, 0.0, 420.0, 300.0),
                        rotation=90,
                    ),
                    ExpectedPage(
                        media_box=(0.0, 0.0, 420.0, 300.0),
                        crop_box=(0.0, 0.0, 420.0, 300.0),
                        visible_box=(0.0, 0.0, 420.0, 300.0),
                        rotation=180,
                    ),
                    ExpectedPage(
                        media_box=(0.0, 0.0, 420.0, 300.0),
                        crop_box=(0.0, 0.0, 420.0, 300.0),
                        visible_box=(0.0, 0.0, 420.0, 300.0),
                        rotation=270,
                    ),
                ),
            ),
            generate=create_rotations_pdf,
        ),
        "annotations.pdf": FixtureSpec(
            expectation=FixtureExpectation(
                purpose="annotation preservation coverage",
                pages=(
                    ExpectedPage(
                        media_box=(0.0, 0.0, 400.0, 400.0),
                        crop_box=(0.0, 0.0, 400.0, 400.0),
                        visible_box=(0.0, 0.0, 400.0, 400.0),
                        rotation=0,
                        annotations=(
                            ExpectedAnnotation(
                                subtype="/Text",
                                rect=(60.0, 250.0, 90.0, 280.0),
                                has_appearance=False,
                            ),
                            ExpectedAnnotation(
                                subtype="/Square",
                                rect=(100.0, 130.0, 280.0, 220.0),
                                has_appearance=False,
                            ),
                        ),
                    ),
                ),
            ),
            generate=create_annotations_pdf,
        ),
    }


def generate_fixtures(
    compatibility_dir: Path,
    fixture_specs: Mapping[str, FixtureSpec],
    font_family: str,
) -> None:
    for name, spec in fixture_specs.items():
        spec.generate(compatibility_dir / name, font_family)


def create_pdf(
    path: Path,
    pages: Iterable[tuple[tuple[float, float], Callable[[QPainter, str, float, float], None]]],
    font_family: str,
) -> None:
    page_specs = tuple(pages)
    if not page_specs:
        raise ValueError("at least one page is required")

    writer = QPdfWriter(str(path))
    writer.setResolution(72)
    first_page_size, first_callback = page_specs[0]
    configure_writer_page(writer, first_page_size)

    painter = QPainter(writer)
    try:
        first_callback(painter, font_family, *first_page_size)
        for page_size, callback in page_specs[1:]:
            configure_writer_page(writer, page_size)
            if not writer.newPage():
                raise RuntimeError("failed to create PDF page")
            callback(painter, font_family, *page_size)
    finally:
        painter.end()


def configure_writer_page(writer: QPdfWriter, page_size: tuple[float, float]) -> None:
    width, height = page_size
    writer.setPageSize(QPageSize(QSizeF(width, height), QPageSize.Unit.Point))
    writer.setPageMargins(QMarginsF(0.0, 0.0, 0.0, 0.0), QPageLayout.Unit.Point)


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


def create_digital_basic_pdf(path: Path, font_family: str) -> None:
    def page_one(painter: QPainter, family: str, width: float, height: float) -> None:
        painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
        painter.setPen(QPen(QColor("#2563eb"), 4))
        painter.drawRect(40, 40, 200, 120)
        painter.fillRect(300, 80, 180, 90, QColor("#fde68a"))
        painter.drawLine(60, 240, 520, 700)
        draw_text(painter, family, 50, 730, "Digital Basic Page 1", size=20)
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
        draw_text(painter, family, 50, 730, "Digital Basic Page 2", size=20)
        draw_text(painter, family, 90, 330, "Marker A2", size=14)
        draw_text(painter, family, 330, 500, "Block B2", size=14)

    create_pdf(
        path,
        [((612.0, 792.0), page_one), ((612.0, 792.0), page_two)],
        font_family,
    )


def create_english_text_pdf(path: Path, font_family: str) -> None:
    def page(painter: QPainter, family: str, width: float, height: float) -> None:
        painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
        draw_text(
            painter,
            family,
            40,
            180,
            "PDF Workbench English compatibility fixture",
            size=18,
        )
        draw_text(painter, family, 40, 220, "Search text should round-trip intact.", size=14)

    create_pdf(path, [((400.0, 300.0), page)], font_family)


def create_japanese_text_pdf(path: Path, font_family: str) -> None:
    def page(painter: QPainter, family: str, width: float, height: float) -> None:
        painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
        draw_text(painter, family, 40, 180, "PDFワークベンチ 日本語互換性テスト", size=18)
        draw_text(painter, family, 40, 220, "東京", size=16)

    create_pdf(path, [((400.0, 300.0), page)], font_family)


def create_page_boxes_pdf(path: Path, font_family: str) -> None:
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
        [((612.0, 792.0), page_one), ((600.0, 800.0), page_two)],
        font_family,
    )
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].MediaBox = pikepdf.Array([0, 0, 612, 792])
        pdf.pages[0].CropBox = pikepdf.Array([36, 72, 576, 720])
        pdf.pages[1].MediaBox = pikepdf.Array([-20, -10, 580, 790])
        pdf.pages[1].CropBox = pikepdf.Array([50, 100, 450, 700])
        pdf.save(path)


def create_rotations_pdf(path: Path, font_family: str) -> None:
    rotations = (0, 90, 180, 270)

    def make_page(
        rotation: int,
    ) -> tuple[tuple[float, float], Callable[[QPainter, str, float, float], None]]:
        def page(painter: QPainter, family: str, width: float, height: float) -> None:
            painter.fillRect(0, 0, int(width), int(height), QColor("#ffffff"))
            painter.setPen(QPen(QColor("#111827"), 3))
            painter.drawLine(120, 210, 120, 70)
            painter.drawLine(120, 70, 95, 100)
            painter.drawLine(120, 70, 145, 100)
            painter.fillRect(250, 120, 80, 100, QColor("#f59e0b"))
            draw_text(painter, family, 40, 255, f"Rotation {rotation}", size=20)
            draw_text(painter, family, 40, 285, f"Page marker R{rotation}", size=14)

        return (420.0, 300.0), page

    create_pdf(path, [make_page(rotation) for rotation in rotations], font_family)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for index, rotation in enumerate(rotations):
            pdf.pages[index].Rotate = rotation
        pdf.save(path)


def create_annotations_pdf(path: Path, font_family: str) -> None:
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


def build_manifest(
    fixture_specs: Mapping[str, FixtureSpec],
    compatibility_dir: Path,
    font_path: Path,
) -> dict[str, object]:
    fixtures: list[dict[str, object]] = []
    font_sha = file_sha256(font_path)
    for name, spec in fixture_specs.items():
        path = compatibility_dir / name
        expectation = spec.expectation
        fixtures.append(
            {
                "file": name,
                "sha256": file_sha256(path),
                "purpose": expectation.purpose,
                "provenance": {
                    "kind": "generated-in-repository",
                    "generator": "scripts/generate_compatibility_fixtures.py",
                    "license": "MIT",
                    "font": {
                        "family": TEXT_FIXTURE_FONT,
                        "file_name": FONT_FILE_NAME,
                        "version": FONT_VERSION,
                        "source": FONT_SOURCE_URL,
                        "license": FONT_LICENSE,
                        "sha256": font_sha,
                    },
                },
                "expected": serialize_expectation(expectation),
            }
        )
    return {"schema_version": 1, "fixtures": fixtures}


def serialize_expectation(expectation: FixtureExpectation) -> dict[str, object]:
    return {
        "page_count": len(expectation.pages),
        "pages": [
            {
                "media_box": list(page.media_box),
                "crop_box": list(page.crop_box),
                "visible_box": list(page.visible_box),
                "rotation": page.rotation,
                "annotation_subtypes": [annotation.subtype for annotation in page.annotations],
                "annotations": [
                    {
                        "subtype": annotation.subtype,
                        "rect": list(annotation.rect),
                        "has_appearance": annotation.has_appearance,
                    }
                    for annotation in page.annotations
                ],
            }
            for page in expectation.pages
        ],
        "text_contains": list(expectation.text_contains),
        "content_bearing": expectation.content_bearing,
    }


def validate_outputs(
    compatibility_dir: Path,
    fixture_specs: Mapping[str, FixtureSpec],
) -> None:
    ensure_no_full_font_binaries(compatibility_dir)
    for name, spec in fixture_specs.items():
        path = compatibility_dir / name
        structure_pages = inspect_structure_pages(path)
        pdfium_pages = inspect_pdfium_pages(path, scale=RENDER_SCALE)
        expectation = spec.expectation

        if len(structure_pages) != len(expectation.pages):
            raise RuntimeError(
                f"{name}: structure page count {len(structure_pages)} "
                f"!= expected {len(expectation.pages)}"
            )
        if len(pdfium_pages) != len(expectation.pages):
            raise RuntimeError(
                f"{name}: PDFium page count {len(pdfium_pages)} "
                f"!= expected {len(expectation.pages)}"
            )

        for index, expected_page in enumerate(expectation.pages):
            structure_page = structure_pages[index]
            pdfium_page = pdfium_pages[index]
            label = f"{name} page {index}"
            assert_box_equals(
                structure_page.media_box,
                expected_page.media_box,
                f"{label} media_box",
            )
            assert_box_equals(structure_page.crop_box, expected_page.crop_box, f"{label} crop_box")
            assert_box_equals(
                pdfium_page.media_box,
                expected_page.media_box,
                f"{label} pdfium media_box",
            )
            assert_box_equals(
                pdfium_page.crop_box,
                expected_page.crop_box,
                f"{label} pdfium crop_box",
            )
            assert_box_equals(
                pdfium_page.visible_box,
                expected_page.visible_box,
                f"{label} visible_box",
            )
            if structure_page.rotation != expected_page.rotation:
                raise RuntimeError(
                    f"{label}: structure rotation {structure_page.rotation} "
                    f"!= {expected_page.rotation}"
                )
            if pdfium_page.rotation != expected_page.rotation:
                raise RuntimeError(
                    f"{label}: PDFium rotation {pdfium_page.rotation} != {expected_page.rotation}"
                )
            assert_annotations_equal(
                structure_page.annotations,
                expected_page.annotations,
                media_box=expected_page.media_box,
                label=label,
            )

        assert_expected_texts(path, expectation.text_contains)
        validate_content_bearing(path, expectation.content_bearing)
        if name == "rotations.pdf":
            validate_rotation_render_sizes(pdfium_pages)
        if spec.verify_japanese_subset:
            verify_japanese_font_subset(path)


def validate_rotation_render_sizes(pdfium_pages: Sequence[PdfiumPageSnapshot]) -> None:
    if len(pdfium_pages) != 4:
        raise RuntimeError("rotations.pdf must have four pages")
    size_0 = pdfium_pages[0].rendered_size
    size_90 = pdfium_pages[1].rendered_size
    size_180 = pdfium_pages[2].rendered_size
    size_270 = pdfium_pages[3].rendered_size
    if size_0 != size_180:
        raise RuntimeError(f"rotation sizes differ for 0/180: {size_0} != {size_180}")
    if size_90 != size_270:
        raise RuntimeError(f"rotation sizes differ for 90/270: {size_90} != {size_270}")
    if size_0[0] <= size_0[1]:
        raise RuntimeError(f"rotation 0 render must be landscape, got {size_0}")
    if size_90[0] >= size_90[1]:
        raise RuntimeError(f"rotation 90 render must be portrait, got {size_90}")
    if size_0 != (size_90[1], size_90[0]):
        raise RuntimeError(f"rotation render sizes must swap, got {size_0} and {size_90}")


def assert_expected_texts(path: Path, expected_texts: Sequence[str]) -> None:
    if not expected_texts:
        return
    actual_text = extract_pdfium_text(path)
    for expected_text in expected_texts:
        normalized = normalize_extracted_text(expected_text)
        if normalized not in actual_text:
            raise RuntimeError(
                f"Missing expected text in {path.name}: {normalized!r} not in {actual_text!r}"
            )


def validate_content_bearing(path: Path, content_bearing: bool) -> None:
    images = render_pdf_pages(path, scale=RENDER_SCALE)
    try:
        for page_index, image in enumerate(images):
            if content_bearing:
                assert_image_has_non_background_content(
                    image,
                    fixture_name=path.name,
                    page_index=page_index,
                )
    finally:
        for image in images:
            image.close()


def ensure_no_full_font_binaries(compatibility_dir: Path) -> None:
    disallowed = {".ttf", ".otf", ".ttc", ".otc"}
    for path in compatibility_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in disallowed:
            raise RuntimeError(f"Full font binary must not be committed: {path.name}")


def verify_japanese_font_subset(path: Path) -> None:
    with pikepdf.open(path) as pdf:
        page = pdf.pages[0]
        font_dict = page.Resources.get("/Font", None)
        if not isinstance(font_dict, pikepdf.Dictionary):
            raise RuntimeError(f"{path.name}: page font resources are missing")

        found_embedded_stream = False
        found_subset_name = False
        for font_ref in font_dict.values():
            font = dereference(font_ref)
            descendant_fonts = font.get("/DescendantFonts", None)
            if descendant_fonts is None:
                continue
            for descendant_ref in descendant_fonts:
                descendant = dereference(descendant_ref)
                descriptor_ref = descendant.get("/FontDescriptor", None)
                if descriptor_ref is None:
                    continue
                descriptor = dereference(descriptor_ref)
                if any(key in descriptor for key in ("/FontFile", "/FontFile2", "/FontFile3")):
                    found_embedded_stream = True
                font_name = str(descriptor.get("/FontName", ""))
                if SUBSET_FONT_NAME_PATTERN.match(font_name):
                    found_subset_name = True
        if not found_embedded_stream:
            raise RuntimeError(f"{path.name}: embedded font stream was not found")
        if not found_subset_name:
            raise RuntimeError(f"{path.name}: subset font name prefix was not found")


def inspect_structure_pages(path: Path) -> tuple[PdfStructurePageSnapshot, ...]:
    with pikepdf.open(path) as pdf:
        snapshots: list[PdfStructurePageSnapshot] = []
        for page in pdf.pages:
            media_box = normalize_box(page.MediaBox)
            crop_box_obj = page.get("/CropBox", None)
            crop_box = media_box if crop_box_obj is None else normalize_box(crop_box_obj)
            rotation = normalize_rotation(int(page.get("/Rotate", 0)))
            annotations: list[PdfAnnotationSnapshot] = []
            annots_obj = page.get("/Annots", None)
            if annots_obj is not None:
                if not isinstance(annots_obj, pikepdf.Array):
                    raise RuntimeError(f"{path.name}: /Annots must be an array")
                for annot_ref in annots_obj:
                    annot = dereference(annot_ref)
                    subtype = str(annot.get("/Subtype", ""))
                    if not subtype:
                        raise RuntimeError(f"{path.name}: annotation /Subtype is missing")
                    rect = normalize_box(annot.get("/Rect", None))
                    if not box_within(rect, media_box):
                        raise RuntimeError(
                            f"{path.name}: annotation rect {rect!r} "
                            f"must stay within MediaBox {media_box!r}"
                        )
                    annotations.append(
                        PdfAnnotationSnapshot(
                            subtype=subtype,
                            rect=rect,
                            has_appearance="/AP" in annot,
                        )
                    )
            snapshots.append(
                PdfStructurePageSnapshot(
                    media_box=media_box,
                    crop_box=crop_box,
                    rotation=rotation,
                    annotations=tuple(annotations),
                )
            )
        return tuple(snapshots)


def inspect_pdfium_pages(path: Path, *, scale: float) -> tuple[PdfiumPageSnapshot, ...]:
    document = pdfium.PdfDocument(str(path))
    snapshots: list[PdfiumPageSnapshot] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            bitmap: Any | None = None
            pil_image: Image.Image | None = None
            try:
                media_box = normalize_box(page.get_mediabox(fallback_ok=True))
                crop_box = normalize_box(page.get_cropbox(fallback_ok=True))
                visible_box = normalize_box(page.get_bbox())
                rotation = normalize_rotation(int(page.get_rotation()))
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil().convert("RGBA")
                rendered_size = (pil_image.width, pil_image.height)
                if rendered_size[0] <= 0 or rendered_size[1] <= 0:
                    raise RuntimeError(
                        f"{path.name} page {page_index}: rendered image dimensions must be positive"
                    )
                snapshots.append(
                    PdfiumPageSnapshot(
                        media_box=media_box,
                        crop_box=crop_box,
                        visible_box=visible_box,
                        rotation=rotation,
                        rendered_size=rendered_size,
                    )
                )
            finally:
                if pil_image is not None:
                    pil_image.close()
                if bitmap is not None and hasattr(bitmap, "close"):
                    bitmap.close()
                page.close()
    finally:
        document.close()
    return tuple(snapshots)


def render_pdf_pages(path: Path, *, scale: float) -> tuple[Image.Image, ...]:
    document = pdfium.PdfDocument(str(path))
    images: list[Image.Image] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            bitmap: Any | None = None
            pil_image: Image.Image | None = None
            try:
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil().convert("RGBA")
                if pil_image.width <= 0 or pil_image.height <= 0:
                    raise RuntimeError(
                        f"{path.name} page {page_index}: rendered image dimensions must be positive"
                    )
                images.append(pil_image.copy())
            finally:
                if pil_image is not None:
                    pil_image.close()
                if bitmap is not None and hasattr(bitmap, "close"):
                    bitmap.close()
                page.close()
    finally:
        document.close()
    return tuple(images)


def extract_pdfium_text(path: Path) -> str:
    document = pdfium.PdfDocument(str(path))
    chunks: list[str] = []
    try:
        for page_index in range(len(document)):
            page = document[page_index]
            text_page = None
            try:
                text_page = page.get_textpage()
                chunks.append(text_page.get_text_range())
            finally:
                if text_page is not None:
                    text_page.close()
                page.close()
    finally:
        document.close()
    return normalize_extracted_text(" ".join(chunks))


def normalize_extracted_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value.replace("\x00", ""))
    collapsed = " ".join(normalized.split())
    return collapsed.strip()


def flatten_on_white(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    composite = Image.alpha_composite(white, rgba)
    rgba.close()
    white.close()
    rgb = composite.convert("RGB")
    composite.close()
    return rgb


def assert_image_has_non_background_content(
    image: Image.Image,
    *,
    fixture_name: str,
    page_index: int,
    background: tuple[int, int, int] = WHITE_RGB,
) -> None:
    flattened = flatten_on_white(image)
    background_image = None
    diff = None
    try:
        background_image = Image.new("RGB", flattened.size, background)
        diff = ImageChops.difference(flattened, background_image)
        if diff.getbbox() is None:
            raise RuntimeError(
                f"{fixture_name} page {page_index}: content-bearing page rendered as blank white"
            )
    finally:
        if diff is not None:
            diff.close()
        if background_image is not None:
            background_image.close()
        flattened.close()


def assert_annotations_equal(
    actual: Sequence[PdfAnnotationSnapshot],
    expected: Sequence[ExpectedAnnotation],
    *,
    media_box: tuple[float, float, float, float],
    label: str,
) -> None:
    if len(actual) != len(expected):
        raise RuntimeError(f"{label}: annotation count {len(actual)} != expected {len(expected)}")
    for index, (actual_annotation, expected_annotation) in enumerate(
        zip(actual, expected, strict=True)
    ):
        if actual_annotation.subtype != expected_annotation.subtype:
            raise RuntimeError(
                f"{label}: annotation {index} subtype "
                f"{actual_annotation.subtype} != {expected_annotation.subtype}"
            )
        assert_box_equals(
            actual_annotation.rect,
            expected_annotation.rect,
            f"{label} annotation {index} rect",
        )
        if actual_annotation.has_appearance != expected_annotation.has_appearance:
            raise RuntimeError(
                f"{label}: annotation {index} has_appearance "
                f"{actual_annotation.has_appearance} != {expected_annotation.has_appearance}"
            )
        if not box_within(actual_annotation.rect, media_box):
            raise RuntimeError(
                f"{label}: annotation {index} rect {actual_annotation.rect!r} "
                f"must stay within MediaBox {media_box!r}"
            )


def assert_box_equals(
    actual: Sequence[float],
    expected: Sequence[float],
    label: str,
    *,
    abs_tolerance: float = 0.01,
) -> None:
    if len(actual) != 4 or len(expected) != 4:
        raise RuntimeError(f"{label}: box length mismatch {actual!r} vs {expected!r}")
    for index, (actual_value, expected_value) in enumerate(zip(actual, expected, strict=True)):
        if abs(float(actual_value) - float(expected_value)) > abs_tolerance:
            raise RuntimeError(
                f"{label}: mismatch at {index}: {tuple(actual)!r} != {tuple(expected)!r}"
            )


def normalize_box(values: object) -> tuple[float, float, float, float]:
    try:
        sequence = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise RuntimeError(f"Invalid box: {values!r}") from exc
    if len(sequence) != 4:
        raise RuntimeError(f"Invalid box: {values!r}")
    floats = tuple(float(value) for value in sequence)
    if not all(math.isfinite(value) for value in floats):
        raise RuntimeError(f"Box values must be finite: {floats!r}")
    left, bottom, right, top = floats
    if right <= left or top <= bottom:
        raise RuntimeError(f"Invalid box ordering: {floats!r}")
    return floats


def box_within(inner: Sequence[float], outer: Sequence[float], *, tolerance: float = 0.01) -> bool:
    return (
        float(inner[0]) >= float(outer[0]) - tolerance
        and float(inner[1]) >= float(outer[1]) - tolerance
        and float(inner[2]) <= float(outer[2]) + tolerance
        and float(inner[3]) <= float(outer[3]) + tolerance
    )


def normalize_rotation(value: int) -> int:
    rotation = int(value) % 360
    if rotation not in {0, 90, 180, 270}:
        raise RuntimeError(f"rotation must normalize to 0/90/180/270, got {value}")
    return rotation


def dereference(value: Any) -> Any:
    return value.get_object() if hasattr(value, "get_object") else value


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 64), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
