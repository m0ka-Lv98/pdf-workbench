from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest
from PIL import Image, ImageSequence

from pdf_workbench.domain.image_to_pdf import (
    ImageScalingMode,
    PdfPageSizeMode,
    TransparencyPolicy,
    build_image_to_pdf_plan,
)
from pdf_workbench.services.image_to_pdf import (
    ImageSourceChangedError,
    ImageToPdfCancelled,
    ImageToPdfError,
    ImageToPdfService,
)
from pdf_workbench.services.pdf_save_service import TargetChangedError, TargetSnapshot


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
