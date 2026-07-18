from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pikepdf
from pikepdf import Name, String

from pdf_test_utils import create_blank_pdf
from pdf_workbench.services.pdf_page_mutation import PdfPageMutationService


def create_resource_fixture(
    path: Path,
    *,
    font_name: str = "Helvetica",
    image_rgb: tuple[int, int, int] | None = None,
    share_font_with_first_page: bool = False,
    share_image_with_first_page: bool = False,
    page_count: int = 1,
) -> Path:
    create_blank_pdf(path, page_count)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        shared_font: pikepdf.Dictionary | None = None
        shared_image: pikepdf.Object | None = None
        for page_index, page in enumerate(pdf.pages):
            if share_font_with_first_page and page_index > 0 and shared_font is not None:
                font_object = shared_font
            else:
                font_object = pikepdf.Dictionary(
                    {
                        "/Type": Name("/Font"),
                        "/Subtype": Name("/Type1"),
                        "/BaseFont": Name(f"/{font_name}"),
                    }
                )
                if shared_font is None:
                    shared_font = font_object
            resources = pikepdf.Dictionary(
                {
                    "/Font": pikepdf.Dictionary(
                        {
                            "/F1": font_object,
                        }
                    ),
                    "/XObject": pikepdf.Dictionary(),
                }
            )
            if image_rgb is not None:
                if share_image_with_first_page and page_index > 0 and shared_image is not None:
                    resources["/XObject"]["/Im1"] = shared_image
                else:
                    image_stream = pikepdf.Stream(
                        pdf,
                        bytes(image_rgb),
                        {
                            "/Type": Name("/XObject"),
                            "/Subtype": Name("/Image"),
                            "/Width": 1,
                            "/Height": 1,
                            "/ColorSpace": Name("/DeviceRGB"),
                            "/BitsPerComponent": 8,
                        },
                    )
                    shared_image = pdf.make_indirect(image_stream)
                    resources["/XObject"]["/Im1"] = shared_image
            page.obj["/Resources"] = resources
            page.obj["/Contents"] = pdf.make_indirect(
                pikepdf.Stream(
                    pdf,
                    b"BT /F1 18 Tf 40 100 Td (A) Tj ET",
                )
            )
        pdf.save(path)
    return path


def page_resource_fingerprint(path: Path, page_index: int = 0) -> str:
    service = PdfPageMutationService()
    snapshot = service.snapshot_document_structure(path)
    return snapshot.pages[page_index].resources_fingerprint


def build_normalized_resources(path: Path, page_index: int = 0) -> object:
    service = PdfPageMutationService()
    with pikepdf.open(path) as pdf:
        page = pdf.pages[page_index]
        resources = page.obj.get("/Resources", None)
        if resources is None:
            resources = service._resolve_inherited_value(page.obj, "/Resources")
        return service._normalize_resource_object(resources, memo={}, active=set())


def test_resource_fingerprint_is_stable_across_save_reopen(tmp_path: Path) -> None:
    path = create_resource_fixture(tmp_path / "resource-save.pdf", font_name="Helvetica")
    before = page_resource_fingerprint(path)

    rewritten = tmp_path / "resource-save-rewritten.pdf"
    with pikepdf.open(path) as pdf:
        pdf.save(rewritten)

    after = page_resource_fingerprint(rewritten)

    assert before == after


def test_resource_fingerprint_matches_shared_and_duplicated_font_objects(tmp_path: Path) -> None:
    shared_path = create_resource_fixture(
        tmp_path / "shared.pdf",
        font_name="Helvetica",
        page_count=2,
        share_font_with_first_page=True,
    )
    duplicated_path = create_resource_fixture(
        tmp_path / "duplicated.pdf",
        font_name="Helvetica",
        page_count=2,
        share_font_with_first_page=False,
    )

    assert page_resource_fingerprint(shared_path, 0) == page_resource_fingerprint(
        duplicated_path,
        0,
    )
    assert page_resource_fingerprint(shared_path, 1) == page_resource_fingerprint(
        duplicated_path,
        1,
    )


def test_resource_fingerprint_matches_shared_and_duplicated_xobjects(tmp_path: Path) -> None:
    shared_path = create_resource_fixture(
        tmp_path / "shared-xobject.pdf",
        font_name="Helvetica",
        image_rgb=(255, 0, 0),
        page_count=2,
        share_image_with_first_page=True,
    )
    duplicated_path = create_resource_fixture(
        tmp_path / "duplicated-xobject.pdf",
        font_name="Helvetica",
        image_rgb=(255, 0, 0),
        page_count=2,
        share_image_with_first_page=False,
    )

    assert page_resource_fingerprint(shared_path, 0) == page_resource_fingerprint(
        duplicated_path,
        0,
    )
    assert page_resource_fingerprint(shared_path, 1) == page_resource_fingerprint(
        duplicated_path,
        1,
    )


def test_resource_fingerprint_differs_for_different_fonts(tmp_path: Path) -> None:
    helvetica_path = create_resource_fixture(tmp_path / "helvetica.pdf", font_name="Helvetica")
    courier_path = create_resource_fixture(tmp_path / "courier.pdf", font_name="Courier")

    assert page_resource_fingerprint(helvetica_path) != page_resource_fingerprint(courier_path)


def test_resource_fingerprint_differs_for_different_image_bytes(tmp_path: Path) -> None:
    red_path = create_resource_fixture(
        tmp_path / "red.pdf",
        font_name="Helvetica",
        image_rgb=(255, 0, 0),
    )
    blue_path = create_resource_fixture(
        tmp_path / "blue.pdf",
        font_name="Helvetica",
        image_rgb=(0, 0, 255),
    )

    assert page_resource_fingerprint(red_path) != page_resource_fingerprint(blue_path)


def test_resource_fingerprint_matches_direct_and_inherited_resources(tmp_path: Path) -> None:
    direct_path = create_resource_fixture(tmp_path / "direct.pdf", font_name="Helvetica")
    inherited_path = create_resource_fixture(tmp_path / "inherited.pdf", font_name="Helvetica")

    with pikepdf.open(inherited_path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0].obj
        resources = page["/Resources"]
        del page["/Resources"]
        pages_root = pdf.Root["/Pages"]
        pages_root["/Resources"] = resources
        pdf.save(inherited_path)

    assert page_resource_fingerprint(direct_path) == page_resource_fingerprint(inherited_path)


def test_resource_fingerprint_differs_for_changed_extgstate(tmp_path: Path) -> None:
    low_alpha = create_resource_fixture(tmp_path / "low-alpha.pdf", font_name="Helvetica")
    high_alpha = create_resource_fixture(tmp_path / "high-alpha.pdf", font_name="Helvetica")

    with pikepdf.open(low_alpha, allow_overwriting_input=True) as pdf:
        ext = pdf.pages[0].obj["/Resources"]["/ExtGState"] = pikepdf.Dictionary()
        ext["/GSa"] = pikepdf.Dictionary({"/Type": Name("/ExtGState"), "/CA": Decimal("0.25")})
        pdf.save(low_alpha)
    with pikepdf.open(high_alpha, allow_overwriting_input=True) as pdf:
        ext = pdf.pages[0].obj["/Resources"]["/ExtGState"] = pikepdf.Dictionary()
        ext["/GSa"] = pikepdf.Dictionary({"/Type": Name("/ExtGState"), "/CA": Decimal("0.75")})
        pdf.save(high_alpha)

    assert page_resource_fingerprint(low_alpha) != page_resource_fingerprint(high_alpha)


def test_resource_fingerprint_differs_for_changed_colorspace(tmp_path: Path) -> None:
    rgb_path = create_resource_fixture(tmp_path / "rgb.pdf", font_name="Helvetica")
    gray_path = create_resource_fixture(tmp_path / "gray.pdf", font_name="Helvetica")

    with pikepdf.open(rgb_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Resources"]["/ColorSpace"] = pikepdf.Dictionary(
            {"/CS1": Name("/DeviceRGB")}
        )
        pdf.save(rgb_path)
    with pikepdf.open(gray_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Resources"]["/ColorSpace"] = pikepdf.Dictionary(
            {"/CS1": Name("/DeviceGray")}
        )
        pdf.save(gray_path)

    assert page_resource_fingerprint(rgb_path) != page_resource_fingerprint(gray_path)


def test_resource_fingerprint_differs_for_missing_font_key(tmp_path: Path) -> None:
    present_path = create_resource_fixture(tmp_path / "font-present.pdf", font_name="Helvetica")
    missing_path = create_resource_fixture(tmp_path / "font-missing.pdf", font_name="Helvetica")

    with pikepdf.open(missing_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Resources"]["/Font"] = pikepdf.Dictionary()
        pdf.save(missing_path)

    assert page_resource_fingerprint(present_path) != page_resource_fingerprint(missing_path)


def test_resource_fingerprint_differs_for_changed_resource_dictionary_key(tmp_path: Path) -> None:
    base_path = create_resource_fixture(tmp_path / "base-key.pdf", font_name="Helvetica")
    changed_path = create_resource_fixture(tmp_path / "changed-key.pdf", font_name="Helvetica")

    with pikepdf.open(changed_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Resources"]["/ProcSet"] = pikepdf.Array([Name("/PDF")])
        pdf.save(changed_path)

    assert page_resource_fingerprint(base_path) != page_resource_fingerprint(changed_path)


def test_resource_fingerprint_distinguishes_name_and_literal_string_values(
    tmp_path: Path,
) -> None:
    name_path = create_resource_fixture(tmp_path / "name-value.pdf", font_name="Helvetica")
    string_path = create_resource_fixture(tmp_path / "string-value.pdf", font_name="Helvetica")

    with pikepdf.open(name_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Resources"]["/Properties"] = pikepdf.Dictionary(
            {"/Marker": Name("/Value")}
        )
        pdf.save(name_path)
    with pikepdf.open(string_path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj["/Resources"]["/Properties"] = pikepdf.Dictionary(
            {"/Marker": String("/Value")}
        )
        pdf.save(string_path)

    assert build_normalized_resources(name_path) != build_normalized_resources(string_path)
    assert page_resource_fingerprint(name_path) != page_resource_fingerprint(string_path)


def test_reorder_pages_preserves_semantic_shared_resources_after_save_reopen(
    tmp_path: Path,
) -> None:
    path = create_resource_fixture(
        tmp_path / "reorder-shared.pdf",
        font_name="Helvetica",
        image_rgb=(255, 0, 0),
        page_count=3,
        share_font_with_first_page=True,
        share_image_with_first_page=True,
    )
    service = PdfPageMutationService()
    before = service.snapshot_document_structure(path)

    mutation = service.reorder_pages(path, (0, 2), 3)
    after = service.snapshot_document_structure(path)

    assert after.pages[0].resources_fingerprint == before.pages[1].resources_fingerprint
    assert after.pages[1].resources_fingerprint == before.pages[0].resources_fingerprint
    assert after.pages[2].resources_fingerprint == before.pages[2].resources_fingerprint

    undo_result = service.undo_page_reordering(path, mutation.receipt)
    assert undo_result.page_count == 3
    assert service.snapshot_document_structure(path) == before


def test_normalize_resource_object_is_stable_for_same_page_after_reopen(tmp_path: Path) -> None:
    path = create_resource_fixture(
        tmp_path / "reopen-normalized.pdf",
        font_name="Helvetica",
        image_rgb=(255, 0, 0),
    )
    before = build_normalized_resources(path)

    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.save(path)

    after = build_normalized_resources(path)

    assert before == after
