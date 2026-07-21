from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pikepdf

from pdf_workbench.services.pdf_page_mutation import SUPPORTED_IMPORTED_ANNOTATION_SUBTYPES

PROHIBITED_IMPORTED_ANNOTATION_KEYS = frozenset(
    {
        "/A",
        "/AA",
        "/Dest",
        "/FS",
        "/RichMediaContent",
        "/RichMediaSettings",
        "/3DD",
        "/3DV",
        "/Sound",
        "/Movie",
    }
)
ALLOWED_IMPORTED_PAGE_KEYS = frozenset(
    {
        "/Type",
        "/Parent",
        "/Contents",
        "/Resources",
        "/MediaBox",
        "/CropBox",
        "/TrimBox",
        "/BleedBox",
        "/ArtBox",
        "/Rotate",
        "/Annots",
    }
)


class PdfPageImportError(RuntimeError):
    """Raised when PDF pages are not safe to copy into a new output."""


class PdfPageImportInspector:
    def reject_unsupported_document_structures(
        self,
        pdf: pikepdf.Pdf,
        *,
        inspect_bookmarks: bool,
    ) -> None:
        root = pdf.Root
        if len(pdf.pages) <= 0:
            raise PdfPageImportError("0ページのPDFは結合できません")
        for key, message in (
            ("/AcroForm", "フォームを含むPDFの結合は未対応です"),
            ("/StructTreeRoot", "タグ付きPDFの結合は未対応です"),
            ("/PageLabels", "PageLabelsを含むPDFの結合は未対応です"),
            ("/Threads", "Article Threadsを含むPDFの結合は未対応です"),
            ("/OpenAction", "OpenActionを含むPDFの結合は未対応です"),
        ):
            if key in root:
                raise PdfPageImportError(message)
        self._validate_names_dictionary(pdf, inspect_bookmarks=inspect_bookmarks)
        if inspect_bookmarks:
            self._reject_unsupported_outline_actions(pdf)
        page_objgens = {
            page.obj.objgen
            for page in pdf.pages
            if self._has_indirect_objgen(getattr(page.obj, "objgen", None))
        }
        for page in pdf.pages:
            self.validate_supported_page_keys(page.obj)
            self.validate_page_annotations(page.obj, source_page_objgens=page_objgens)

    @staticmethod
    def validate_supported_page_keys(page_object: pikepdf.Dictionary) -> None:
        for key_object in page_object:
            key = str(key_object)
            if key in ALLOWED_IMPORTED_PAGE_KEYS:
                continue
            if key == "/B":
                raise PdfPageImportError("Article beadを含むページの結合は未対応です")
            if key == "/AA":
                raise PdfPageImportError("ページアクションを含むページの結合は未対応です")
            raise PdfPageImportError(f"結合元PDFのページに未対応の{key}があります")

    def validate_page_annotations(
        self,
        page_object: pikepdf.Dictionary,
        *,
        source_page_objgens: set[tuple[int, int]],
    ) -> None:
        annots_object = page_object.get("/Annots", None)
        if annots_object is None:
            return
        annots = self.dereference(annots_object)
        if not isinstance(annots, pikepdf.Array):
            raise PdfPageImportError("注釈配列が不正です")
        for annot_ref in annots:
            annot = self.dereference(annot_ref)
            if not isinstance(annot, pikepdf.Dictionary):
                raise PdfPageImportError("注釈構造が不正です")
            subtype = str(annot.get("/Subtype", ""))
            if subtype == "/Widget":
                raise PdfPageImportError("Widget注釈を含むPDFの結合は未対応です")
            if subtype not in SUPPORTED_IMPORTED_ANNOTATION_SUBTYPES:
                raise PdfPageImportError(f"{subtype or '不明な'}注釈を含むPDFの結合は未対応です")
            prohibited = next(
                (key for key in PROHIBITED_IMPORTED_ANNOTATION_KEYS if key in annot),
                None,
            )
            if prohibited is not None:
                raise PdfPageImportError("annotation actionまたは外部依存を含む結合は未対応です")
            parent = annot.get("/P", None)
            if parent is not None:
                parent_obj = self.dereference(parent)
                parent_objgen = getattr(parent_obj, "objgen", None)
                own_objgen = getattr(page_object, "objgen", None)
                if (
                    not self._has_indirect_objgen(parent_objgen)
                    or parent_objgen not in source_page_objgens
                    or parent_objgen != own_objgen
                ):
                    raise PdfPageImportError("他ページを参照する注釈を含む結合は未対応です")

    def rewrite_annotation_parents(self, page: pikepdf.Page) -> None:
        annots = page.obj.get("/Annots", None)
        if annots is None:
            return
        annots_array = self.dereference(annots)
        if not isinstance(annots_array, pikepdf.Array):
            raise PdfPageImportError("注釈配列のコピーに失敗しました")
        for annot_ref in annots_array:
            annot = self.dereference(annot_ref)
            if not isinstance(annot, pikepdf.Dictionary):
                raise PdfPageImportError("注釈オブジェクトのコピーに失敗しました")
            annot[pikepdf.Name("/P")] = page.obj

    def _reject_unsupported_outline_actions(self, pdf: pikepdf.Pdf) -> None:
        for item in self._iter_outline_items(pdf):
            action = getattr(item, "action", None)
            if action is not None:
                action_dict = self.dereference(action)
                if not isinstance(action_dict, pikepdf.Dictionary):
                    raise PdfPageImportError("bookmark actionが不正です")
                action_type = str(action_dict.get("/S", ""))
                if action_type and action_type != "/GoTo":
                    raise PdfPageImportError(f"{action_type} bookmark actionは結合未対応です")

    def _iter_outline_items(self, pdf: pikepdf.Pdf) -> Iterable[Any]:
        try:
            with pdf.open_outline() as outline:
                yield from self._walk_outline(outline.root)
        except PdfPageImportError:
            raise
        except Exception as exc:
            raise PdfPageImportError("bookmark構造の検証に失敗しました") from exc

    def _walk_outline(self, items: Iterable[Any]) -> Iterable[Any]:
        for item in items:
            yield item
            children = getattr(item, "children", ())
            if children:
                yield from self._walk_outline(children)

    def _validate_names_dictionary(self, pdf: pikepdf.Pdf, *, inspect_bookmarks: bool) -> None:
        root = pdf.Root
        names_object = root.get("/Names", None)
        if names_object is None:
            return
        names = self.dereference(names_object)
        if not isinstance(names, pikepdf.Dictionary):
            raise PdfPageImportError("Names辞書が不正です")
        allowed_keys = {"/Dests"} if inspect_bookmarks else set()
        for key_object in names:
            key = str(key_object)
            if key in allowed_keys:
                continue
            if key == "/EmbeddedFiles":
                raise PdfPageImportError("添付ファイルを含むPDFの結合は未対応です")
            if key == "/JavaScript":
                raise PdfPageImportError("JavaScriptを含むPDFの結合は未対応です")
            raise PdfPageImportError(f"未対応のNames entry {key} を含むPDFは結合できません")

    @staticmethod
    def dereference(value: Any) -> Any:
        try:
            return value.get_object()
        except (AttributeError, ValueError):
            return value

    @staticmethod
    def _has_indirect_objgen(value: object) -> bool:
        return (
            isinstance(value, tuple)
            and len(value) == 2
            and all(isinstance(item, int) for item in value)
            and value != (0, 0)
        )
