from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pikepdf
import pytest
from PIL import Image, ImageCms, ImageFile, ImageOps, ImageSequence

from pdf_workbench.domain.image_to_pdf import (
    ImageScalingMode,
    PdfOrientation,
    PdfPageSizeMode,
    TransparencyPolicy,
    build_image_to_pdf_plan,
    margins_from_mm,
)
from pdf_workbench.services.image_to_pdf import (
    ImageSourceChangedError,
    ImageToPdfCancelled,
    ImageToPdfError,
    ImageToPdfService,
)
from pdf_workbench.services.pdf_save_service import TargetChangedError, TargetSnapshot


def srgb_profile_bytes() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def image_xobject(path: Path, page_index: int = 0) -> dict[str, Any]:
    with pikepdf.open(path) as pdf:
        image = pdf.pages[page_index].Resources.XObject.Im0
        assert isinstance(image, pikepdf.Stream)
        info: dict[str, Any] = {
            "/ColorSpace": str(image["/ColorSpace"]),
            "/Width": int(image["/Width"]),
            "/Height": int(image["/Height"]),
            "/BitsPerComponent": int(image["/BitsPerComponent"]),
        }
        if "/SMask" in image:
            soft_mask = image["/SMask"]
            assert isinstance(soft_mask, pikepdf.Stream)
            info["/SMask"] = {
                "/ColorSpace": str(soft_mask["/ColorSpace"]),
                "/Width": int(soft_mask["/Width"]),
                "/Height": int(soft_mask["/Height"]),
                "/BitsPerComponent": int(soft_mask["/BitsPerComponent"]),
            }
        return info


def image_bytes(path: Path, page_index: int = 0) -> bytes:
    with pikepdf.open(path) as pdf:
        image = pdf.pages[page_index].Resources.XObject.Im0
        assert isinstance(image, pikepdf.Stream)
        return bytes(image.read_bytes())


def soft_mask_bytes(path: Path, page_index: int = 0) -> bytes:
    with pikepdf.open(path) as pdf:
        image = pdf.pages[page_index].Resources.XObject.Im0
        assert isinstance(image, pikepdf.Stream)
        soft_mask = image["/SMask"]
        assert isinstance(soft_mask, pikepdf.Stream)
        return bytes(soft_mask.read_bytes())


def create_pdf_from_image(
    tmp_path: Path,
    image_path: Path,
    *,
    output_name: str = "out.pdf",
    transparency_policy: TransparencyPolicy = TransparencyPolicy.WHITE_BACKGROUND,
    page_size_mode: PdfPageSizeMode = PdfPageSizeMode.FIT_IMAGE,
    scaling_mode: ImageScalingMode = ImageScalingMode.FIT,
    orientation: PdfOrientation = PdfOrientation.AUTO,
    margins=None,
    custom_page_width_points: float | None = None,
    custom_page_height_points: float | None = None,
) -> Path:
    output = tmp_path / output_name
    service = ImageToPdfService()
    inspected = service.inspect_image_input(image_path)
    plan = build_image_to_pdf_plan(
        (inspected.image_input,),
        output,
        transparency_policy=transparency_policy,
        page_size_mode=page_size_mode,
        scaling_mode=scaling_mode,
        orientation=orientation,
        margins=margins,
        custom_page_width_points=custom_page_width_points,
        custom_page_height_points=custom_page_height_points,
    )
    service.create_pdf(
        plan,
        expected_source_revisions={inspected.image_input.path: inspected.source_revision},
        expected_target_snapshot=TargetSnapshot.capture(output),
        overwrite=False,
    )
    return output


def save_image(path: Path, mode: str = "RGB", *, size: tuple[int, int] = (40, 20)) -> Path:
    image = Image.new(mode, size, 128 if mode == "L" else (200, 10, 20))
    if mode == "RGBA":
        image = Image.new("RGBA", size, (200, 10, 20, 120))
    try:
        image.save(path)
    finally:
        image.close()
    return path


def plan_for(service: ImageToPdfService, paths: tuple[Path, ...], output: Path):
    inspected = tuple(service.inspect_image_input(path) for path in paths)
    return (
        build_image_to_pdf_plan(tuple(item.image_input for item in inspected), output),
        {item.image_input.path: item.source_revision for item in inspected},
    )


@pytest.mark.parametrize(
    ("suffix", "format_name"),
    [
        (".jpg", "JPEG"),
        (".png", "PNG"),
        (".tif", "TIFF"),
        (".bmp", "BMP"),
        (".webp", "WEBP"),
    ],
)
def test_inspects_supported_image_formats(tmp_path: Path, suffix: str, format_name: str) -> None:
    path = save_image(tmp_path / f"input{suffix}")
    inspected = ImageToPdfService().inspect_image_input(path)

    assert inspected.image_input.detected_format == format_name
    assert inspected.image_input.frame_count == 1
    assert inspected.source_revision.sha256


def test_rejects_unsupported_gif_and_animated_webp(tmp_path: Path) -> None:
    gif_path = save_image(tmp_path / "animated.gif")
    frames = [Image.new("RGB", (10, 10), "red"), Image.new("RGB", (10, 10), "blue")]
    webp_path = tmp_path / "animated.webp"
    frames[0].save(webp_path, save_all=True, append_images=frames[1:], duration=10)
    for frame in frames:
        frame.close()

    service = ImageToPdfService()
    with pytest.raises(ImageToPdfError, match="GIF"):
        service.inspect_image_input(gif_path)
    with pytest.raises(ImageToPdfError, match="アニメーション"):
        service.inspect_image_input(webp_path)


def test_multiframe_tiff_becomes_multiple_pages(tmp_path: Path) -> None:
    first = Image.new("RGB", (20, 20), "red")
    second = Image.new("RGB", (20, 20), "green")
    tiff_path = tmp_path / "scan.tiff"
    first.save(tiff_path, save_all=True, append_images=[second])
    first.close()
    second.close()
    output = tmp_path / "out.pdf"
    service = ImageToPdfService()
    plan, revisions = plan_for(service, (tiff_path,), output)

    result = service.create_pdf(
        plan,
        expected_source_revisions=revisions,
        expected_target_snapshot=TargetSnapshot.capture(output),
        overwrite=False,
    )

    assert result.total_page_count == 2
    with pikepdf.open(output) as pdf:
        assert len(pdf.pages) == 2


def test_creates_pdf_from_multiple_reordered_images_and_validates_structure(tmp_path: Path) -> None:
    first = save_image(tmp_path / "first.png", size=(40, 20))
    second = save_image(tmp_path / "second.png", size=(20, 40))
    output = tmp_path / "out.pdf"
    service = ImageToPdfService()
    plan, revisions = plan_for(service, (second, first), output)

    result = service.create_pdf(
        plan,
        expected_source_revisions=revisions,
        expected_target_snapshot=TargetSnapshot.capture(output),
        overwrite=False,
    )

    assert result.total_page_count == 2
    assert tuple(item.label for item in result.inputs) == ("second.png", "first.png")
    with pikepdf.open(output) as pdf:
        assert len(pdf.pages) == 2
        assert "/Annots" not in pdf.pages[0].obj
        assert "/Names" not in pdf.Root


def test_preserve_alpha_writes_soft_mask(tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "alpha.png", mode="RGBA")
    output = tmp_path / "alpha.pdf"
    service = ImageToPdfService()
    inspected = service.inspect_image_input(image_path)
    plan = build_image_to_pdf_plan(
        (inspected.image_input,),
        output,
        transparency_policy=TransparencyPolicy.PRESERVE_ALPHA,
    )

    service.create_pdf(
        plan,
        expected_source_revisions={inspected.image_input.path: inspected.source_revision},
        expected_target_snapshot=TargetSnapshot.capture(output),
        overwrite=False,
    )

    with pikepdf.open(output) as pdf:
        image = pdf.pages[0].Resources.XObject.Im0
        assert "/SMask" in image


def test_white_background_flattens_alpha_without_soft_mask(tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "alpha.png", mode="RGBA")
    output = tmp_path / "flat.pdf"
    service = ImageToPdfService()
    inspected = service.inspect_image_input(image_path)
    plan = build_image_to_pdf_plan(
        (inspected.image_input,),
        output,
        transparency_policy=TransparencyPolicy.WHITE_BACKGROUND,
    )

    service.create_pdf(
        plan,
        expected_source_revisions={inspected.image_input.path: inspected.source_revision},
        expected_target_snapshot=TargetSnapshot.capture(output),
        overwrite=False,
    )

    with pikepdf.open(output) as pdf:
        image = pdf.pages[0].Resources.XObject.Im0
        assert "/SMask" not in image


def test_source_drift_is_rejected_and_existing_target_is_preserved(tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "source.png")
    output = tmp_path / "out.pdf"
    output.write_bytes(b"existing target")
    original_target = output.read_bytes()
    service = ImageToPdfService()
    plan, revisions = plan_for(service, (image_path,), output)
    save_image(image_path, size=(40, 20))

    with pytest.raises(ImageSourceChangedError):
        service.create_pdf(
            plan,
            expected_source_revisions=revisions,
            expected_target_snapshot=TargetSnapshot.capture(output),
            overwrite=True,
        )

    assert output.read_bytes() == original_target
    assert not list(tmp_path.glob("*.image-to-pdf.tmp.pdf"))


def test_target_snapshot_drift_is_rejected(tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "source.png")
    output = tmp_path / "out.pdf"
    service = ImageToPdfService()
    plan, revisions = plan_for(service, (image_path,), output)
    snapshot = TargetSnapshot.capture(output)
    output.write_bytes(b"late target")

    with pytest.raises(TargetChangedError):
        service.create_pdf(
            plan,
            expected_source_revisions=revisions,
            expected_target_snapshot=snapshot,
            overwrite=True,
        )

    assert output.read_bytes() == b"late target"


def test_cancel_before_processing_preserves_target_and_cleans_candidate(tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "source.png")
    output = tmp_path / "out.pdf"
    service = ImageToPdfService()
    plan, revisions = plan_for(service, (image_path,), output)

    with pytest.raises(ImageToPdfCancelled):
        service.create_pdf(
            plan,
            expected_source_revisions=revisions,
            expected_target_snapshot=TargetSnapshot.capture(output),
            overwrite=False,
            should_cancel=lambda: True,
        )

    assert not output.exists()
    assert not list(tmp_path.glob("*.image-to-pdf.tmp.pdf"))


def test_actual_size_failure_preserves_target(tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "source.png", size=(5000, 5000))
    output = tmp_path / "out.pdf"
    service = ImageToPdfService()
    inspected = service.inspect_image_input(image_path)
    plan = build_image_to_pdf_plan(
        (inspected.image_input,),
        output,
        page_size_mode=PdfPageSizeMode.A4,
        scaling_mode=ImageScalingMode.ACTUAL_SIZE,
    )

    with pytest.raises(ImageToPdfError, match="actual-size"):
        service.create_pdf(
            plan,
            expected_source_revisions={inspected.image_input.path: inspected.source_revision},
            expected_target_snapshot=TargetSnapshot.capture(output),
            overwrite=False,
        )

    assert not output.exists()


def test_multiframe_processing_uses_one_source_and_frame_at_a_time(tmp_path: Path) -> None:
    first = Image.new("RGB", (10, 10), "red")
    second = Image.new("RGB", (10, 10), "green")
    tiff_path = tmp_path / "scan.tiff"
    first.save(tiff_path, save_all=True, append_images=[second])
    first.close()
    second.close()

    service = ImageToPdfService()
    inspected = service.inspect_image_input(tiff_path)
    seen_frames = [frame.copy() for frame in ImageSequence.Iterator(Image.open(tiff_path))]
    for frame in seen_frames:
        frame.close()

    plan = build_image_to_pdf_plan((inspected.image_input,), tmp_path / "out.pdf")
    service.create_pdf(
        plan,
        expected_source_revisions={inspected.image_input.path: inspected.source_revision},
        expected_target_snapshot=TargetSnapshot.capture(plan.output_path),
        overwrite=False,
    )

    assert plan.total_page_count == 2


def test_cmyk_without_icc_is_rejected(tmp_path: Path) -> None:
    image = Image.new("CMYK", (4, 1), (0, 255, 255, 0))
    image_path = tmp_path / "cmyk.tif"
    image.save(image_path)
    image.close()

    output = tmp_path / "out.pdf"
    service = ImageToPdfService()
    inspected = service.inspect_image_input(image_path)
    plan = build_image_to_pdf_plan((inspected.image_input,), output)

    with pytest.raises(ImageToPdfError, match="CMYK"):
        service.create_pdf(
            plan,
            expected_source_revisions={inspected.image_input.path: inspected.source_revision},
            expected_target_snapshot=TargetSnapshot.capture(output),
            overwrite=False,
        )


def test_invalid_cmyk_icc_is_rejected(tmp_path: Path) -> None:
    image = Image.new("CMYK", (4, 1), (0, 255, 255, 0))
    image.info["icc_profile"] = b"not an icc profile"
    image_path = tmp_path / "cmyk.tif"
    image.save(image_path, icc_profile=b"not an icc profile")
    image.close()

    output = tmp_path / "out.pdf"
    service = ImageToPdfService()
    inspected = service.inspect_image_input(image_path)
    plan = build_image_to_pdf_plan((inspected.image_input,), output)

    with pytest.raises(ImageToPdfError, match="ICC"):
        service.create_pdf(
            plan,
            expected_source_revisions={inspected.image_input.path: inspected.source_revision},
            expected_target_snapshot=TargetSnapshot.capture(output),
            overwrite=False,
        )


def test_icc_cmyk_is_converted_to_rgb_not_grayscale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = Image.new("CMYK", (3, 1))
    image.putdata([(0, 255, 255, 0), (255, 0, 255, 0), (255, 255, 0, 0)])
    image_path = tmp_path / "cmyk.tif"
    image.save(image_path, icc_profile=srgb_profile_bytes())
    image.close()

    def fake_cmyk_to_rgb(self: ImageToPdfService, source: Image.Image) -> Image.Image:
        assert source.mode == "CMYK"
        converted = Image.new("RGB", source.size)
        converted.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        return converted

    monkeypatch.setattr(ImageToPdfService, "_convert_cmyk_to_srgb", fake_cmyk_to_rgb)

    output = create_pdf_from_image(tmp_path, image_path)
    image = image_xobject(output)

    assert str(image["/ColorSpace"]) == "/DeviceRGB"
    assert image_bytes(output) == bytes((255, 0, 0, 0, 255, 0, 0, 0, 255))


def test_icc_rgba_preserve_alpha_writes_soft_mask(tmp_path: Path) -> None:
    image = Image.new("RGBA", (3, 1))
    image.putdata([(255, 0, 0, 0), (0, 255, 0, 127), (0, 0, 255, 255)])
    image_path = tmp_path / "icc-alpha.png"
    image.save(image_path, icc_profile=srgb_profile_bytes())
    image.close()

    output = create_pdf_from_image(
        tmp_path,
        image_path,
        transparency_policy=TransparencyPolicy.PRESERVE_ALPHA,
    )
    image = image_xobject(output)

    assert str(image["/ColorSpace"]) == "/DeviceRGB"
    assert "/SMask" in image
    assert soft_mask_bytes(output) == bytes((0, 127, 255))


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (TransparencyPolicy.WHITE_BACKGROUND, (255, 255, 255, 127, 127, 255)),
        (TransparencyPolicy.BLACK_BACKGROUND, (0, 0, 0, 0, 0, 128)),
    ],
)
def test_icc_rgba_flattens_after_color_conversion(
    tmp_path: Path,
    policy: TransparencyPolicy,
    expected: tuple[int, ...],
) -> None:
    image = Image.new("RGBA", (2, 1))
    image.putdata([(200, 0, 0, 0), (0, 0, 255, 128)])
    image_path = tmp_path / f"{policy.value}.png"
    image.save(image_path, icc_profile=srgb_profile_bytes())
    image.close()

    output = create_pdf_from_image(tmp_path, image_path, transparency_policy=policy)

    assert "/SMask" not in image_xobject(output)
    assert image_bytes(output) == bytes(expected)


def test_la_preserve_alpha_and_dimensions_match(tmp_path: Path) -> None:
    image = Image.new("LA", (3, 1))
    image.putdata([(10, 0), (128, 128), (250, 255)])
    image_path = tmp_path / "luminance-alpha.png"
    image.save(image_path)
    image.close()

    output = create_pdf_from_image(
        tmp_path,
        image_path,
        transparency_policy=TransparencyPolicy.PRESERVE_ALPHA,
    )
    image = image_xobject(output)
    soft_mask = image["/SMask"]

    assert str(image["/ColorSpace"]) == "/DeviceGray"
    assert int(image["/Width"]) == int(soft_mask["/Width"]) == 3
    assert int(image["/Height"]) == int(soft_mask["/Height"]) == 1
    assert image_bytes(output) == bytes((10, 128, 250))
    assert soft_mask_bytes(output) == bytes((0, 128, 255))


def test_palette_transparency_table_is_preserved_as_soft_mask(tmp_path: Path) -> None:
    image = Image.new("P", (3, 1))
    image.putpalette([255, 0, 0, 0, 255, 0, 0, 0, 255])
    image.putdata([0, 1, 2])
    image.info["transparency"] = bytes((0, 128, 255))
    image_path = tmp_path / "palette.png"
    image.save(image_path, transparency=image.info["transparency"])
    image.close()

    output = create_pdf_from_image(
        tmp_path,
        image_path,
        transparency_policy=TransparencyPolicy.PRESERVE_ALPHA,
    )

    assert "/SMask" in image_xobject(output)
    assert soft_mask_bytes(output) == bytes((0, 128, 255))


def test_16bit_grayscale_scales_full_range_to_8bit(tmp_path: Path) -> None:
    image = Image.new("I;16", (3, 1))
    image.putdata([0, 32768, 65535])
    image_path = tmp_path / "depth16.tif"
    image.save(image_path)
    image.close()

    output = create_pdf_from_image(tmp_path, image_path)

    assert image_bytes(output) == bytes((0, 128, 255))


def test_16bit_big_endian_and_little_endian_match(tmp_path: Path) -> None:
    little = Image.new("I;16L", (2, 1))
    little.putdata([0, 65535])
    big = Image.new("I;16B", (2, 1))
    big.putdata([0, 65535])
    little_path = tmp_path / "little.tif"
    big_path = tmp_path / "big.tif"
    little.save(little_path)
    big.save(big_path)
    little.close()
    big.close()

    little_output = create_pdf_from_image(tmp_path, little_path, output_name="little.pdf")
    big_output = create_pdf_from_image(tmp_path, big_path, output_name="big.pdf")

    assert image_bytes(little_output) == image_bytes(big_output) == bytes((0, 255))


def test_float_image_rejects_nan_and_infinity(tmp_path: Path) -> None:
    service = ImageToPdfService()
    for filename, value in (
        ("nan.tif", math.nan),
        ("positive-inf.tif", math.inf),
        ("negative-inf.tif", -math.inf),
    ):
        image = Image.new("F", (2, 1))
        image.putdata([0.0, value])
        image_path = tmp_path / filename
        image.save(image_path)
        image.close()
        inspected = service.inspect_image_input(image_path)
        plan = build_image_to_pdf_plan((inspected.image_input,), tmp_path / f"{filename}.pdf")

        with pytest.raises(ImageToPdfError, match=r"floating|高ビット"):
            service.create_pdf(
                plan,
                expected_source_revisions={inspected.image_input.path: inspected.source_revision},
                expected_target_snapshot=TargetSnapshot.capture(plan.output_path),
                overwrite=False,
            )


def test_float_image_normalization_and_constant_is_deterministic(tmp_path: Path) -> None:
    image = Image.new("F", (3, 1))
    image.putdata([0.0, 0.5, 1.0])
    image_path = tmp_path / "float.tif"
    image.save(image_path)
    image.close()
    output = create_pdf_from_image(tmp_path, image_path)

    constant = Image.new("F", (2, 1), 12.0)
    constant_path = tmp_path / "constant.tif"
    constant.save(constant_path)
    constant.close()
    constant_output = create_pdf_from_image(tmp_path, constant_path, output_name="constant.pdf")

    assert image_bytes(output) == bytes((0, 128, 255))
    assert image_bytes(constant_output) == bytes((255, 255))


@pytest.mark.parametrize("orientation", range(1, 9))
def test_exif_orientation_1_through_8_normalizes_output_pixels(
    tmp_path: Path,
    orientation: int,
) -> None:
    image = Image.new("RGB", (2, 3))
    image.putdata(
        [
            (10, 0, 0),
            (20, 0, 0),
            (30, 0, 0),
            (40, 0, 0),
            (50, 0, 0),
            (60, 0, 0),
        ]
    )
    exif = Image.Exif()
    exif[274] = orientation
    image_path = tmp_path / f"orientation-{orientation}.jpg"
    image.save(image_path, exif=exif)
    expected = Image.open(image_path)
    try:
        expected = ImageOps.exif_transpose(expected)
        expected_bytes = expected.convert("RGB").tobytes()
        expected_size = expected.size
    finally:
        expected.close()
        image.close()

    output = create_pdf_from_image(
        tmp_path,
        image_path,
        output_name=f"orientation-{orientation}.pdf",
    )
    xobject = image_xobject(output)

    assert (int(xobject["/Width"]), int(xobject["/Height"])) == expected_size
    assert image_bytes(output) == expected_bytes


def test_invalid_exif_orientation_is_rejected(tmp_path: Path) -> None:
    image = Image.new("RGB", (2, 2), "red")
    exif = Image.Exif()
    exif[274] = 9
    image_path = tmp_path / "invalid-exif.jpg"
    image.save(image_path, exif=exif)
    image.close()

    with pytest.raises(ImageToPdfError, match="EXIF"):
        ImageToPdfService().inspect_image_input(image_path)


def test_fill_landscape_image_center_crops_without_distortion(tmp_path: Path) -> None:
    image = Image.new("RGB", (5, 1))
    image.putdata([(255, 0, 0), (200, 0, 0), (0, 255, 0), (0, 0, 200), (0, 0, 255)])
    image_path = tmp_path / "wide.png"
    image.save(image_path)
    image.close()

    output = create_pdf_from_image(
        tmp_path,
        image_path,
        page_size_mode=PdfPageSizeMode.CUSTOM,
        scaling_mode=ImageScalingMode.FILL,
        margins=margins_from_mm(0, 0, 0, 0),
        custom_page_width_points=30.0,
        custom_page_height_points=10.0,
    )

    assert image_bytes(output) == bytes((200, 0, 0, 0, 255, 0, 0, 0, 200))


def test_fill_crop_preserves_alpha_alignment(tmp_path: Path) -> None:
    image = Image.new("RGBA", (5, 1))
    image.putdata(
        [
            (255, 0, 0, 0),
            (255, 0, 0, 64),
            (0, 255, 0, 128),
            (0, 0, 255, 192),
            (0, 0, 255, 255),
        ]
    )
    image_path = tmp_path / "wide-alpha.png"
    image.save(image_path)
    image.close()

    output = create_pdf_from_image(
        tmp_path,
        image_path,
        page_size_mode=PdfPageSizeMode.CUSTOM,
        scaling_mode=ImageScalingMode.FILL,
        transparency_policy=TransparencyPolicy.PRESERVE_ALPHA,
        margins=margins_from_mm(0, 0, 0, 0),
        custom_page_width_points=30.0,
        custom_page_height_points=10.0,
    )

    assert image_bytes(output) == bytes((255, 0, 0, 0, 255, 0, 0, 0, 255))
    assert soft_mask_bytes(output) == bytes((64, 128, 192))


def test_page_size_orientation_margin_service_geometry(tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "geometry.png", size=(96, 48))
    output = create_pdf_from_image(
        tmp_path,
        image_path,
        page_size_mode=PdfPageSizeMode.A4,
        orientation=PdfOrientation.LANDSCAPE,
        margins=margins_from_mm(10, 20, 30, 40),
    )

    with pikepdf.open(output) as pdf:
        page = pdf.pages[0]
        media_box = tuple(float(value) for value in page.obj["/MediaBox"])
        content = bytes(page.obj["/Contents"].read_bytes()).decode("ascii")
        assert "/CropBox" not in page.obj

    assert media_box[2] > media_box[3]
    assert " cm" in content


def test_image_inspection_does_not_change_load_truncated_images_global(tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "source.png")
    previous = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        ImageToPdfService().inspect_image_input(image_path)
        assert ImageFile.LOAD_TRUNCATED_IMAGES is True
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous


def test_truncated_image_is_rejected(tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "truncated.png")
    image_path.write_bytes(image_path.read_bytes()[:12])

    with pytest.raises(ImageToPdfError, match="開けません"):
        ImageToPdfService().inspect_image_input(image_path)


def test_decompression_bomb_warning_is_rejected_without_large_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = save_image(tmp_path / "tiny.png", size=(2, 2))
    previous = Image.MAX_IMAGE_PIXELS
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    try:
        with pytest.raises(ImageToPdfError, match="大きすぎ"):
            ImageToPdfService().inspect_image_input(image_path)
    finally:
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", previous)


def test_extension_and_detected_format_policy(tmp_path: Path) -> None:
    jpg = save_image(tmp_path / "photo.JPG")
    png_named_jpeg = tmp_path / "actually-jpeg.png"
    Image.new("RGB", (2, 2), "red").save(png_named_jpeg, format="JPEG")
    unsupported_suffix = tmp_path / "image.dat"
    Image.new("RGB", (2, 2), "red").save(unsupported_suffix, format="PNG")
    gif_named_png = tmp_path / "fake.gif"
    Image.new("RGB", (2, 2), "red").save(gif_named_png, format="PNG")

    service = ImageToPdfService()

    assert service.inspect_image_input(jpg).image_input.detected_format == "JPEG"
    assert service.inspect_image_input(png_named_jpeg).image_input.detected_format == "JPEG"
    assert service.inspect_image_input(gif_named_png).image_input.detected_format == "PNG"
    with pytest.raises(ImageToPdfError, match="対応していない"):
        service.inspect_image_input(unsupported_suffix)


def test_multiframe_tiff_uses_each_frames_dimensions_and_order(tmp_path: Path) -> None:
    first = Image.new("RGB", (2, 1), "red")
    second = Image.new("RGB", (1, 3), "blue")
    tiff_path = tmp_path / "variable.tiff"
    first.save(tiff_path, save_all=True, append_images=[second])
    first.close()
    second.close()

    output = create_pdf_from_image(tmp_path, tiff_path)

    first_image = image_xobject(output, 0)
    second_image = image_xobject(output, 1)
    assert (int(first_image["/Width"]), int(first_image["/Height"])) == (2, 1)
    assert (int(second_image["/Width"]), int(second_image["/Height"])) == (1, 3)
    assert image_bytes(output, 0) == bytes((255, 0, 0, 255, 0, 0))
    assert image_bytes(output, 1) == bytes((0, 0, 255) * 3)


def test_resource_tracker_stays_bounded_for_many_inputs(tmp_path: Path) -> None:
    paths = tuple(save_image(tmp_path / f"image-{index:03}.png") for index in range(105))
    output = tmp_path / "many.pdf"
    current: dict[str, int] = {}
    maximum: dict[str, int] = {}
    sequence: list[str] = []

    def tracker(name: str, delta: int) -> None:
        current[name] = current.get(name, 0) + delta
        maximum[name] = max(maximum.get(name, 0), current[name])
        if name == "decoded_frame" and delta > 0:
            sequence.append(name)

    service = ImageToPdfService(resource_tracker=tracker)
    plan, revisions = plan_for(service, paths, output)

    service.create_pdf(
        plan,
        expected_source_revisions=revisions,
        expected_target_snapshot=TargetSnapshot.capture(output),
        overwrite=False,
    )

    assert maximum["source_file"] == 1
    assert maximum["decoded_frame"] == 1
    assert maximum["color_image"] == 1
    assert len(sequence) == 105
    assert all(value == 0 for value in current.values())


def test_expected_revision_mapping_extra_and_missing_are_rejected(tmp_path: Path) -> None:
    image_path = save_image(tmp_path / "source.png")
    extra_path = save_image(tmp_path / "extra.png")
    output = tmp_path / "out.pdf"
    service = ImageToPdfService()
    plan, revisions = plan_for(service, (image_path,), output)
    extra = service.inspect_image_input(extra_path)

    with pytest.raises(ImageToPdfError, match="不足"):
        service.create_pdf(
            plan,
            expected_source_revisions={},
            expected_target_snapshot=TargetSnapshot.capture(output),
            overwrite=False,
        )

    revisions[extra.image_input.path] = extra.source_revision
    with pytest.raises(ImageToPdfError, match="余分"):
        service.create_pdf(
            plan,
            expected_source_revisions=revisions,
            expected_target_snapshot=TargetSnapshot.capture(output),
            overwrite=False,
        )


def test_validation_failure_preserves_existing_target(tmp_path: Path) -> None:
    class RejectingValidator:
        def validate(self, *_args: Any, **_kwargs: Any) -> None:
            raise ImageToPdfError("forced validation failure")

    image_path = save_image(tmp_path / "source.png")
    output = tmp_path / "out.pdf"
    output.write_bytes(b"existing")
    service = ImageToPdfService(validator=RejectingValidator())  # type: ignore[arg-type]
    plan, revisions = plan_for(service, (image_path,), output)

    with pytest.raises(ImageToPdfError, match="forced"):
        service.create_pdf(
            plan,
            expected_source_revisions=revisions,
            expected_target_snapshot=TargetSnapshot.capture(output),
            overwrite=True,
        )

    assert output.read_bytes() == b"existing"
    assert not list(tmp_path.glob("*.image-to-pdf.tmp.pdf"))
