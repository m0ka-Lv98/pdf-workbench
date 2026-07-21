from __future__ import annotations

from typing import Any

import pikepdf
import pytest

from pdf_workbench.services.pdf_page_import import PdfPageImportError, PdfPageImportInspector


def inspector() -> PdfPageImportInspector:
    return PdfPageImportInspector()


def dictionary(payload: dict[str, Any]) -> pikepdf.Dictionary:
    return pikepdf.Dictionary(payload)


@pytest.mark.parametrize(
    ("key", "message"),
    [
        ("/B", "Article bead"),
        ("/AA", "ページアクション"),
        ("/Foo", "/Foo"),
    ],
)
def test_import_inspector_rejects_unsupported_page_keys(key: str, message: str) -> None:
    page = dictionary({"/Type": pikepdf.Name("/Page"), key: pikepdf.String("bad")})

    with pytest.raises(PdfPageImportError, match=message):
        inspector().validate_supported_page_keys(page)


def test_import_inspector_allows_supported_page_keys() -> None:
    page = dictionary(
        {
            "/Type": pikepdf.Name("/Page"),
            "/MediaBox": pikepdf.Array([0, 0, 200, 200]),
            "/CropBox": pikepdf.Array([0, 0, 100, 100]),
            "/Rotate": 90,
        }
    )

    inspector().validate_supported_page_keys(page)


def test_import_inspector_rejects_malformed_annotation_array() -> None:
    page = dictionary({"/Annots": pikepdf.String("bad")})

    with pytest.raises(PdfPageImportError, match="注釈配列"):
        inspector().validate_page_annotations(page, source_page_objgens=set())


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        (pikepdf.String("bad"), "注釈構造"),
        (dictionary({"/Subtype": pikepdf.Name("/Widget")}), "Widget"),
        (dictionary({"/Subtype": pikepdf.Name("/FileAttachment")}), "FileAttachment"),
        (
            dictionary({"/Subtype": pikepdf.Name("/Text"), "/A": pikepdf.Dictionary()}),
            "外部依存",
        ),
    ],
)
def test_import_inspector_rejects_unsupported_annotations(
    annotation: object,
    message: str,
) -> None:
    page = dictionary({"/Annots": pikepdf.Array([annotation])})

    with pytest.raises(PdfPageImportError, match=message):
        inspector().validate_page_annotations(page, source_page_objgens=set())


def test_import_inspector_rewrites_supported_annotation_parent() -> None:
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(200, 200))
        annotation = pdf.make_indirect(
            pikepdf.Dictionary(
                {
                    "/Type": pikepdf.Name("/Annot"),
                    "/Subtype": pikepdf.Name("/Text"),
                    "/Rect": pikepdf.Array([10, 10, 20, 20]),
                }
            )
        )
        page.obj["/Annots"] = pdf.make_indirect(pikepdf.Array([annotation]))

        inspector().rewrite_annotation_parents(page)

        assert annotation["/P"].objgen == page.obj.objgen


def test_import_inspector_rejects_non_goto_outline_action() -> None:
    class Item:
        def __init__(self) -> None:
            self.action = pikepdf.Dictionary({"/S": pikepdf.Name("/URI")})
            self.children: list[object] = []

    class OutlineContext:
        def __init__(self) -> None:
            self.root = [Item()]

        def __enter__(self) -> OutlineContext:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Pdf:
        def open_outline(self) -> OutlineContext:
            return OutlineContext()

    with pytest.raises(PdfPageImportError, match="/URI"):
        inspector()._reject_unsupported_outline_actions(Pdf())  # type: ignore[arg-type]
