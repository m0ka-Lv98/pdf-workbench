from __future__ import annotations

from types import TracebackType
from typing import Any

import pytest

from pdf_workbench.services import pdf_document_validator
from pdf_workbench.services.pdf_document_validator import (
    PdfDocumentValidationError,
    PdfDocumentValidator,
)


class FakePikePdf:
    def __init__(self, page_count: int) -> None:
        self.pages = [object()] * page_count

    def __enter__(self) -> FakePikePdf:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakePilImage:
    def __init__(self, width: int = 1, height: int = 1) -> None:
        self.width = width
        self.height = height
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeBitmap:
    def __init__(self, image: FakePilImage) -> None:
        self.image = image
        self.closed = False

    def to_pil(self) -> FakePilImage:
        return self.image

    def close(self) -> None:
        self.closed = True


class FakePdfiumPage:
    def __init__(self, image: FakePilImage) -> None:
        self.image = image
        self.bitmap: FakeBitmap | None = None
        self.closed = False

    def render(self, *, scale: float) -> FakeBitmap:
        assert scale == 0.2
        self.bitmap = FakeBitmap(self.image)
        return self.bitmap

    def close(self) -> None:
        self.closed = True


class FakePdfiumDocument:
    def __init__(self, path: str, *, page_count: int = 2) -> None:
        self.path = path
        self.pages = [FakePdfiumPage(FakePilImage()) for _ in range(page_count)]
        self.closed = False

    def __len__(self) -> int:
        return len(self.pages)

    def __getitem__(self, index: int) -> FakePdfiumPage:
        return self.pages[index]

    def close(self) -> None:
        self.closed = True


def test_validator_rejects_invalid_page_count_from_pikepdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pdf_document_validator.pikepdf,
        "open",
        lambda _path: FakePikePdf(page_count=0),
    )

    with pytest.raises(PdfDocumentValidationError, match="ページ数"):
        PdfDocumentValidator().validate("empty.pdf")


def test_validator_rejects_invalid_render_page_index(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_document = FakePdfiumDocument("valid.pdf", page_count=2)
    monkeypatch.setattr(
        pdf_document_validator.pikepdf,
        "open",
        lambda _path: FakePikePdf(page_count=2),
    )
    monkeypatch.setattr(
        pdf_document_validator.pdfium,
        "PdfDocument",
        lambda path: fake_document,
    )

    with pytest.raises(PdfDocumentValidationError, match="描画検証範囲"):
        PdfDocumentValidator().validate("valid.pdf", render_page_indexes=(2,))

    assert fake_document.closed is True


def test_validator_closes_render_resources_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_document = FakePdfiumDocument("bad-render.pdf", page_count=1)
    fake_document.pages[0].image = FakePilImage(width=0, height=1)
    monkeypatch.setattr(
        pdf_document_validator.pikepdf,
        "open",
        lambda _path: FakePikePdf(page_count=1),
    )
    monkeypatch.setattr(
        pdf_document_validator.pdfium,
        "PdfDocument",
        lambda path: fake_document,
    )

    with pytest.raises(PdfDocumentValidationError, match="描画検証"):
        PdfDocumentValidator().validate("bad-render.pdf")

    page = fake_document.pages[0]
    assert page.closed is True
    assert page.bitmap is not None
    assert page.bitmap.closed is True
    assert page.image.closed is True
    assert fake_document.closed is True


def test_validator_wraps_pdfium_open_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pdf_document_validator.pikepdf,
        "open",
        lambda _path: FakePikePdf(page_count=1),
    )

    def fail_pdfium_open(_path: str) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(pdf_document_validator.pdfium, "PdfDocument", fail_pdfium_open)

    with pytest.raises(PdfDocumentValidationError, match="PDFium"):
        PdfDocumentValidator().validate("broken.pdf")
