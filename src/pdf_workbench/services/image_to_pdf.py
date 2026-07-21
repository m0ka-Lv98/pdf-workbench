from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import stat
import tempfile
import warnings
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

import pikepdf
from PIL import Image, ImageCms, ImageFile, ImageOps, ImageSequence, UnidentifiedImageError

from pdf_workbench.domain.document_session import FileFingerprint
from pdf_workbench.domain.image_to_pdf import (
    ImageFrameRef,
    ImageInput,
    ImageScalingMode,
    ImageSourceRevision,
    ImageToPdfPlan,
    PageGeometry,
    PdfPageSizeMode,
    TransparencyPolicy,
    build_page_geometry,
)
from pdf_workbench.services.pdf_document_validator import (
    PdfDocumentValidationError,
    PdfDocumentValidator,
)
from pdf_workbench.services.pdf_save_service import TargetChangedError, TargetSnapshot

logger = logging.getLogger(__name__)

SUPPORTED_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "TIFF", "BMP", "WEBP"})
SUPPORTED_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
)
_DISALLOWED_ROOT_KEYS = (
    "/Names",
    "/AcroForm",
    "/PageLabels",
    "/Threads",
    "/OpenAction",
    "/StructTreeRoot",
    "/Metadata",
)


class ImageToPdfStage(StrEnum):
    VALIDATING = "validating"
    DECODING = "decoding"
    COLOR_CONVERSION = "color_conversion"
    ENCODING = "encoding"
    WRITING_PAGE = "writing_page"
    VALIDATING_OUTPUT = "validating_output"
    REPLACING = "replacing"
    COMPLETE = "complete"


class ImageToPdfError(RuntimeError):
    """Raised when Image-to-PDF cannot complete safely."""


class ImageToPdfCancelled(ImageToPdfError):
    """Raised when the user cancels Image-to-PDF creation."""


class ImageSourceChangedError(ImageToPdfError):
    """Raised when one image source changes while creating the PDF."""


@dataclass(frozen=True, slots=True)
class ImageToPdfProgress:
    stage: ImageToPdfStage
    input_number: int
    input_count: int
    filename: str
    frame_number: int
    frame_count: int
    completed_pages: int
    total_pages: int
    message: str


@dataclass(frozen=True, slots=True)
class ImageToPdfResultInput:
    label: str
    path: Path
    frame_count: int
    display_range: str


@dataclass(frozen=True, slots=True)
class ImageToPdfResult:
    target_path: Path
    fingerprint: FileFingerprint
    input_count: int
    total_page_count: int
    page_size_mode: PdfPageSizeMode
    scaling_mode: ImageScalingMode
    transparency_policy: TransparencyPolicy
    inputs: tuple[ImageToPdfResultInput, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class InspectedImageInput:
    image_input: ImageInput
    source_revision: ImageSourceRevision


@dataclass(frozen=True, slots=True)
class EncodedFrame:
    width: int
    height: int
    color_bytes: bytes
    color_space: pikepdf.Name
    bits_per_component: int
    alpha_bytes: bytes | None
    geometry: PageGeometry


@dataclass(frozen=True, slots=True)
class ExpectedImagePage:
    output_page_index: int
    source_path: Path
    frame_index: int
    media_box: tuple[float, float, float, float]
    image_matrix: tuple[float, float, float, float, float, float]
    image_pixel_width: int
    image_pixel_height: int
    color_space: str
    has_soft_mask: bool


class ImageToPdfService:
    def __init__(
        self,
        *,
        validator: PdfDocumentValidator | None = None,
        resource_tracker: Callable[[str, int], None] | None = None,
    ) -> None:
        self._validator = validator if validator is not None else PdfDocumentValidator()
        self._resource_tracker = resource_tracker

    def inspect_image_input(self, path: Path) -> InspectedImageInput:
        resolved_path = path.expanduser().resolve()
        self._validate_input_path(resolved_path)
        with self._open_image(resolved_path) as image:
            detected_format = str(image.format or "").upper()
            self._validate_detected_format(image, detected_format)
            frame_count = int(getattr(image, "n_frames", 1))
            image.seek(0)
            width, height = self._exif_normalized_size(image)
            mode = str(image.mode)
            exif_orientation = self._read_exif_orientation(image)
            revision = self.read_source_revision(
                resolved_path,
                detected_format=detected_format,
                frame_count=frame_count,
                pixel_width=width,
                pixel_height=height,
            )
            return InspectedImageInput(
                image_input=ImageInput(
                    path=resolved_path,
                    label=resolved_path.name,
                    detected_format=detected_format,
                    pixel_width=width,
                    pixel_height=height,
                    frame_count=frame_count,
                    color_mode=mode,
                    has_alpha=self._has_alpha(image),
                    has_icc_profile=bool(image.info.get("icc_profile")),
                    exif_orientation=exif_orientation,
                ),
                source_revision=revision,
            )

    def read_source_revision(
        self,
        path: Path,
        *,
        detected_format: str | None = None,
        frame_count: int | None = None,
        pixel_width: int | None = None,
        pixel_height: int | None = None,
    ) -> ImageSourceRevision:
        resolved_path = path.expanduser().resolve()
        stat_result = resolved_path.stat()
        if (
            detected_format is None
            or frame_count is None
            or pixel_width is None
            or pixel_height is None
        ):
            inspected = self.inspect_image_input(resolved_path)
            return inspected.source_revision
        return ImageSourceRevision(
            resolved_path=resolved_path,
            size_bytes=stat_result.st_size,
            modified_time_ns=stat_result.st_mtime_ns,
            sha256=self._sha256(resolved_path),
            detected_format=detected_format,
            frame_count=frame_count,
            pixel_width=pixel_width,
            pixel_height=pixel_height,
        )

    def create_pdf(
        self,
        plan: ImageToPdfPlan,
        *,
        expected_source_revisions: Mapping[Path, ImageSourceRevision],
        expected_target_snapshot: TargetSnapshot,
        overwrite: bool,
        is_managed_path: Callable[[Path], bool] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        progress_callback: Callable[[ImageToPdfProgress], None] | None = None,
    ) -> ImageToPdfResult:
        candidate_path: Path | None = None
        primary_error: BaseException | None = None
        expected_pages: list[ExpectedImagePage] = []
        created_at = datetime.now(UTC)
        try:
            self._check_cancelled(should_cancel)
            self._preflight(plan, overwrite=overwrite, is_managed_path=is_managed_path)
            source_revisions = self._read_and_validate_source_revisions(
                plan,
                expected_source_revisions,
            )
            self._ensure_target_policy(
                plan.output_path,
                expected_target_snapshot,
                overwrite=overwrite,
            )
            self._ensure_target_snapshot_matches(plan.output_path, expected_target_snapshot)
            candidate_path = self._create_temp_output_path(plan.output_path)
            completed_pages = 0
            self._emit_progress(
                progress_callback,
                plan,
                ImageToPdfStage.VALIDATING,
                plan.frame_mapping[0],
                completed_pages=0,
            )
            with pikepdf.Pdf.new() as output_pdf:
                for image_input in plan.inputs:
                    self._check_cancelled(should_cancel)
                    self._ensure_source_revision_unchanged(
                        image_input.path,
                        source_revisions[image_input.path],
                    )
                    with (
                        self._tracked_resource("source_file"),
                        self._open_image(image_input.path) as image,
                    ):
                        self._validate_runtime_image_identity(image, image_input)
                        for frame_index in range(image_input.frame_count):
                            self._check_cancelled(should_cancel)
                            image.seek(frame_index)
                            mapping = plan.frame_mapping[completed_pages]
                            self._emit_progress(
                                progress_callback,
                                plan,
                                ImageToPdfStage.DECODING,
                                mapping,
                                completed_pages=completed_pages,
                            )
                            with self._tracked_resource("decoded_frame"):
                                frame = ImageSequence.Iterator(image)[frame_index]
                                encoded_frame = self._prepare_frame(frame, plan=plan)
                            self._emit_progress(
                                progress_callback,
                                plan,
                                ImageToPdfStage.WRITING_PAGE,
                                mapping,
                                completed_pages=completed_pages,
                            )
                            expected_pages.append(
                                self._append_page(
                                    output_pdf,
                                    encoded_frame,
                                    mapping=mapping,
                                )
                            )
                            completed_pages += 1
                            self._check_cancelled(should_cancel)
                            self._ensure_source_revision_unchanged(
                                image_input.path,
                                source_revisions[image_input.path],
                            )
                self._check_cancelled(should_cancel)
                output_pdf.save(candidate_path, compress_streams=True)
            self._fsync_file(candidate_path)
            self._emit_progress(
                progress_callback,
                plan,
                ImageToPdfStage.VALIDATING_OUTPUT,
                plan.frame_mapping[-1],
                completed_pages=plan.total_page_count,
            )
            self._validate_candidate(
                plan,
                candidate_path,
                expected_pages=tuple(expected_pages),
            )
            for image_input in plan.inputs:
                self._ensure_source_revision_unchanged(
                    image_input.path,
                    source_revisions[image_input.path],
                )
            self._check_cancelled(should_cancel)
            self._ensure_target_snapshot_matches(plan.output_path, expected_target_snapshot)
            self._emit_progress(
                progress_callback,
                plan,
                ImageToPdfStage.REPLACING,
                plan.frame_mapping[-1],
                completed_pages=plan.total_page_count,
            )
            self._apply_existing_target_mode(candidate_path, plan.output_path)
            self._ensure_target_snapshot_matches(plan.output_path, expected_target_snapshot)
            fingerprint = self._build_fingerprint(candidate_path)
            self._replace_atomically(candidate_path, plan.output_path)
            self._fsync_parent_directory(plan.output_path.parent)
            candidate_path = None
            self._emit_progress(
                progress_callback,
                plan,
                ImageToPdfStage.COMPLETE,
                plan.frame_mapping[-1],
                completed_pages=plan.total_page_count,
            )
            return ImageToPdfResult(
                target_path=plan.output_path,
                fingerprint=fingerprint,
                input_count=len(plan.inputs),
                total_page_count=plan.total_page_count,
                page_size_mode=plan.page_size_mode,
                scaling_mode=plan.scaling_mode,
                transparency_policy=plan.transparency_policy,
                inputs=self._result_inputs(plan),
                created_at=created_at,
            )
        except (ImageToPdfError, TargetChangedError) as exc:
            primary_error = exc
            raise
        except OSError as exc:
            primary_error = exc
            raise ImageToPdfError("画像PDFの保存準備に失敗しました") from exc
        finally:
            self._cleanup_candidate(candidate_path, primary_error=primary_error)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _validate_input_path(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            raise ImageToPdfError("画像ファイルが存在しません")
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ImageToPdfError("対応していない画像形式です")
        if not os.access(path, os.R_OK):
            raise ImageToPdfError("画像ファイルを読み取れません")

    def _open_image(self, path: Path) -> Image.Image:
        previous_load_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                ImageFile.LOAD_TRUNCATED_IMAGES = False
                image = Image.open(path)
                image.verify()
            image = Image.open(path)
            image.load()
            return image
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            raise ImageToPdfError("画像が大きすぎるため安全に処理できません") from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ImageToPdfError("画像ファイルを開けません") from exc
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = previous_load_truncated

    def _validate_detected_format(self, image: Image.Image, detected_format: str) -> None:
        if detected_format == "GIF":
            raise ImageToPdfError("GIF画像は初期実装では未対応です")
        if detected_format not in SUPPORTED_IMAGE_FORMATS:
            raise ImageToPdfError("対応していない画像形式です")
        if detected_format in {"WEBP", "PNG"} and bool(getattr(image, "is_animated", False)):
            raise ImageToPdfError("アニメーション画像は未対応です")

    def _read_and_validate_source_revisions(
        self,
        plan: ImageToPdfPlan,
        expected_source_revisions: Mapping[Path, ImageSourceRevision],
    ) -> dict[Path, ImageSourceRevision]:
        expected_by_path = {
            path.expanduser().resolve(): revision
            for path, revision in expected_source_revisions.items()
        }
        plan_paths = {item.path for item in plan.inputs}
        missing = plan_paths - set(expected_by_path)
        extra = set(expected_by_path) - plan_paths
        if missing:
            raise ImageToPdfError("画像入力の期待revisionが不足しています")
        if extra:
            raise ImageToPdfError("画像入力の期待revisionが余分です")
        revisions: dict[Path, ImageSourceRevision] = {}
        for item in plan.inputs:
            current = self.inspect_image_input(item.path).source_revision
            expected = expected_by_path[item.path]
            if current != expected:
                raise ImageSourceChangedError(f"{item.label} が変更されたため作成を中止しました")
            revisions[item.path] = current
        return revisions

    def _ensure_source_revision_unchanged(
        self,
        path: Path,
        expected_revision: ImageSourceRevision,
    ) -> None:
        current = self.inspect_image_input(path).source_revision
        if current != expected_revision:
            raise ImageSourceChangedError(f"{path.name} が作成中に変更されました")

    def _preflight(
        self,
        plan: ImageToPdfPlan,
        *,
        overwrite: bool,
        is_managed_path: Callable[[Path], bool] | None,
    ) -> None:
        output_parent = plan.output_path.parent
        if not output_parent.exists():
            raise ImageToPdfError("出力先フォルダが存在しません")
        if not output_parent.is_dir():
            raise ImageToPdfError("出力先フォルダが不正です")
        if not os.access(output_parent, os.W_OK):
            raise ImageToPdfError("出力先フォルダに書き込めません")
        if is_managed_path is not None and is_managed_path(plan.output_path):
            raise ImageToPdfError("アプリの一時作業フォルダ内には出力できません")
        for image_input in plan.inputs:
            self._validate_input_path(image_input.path)
            if is_managed_path is not None and is_managed_path(image_input.path):
                raise ImageToPdfError("アプリの一時作業フォルダ内の画像は入力にできません")
            if image_input.path == plan.output_path:
                raise ImageToPdfError("出力PDFと同じ場所の画像は入力にできません")
        if plan.output_path.exists() and not overwrite:
            raise ImageToPdfError("出力先PDFが既に存在します")

    @staticmethod
    def _ensure_target_policy(
        target_path: Path,
        target_snapshot: TargetSnapshot,
        *,
        overwrite: bool,
    ) -> None:
        if target_snapshot.exists and not overwrite:
            raise ImageToPdfError("出力先PDFが既に存在します")
        if target_path.exists() != target_snapshot.exists:
            raise TargetChangedError("出力先の状態が変更されました")

    @staticmethod
    def _ensure_target_snapshot_matches(
        target_path: Path,
        target_snapshot: TargetSnapshot,
    ) -> None:
        try:
            current_snapshot = TargetSnapshot.capture(target_path)
        except OSError as exc:
            raise TargetChangedError("出力先の状態を再確認できません") from exc
        if current_snapshot != target_snapshot:
            raise TargetChangedError("画像PDFの作成中に出力先が変更されました")

    @staticmethod
    def _create_temp_output_path(target_path: Path) -> Path:
        file_descriptor, temp_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=f".{target_path.stem}.",
            suffix=".image-to-pdf.tmp.pdf",
        )
        os.close(file_descriptor)
        return Path(temp_name)

    def _validate_runtime_image_identity(self, image: Image.Image, expected: ImageInput) -> None:
        detected_format = str(image.format or "").upper()
        self._validate_detected_format(image, detected_format)
        frame_count = int(getattr(image, "n_frames", 1))
        width, height = self._exif_normalized_size(image)
        if (
            detected_format != expected.detected_format
            or frame_count != expected.frame_count
            or width != expected.pixel_width
            or height != expected.pixel_height
        ):
            raise ImageSourceChangedError(f"{expected.label} の画像構造が変更されました")

    def _prepare_frame(self, frame: Image.Image, *, plan: ImageToPdfPlan) -> EncodedFrame:
        normalized = ImageOps.exif_transpose(frame)
        try:
            dpi = normalized.info.get("dpi", None)
            dpi_x, dpi_y = self._dpi_pair(dpi)
            try:
                geometry = build_page_geometry(
                    pixel_width=normalized.width,
                    pixel_height=normalized.height,
                    dpi_x=dpi_x,
                    dpi_y=dpi_y,
                    plan=plan,
                )
            except ValueError as exc:
                raise ImageToPdfError(str(exc)) from exc
            if geometry.source_crop_box is not None:
                cropped = normalized.crop(geometry.source_crop_box)
                normalized.close()
                normalized = cropped
            color_image, alpha_image = self._normalize_color_and_alpha(normalized, plan)
            try:
                with self._tracked_resource("color_image"):
                    if (
                        alpha_image is not None
                        and plan.transparency_policy is TransparencyPolicy.PRESERVE_ALPHA
                    ):
                        with self._tracked_resource("alpha_image"):
                            alpha_bytes = alpha_image.convert("L").tobytes()
                    else:
                        alpha_bytes = None
                    if color_image.mode == "L":
                        color_space = pikepdf.Name("/DeviceGray")
                    else:
                        converted_color = color_image.convert("RGB")
                        if converted_color is not color_image:
                            color_image.close()
                            color_image = converted_color
                        color_space = pikepdf.Name("/DeviceRGB")
                    return EncodedFrame(
                        width=color_image.width,
                        height=color_image.height,
                        color_bytes=color_image.tobytes(),
                        color_space=color_space,
                        bits_per_component=8,
                        alpha_bytes=alpha_bytes,
                        geometry=geometry,
                    )
            finally:
                color_image.close()
                if alpha_image is not None:
                    alpha_image.close()
        finally:
            normalized.close()

    @staticmethod
    def _dpi_pair(value: object) -> tuple[float | None, float | None]:
        if not isinstance(value, tuple) or len(value) < 2:
            return (None, None)
        x, y = value[0], value[1]
        return (
            float(x) if isinstance(x, int | float) else None,
            float(y) if isinstance(y, int | float) else None,
        )

    def _normalize_color_and_alpha(
        self,
        image: Image.Image,
        plan: ImageToPdfPlan,
    ) -> tuple[Image.Image, Image.Image | None]:
        working = self._convert_indexed_or_transparent(image)
        try:
            if working.mode in {"I;16", "I;16B", "I;16L", "I", "F"}:
                normalized_depth = self._normalize_high_bit_depth_image(working)
                working.close()
                working = normalized_depth

            alpha = self._extract_alpha(working)
            color_source = self._image_without_alpha(working)
            try:
                if color_source.mode == "CMYK":
                    converted_color = self._convert_cmyk_to_srgb(color_source)
                elif color_source.info.get("icc_profile") and color_source.mode in {"RGB", "L"}:
                    converted_color = self._convert_with_icc_to_srgb(color_source)
                else:
                    converted_color = color_source.copy()
            finally:
                color_source.close()

            if alpha is not None and alpha.size != converted_color.size:
                alpha.close()
                converted_color.close()
                raise ImageToPdfError("透明度情報のサイズが画像と一致しません")

            if alpha is not None:
                if plan.transparency_policy is TransparencyPolicy.PRESERVE_ALPHA:
                    return (converted_color, alpha)
                background_color = (
                    (255, 255, 255)
                    if plan.transparency_policy is TransparencyPolicy.WHITE_BACKGROUND
                    else (0, 0, 0)
                )
                background = Image.new("RGB", converted_color.size, background_color)
                rgba = converted_color.convert("RGBA")
                try:
                    background.paste(rgba, mask=alpha)
                finally:
                    rgba.close()
                    converted_color.close()
                    alpha.close()
                return (background, None)
            if converted_color.mode == "L":
                return (converted_color, None)
            if converted_color.mode != "RGB":
                converted_rgb = converted_color.convert("RGB")
                converted_color.close()
                return (converted_rgb, None)
            return (converted_color, None)
        finally:
            working.close()

    @staticmethod
    def _extract_alpha(image: Image.Image) -> Image.Image | None:
        bands = image.getbands()
        if "A" in bands:
            return image.getchannel("A")
        if "a" in bands:
            return image.getchannel("a")
        return None

    @staticmethod
    def _image_without_alpha(image: Image.Image) -> Image.Image:
        icc_profile = image.info.get("icc_profile")
        if image.mode == "RGBA":
            result = image.convert("RGB")
        elif image.mode == "LA":
            result = image.getchannel("L")
        else:
            result = image.copy()
        if isinstance(icc_profile, bytes) and icc_profile:
            result.info["icc_profile"] = icc_profile
        return result

    @staticmethod
    def _convert_indexed_or_transparent(image: Image.Image) -> Image.Image:
        if image.mode == "P" or "transparency" in image.info:
            return image.convert("RGBA" if "transparency" in image.info else "RGB")
        return image.copy()

    def _convert_with_icc_to_srgb(self, image: Image.Image) -> Image.Image:
        profile_bytes = image.info.get("icc_profile")
        if not isinstance(profile_bytes, bytes) or not profile_bytes:
            return image.copy()
        if image.mode not in {"RGB", "L", "CMYK"}:
            raise ImageToPdfError("ICC profile付き画像の色空間が未対応です")
        try:
            source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes))
            target_profile = ImageCms.createProfile("sRGB")
            output_mode = "RGB" if image.mode in {"RGB", "CMYK"} else "L"
            converted = ImageCms.profileToProfile(
                image,
                source_profile,
                target_profile,
                outputMode=output_mode,
            )
        except Exception as exc:
            raise ImageToPdfError("ICC profileを安全に変換できません") from exc
        if converted is None:
            raise ImageToPdfError("ICC profileを安全に変換できません")
        return converted

    def _convert_cmyk_to_srgb(self, image: Image.Image) -> Image.Image:
        if not image.info.get("icc_profile"):
            raise ImageToPdfError("ICC profileなしのCMYK画像は未対応です")
        return self._convert_with_icc_to_srgb(image)

    def _normalize_high_bit_depth_image(self, image: Image.Image) -> Image.Image:
        range_source = image
        if image.mode in {"I;16", "I;16B", "I;16L"}:
            range_source = image.convert("I")
        try:
            if range_source.mode == "F":
                self._ensure_finite_float_pixels(range_source)
            extrema = range_source.getextrema()
            if not isinstance(extrema, tuple) or len(extrema) != 2:
                raise ImageToPdfError("高ビット深度画像の範囲を検査できません")
            minimum, maximum = extrema
            if not isinstance(minimum, int | float) or not isinstance(maximum, int | float):
                raise ImageToPdfError("高ビット深度画像の範囲が不正です")
            minimum_float = float(minimum)
            maximum_float = float(maximum)
            if not math.isfinite(minimum_float) or not math.isfinite(maximum_float):
                raise ImageToPdfError("高ビット深度画像に不正な値が含まれています")
            if maximum_float < minimum_float:
                raise ImageToPdfError("高ビット深度画像の範囲が不正です")
            if maximum_float == minimum_float:
                value = 0 if maximum_float <= 0 else 255
                return Image.new("L", image.size, value)
            scale = 255.0 / (maximum_float - minimum_float)
            converted_values = bytearray()
            float_image = range_source.convert("F")
            try:
                for pixel in self._iter_pixels(float_image):
                    if not isinstance(pixel, int | float) or not math.isfinite(float(pixel)):
                        raise ImageToPdfError("高ビット深度画像に不正な値が含まれています")
                    converted_values.append(
                        max(0, min(255, round((float(pixel) - minimum_float) * scale)))
                    )
            finally:
                float_image.close()
            normalized = Image.new("L", image.size)
            normalized.frombytes(bytes(converted_values))
            return normalized
        finally:
            if range_source is not image:
                range_source.close()

    @staticmethod
    def _ensure_finite_float_pixels(image: Image.Image) -> None:
        for value in ImageToPdfService._iter_pixels(image):
            if not isinstance(value, int | float) or not math.isfinite(float(value)):
                raise ImageToPdfError("floating image data is invalid")

    @staticmethod
    def _iter_pixels(image: Image.Image) -> Iterable[object]:
        flattened_data = getattr(image, "get_flattened_data", None)
        if callable(flattened_data):
            return cast(Iterable[object], flattened_data())
        return ImageToPdfService._iter_pixels_with_getdata(image)

    @staticmethod
    def _iter_pixels_with_getdata(image: Image.Image) -> Iterator[object]:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Image\.Image\.getdata is deprecated",
                category=DeprecationWarning,
            )
            yield from cast(Iterable[object], image.getdata())

    def _append_page(
        self,
        pdf: pikepdf.Pdf,
        frame: EncodedFrame,
        *,
        mapping: ImageFrameRef,
    ) -> ExpectedImagePage:
        page = pdf.add_blank_page(page_size=(frame.geometry.page_width, frame.geometry.page_height))
        image_stream = pikepdf.Stream(pdf, frame.color_bytes)
        image_stream[pikepdf.Name("/Type")] = pikepdf.Name("/XObject")
        image_stream[pikepdf.Name("/Subtype")] = pikepdf.Name("/Image")
        image_stream[pikepdf.Name("/Width")] = frame.width
        image_stream[pikepdf.Name("/Height")] = frame.height
        image_stream[pikepdf.Name("/ColorSpace")] = frame.color_space
        image_stream[pikepdf.Name("/BitsPerComponent")] = frame.bits_per_component
        if frame.alpha_bytes is not None:
            alpha_stream = pikepdf.Stream(pdf, frame.alpha_bytes)
            alpha_stream[pikepdf.Name("/Type")] = pikepdf.Name("/XObject")
            alpha_stream[pikepdf.Name("/Subtype")] = pikepdf.Name("/Image")
            alpha_stream[pikepdf.Name("/Width")] = frame.width
            alpha_stream[pikepdf.Name("/Height")] = frame.height
            alpha_stream[pikepdf.Name("/ColorSpace")] = pikepdf.Name("/DeviceGray")
            alpha_stream[pikepdf.Name("/BitsPerComponent")] = 8
            image_stream[pikepdf.Name("/SMask")] = pdf.make_indirect(alpha_stream)
        image_ref = pdf.make_indirect(image_stream)
        page.obj[pikepdf.Name("/Resources")] = pikepdf.Dictionary(
            {"/XObject": pikepdf.Dictionary({"/Im0": image_ref})}
        )
        content = (
            "q\n"
            f"{frame.geometry.image_width:.6f} 0 0 {frame.geometry.image_height:.6f} "
            f"{frame.geometry.image_x:.6f} {frame.geometry.image_y:.6f} cm\n"
            "/Im0 Do\n"
            "Q\n"
        ).encode("ascii")
        page.obj[pikepdf.Name("/Contents")] = pdf.make_indirect(pikepdf.Stream(pdf, content))
        return ExpectedImagePage(
            output_page_index=mapping.output_page_index,
            source_path=mapping.input_path,
            frame_index=mapping.frame_index,
            media_box=(0.0, 0.0, frame.geometry.page_width, frame.geometry.page_height),
            image_matrix=(
                frame.geometry.image_width,
                0.0,
                0.0,
                frame.geometry.image_height,
                frame.geometry.image_x,
                frame.geometry.image_y,
            ),
            image_pixel_width=frame.width,
            image_pixel_height=frame.height,
            color_space=str(frame.color_space),
            has_soft_mask=frame.alpha_bytes is not None,
        )

    def _validate_candidate(
        self,
        plan: ImageToPdfPlan,
        candidate_path: Path,
        *,
        expected_pages: tuple[ExpectedImagePage, ...],
    ) -> None:
        if not candidate_path.exists() or candidate_path.stat().st_size <= 0:
            raise ImageToPdfError("画像PDF候補が作成されていません")
        try:
            self._validator.validate(
                str(candidate_path),
                expected_page_count=plan.total_page_count,
                render_page_indexes=range(plan.total_page_count),
            )
        except PdfDocumentValidationError as exc:
            raise ImageToPdfError(str(exc)) from exc
        try:
            with pikepdf.open(candidate_path) as pdf:
                if len(pdf.pages) != plan.total_page_count:
                    raise ImageToPdfError("画像PDF候補のページ数が一致しません")
                for key in _DISALLOWED_ROOT_KEYS:
                    if key in pdf.Root:
                        raise ImageToPdfError(f"画像PDF候補に未対応の{key}が残っています")
                with pdf.open_outline() as outline:
                    if outline.root:
                        raise ImageToPdfError("画像PDF候補にbookmarkが残っています")
                for expected, page in zip(expected_pages, pdf.pages, strict=True):
                    self._validate_candidate_page(expected, page)
        except ImageToPdfError:
            raise
        except Exception as exc:
            raise ImageToPdfError("画像PDF候補の構造検証に失敗しました") from exc

    def _validate_candidate_page(self, expected: ExpectedImagePage, page: pikepdf.Page) -> None:
        if page.obj.get("/Type", None) != pikepdf.Name("/Page"):
            raise ImageToPdfError("画像PDF候補のPage Typeが不正です")
        media_box = tuple(float(value) for value in page.obj["/MediaBox"])
        self._assert_close_tuple(media_box, expected.media_box, "MediaBox")
        for unexpected_key in ("/CropBox", "/Rotate", "/AA", "/Metadata"):
            if unexpected_key in page.obj:
                raise ImageToPdfError(f"画像PDF候補ページに未対応の{unexpected_key}が残っています")
        if "/Annots" in page.obj:
            raise ImageToPdfError("画像PDF候補にannotationが残っています")
        resources = page.obj.get("/Resources", None)
        if not isinstance(resources, pikepdf.Dictionary):
            raise ImageToPdfError("画像PDF候補のResourcesが不正です")
        xobjects = resources.get("/XObject", None)
        if not isinstance(xobjects, pikepdf.Dictionary) or set(xobjects.keys()) != {"/Im0"}:
            raise ImageToPdfError("画像PDF候補のXObjectが不正です")
        image = xobjects["/Im0"]
        if not isinstance(image, pikepdf.Stream):
            raise ImageToPdfError("画像PDF候補のImage XObjectが不正です")
        if image.get("/Type", None) != pikepdf.Name("/XObject"):
            raise ImageToPdfError("画像PDF候補のImage Typeが不正です")
        if image.get("/Subtype", None) != pikepdf.Name("/Image"):
            raise ImageToPdfError("画像PDF候補のImage Subtypeが不正です")
        if int(image["/Width"]) != expected.image_pixel_width:
            raise ImageToPdfError("画像PDF候補のimage widthが一致しません")
        if int(image["/Height"]) != expected.image_pixel_height:
            raise ImageToPdfError("画像PDF候補のimage heightが一致しません")
        if str(image["/ColorSpace"]) != expected.color_space:
            raise ImageToPdfError("画像PDF候補のColorSpaceが一致しません")
        if int(image["/BitsPerComponent"]) != 8:
            raise ImageToPdfError("画像PDF候補のBitsPerComponentが不正です")
        has_soft_mask = "/SMask" in image
        if has_soft_mask != expected.has_soft_mask:
            raise ImageToPdfError("画像PDF候補の透明度設定が一致しません")
        if has_soft_mask:
            soft_mask = image["/SMask"]
            if not isinstance(soft_mask, pikepdf.Stream):
                raise ImageToPdfError("画像PDF候補のSMaskが不正です")
            if soft_mask.get("/Subtype", None) != pikepdf.Name("/Image"):
                raise ImageToPdfError("画像PDF候補のSMask Subtypeが不正です")
            if int(soft_mask["/Width"]) != expected.image_pixel_width:
                raise ImageToPdfError("画像PDF候補のSMask widthが一致しません")
            if int(soft_mask["/Height"]) != expected.image_pixel_height:
                raise ImageToPdfError("画像PDF候補のSMask heightが一致しません")
            if soft_mask.get("/ColorSpace", None) != pikepdf.Name("/DeviceGray"):
                raise ImageToPdfError("画像PDF候補のSMask ColorSpaceが不正です")
            if int(soft_mask["/BitsPerComponent"]) != 8:
                raise ImageToPdfError("画像PDF候補のSMask BitsPerComponentが不正です")
        content = bytes(page.obj["/Contents"].read_bytes()).decode("ascii")
        tokens = content.split()
        if len(tokens) != 11 or tokens[0] != "q" or tokens[7] != "cm":
            raise ImageToPdfError("画像PDF候補のcontent streamが不正です")
        if tokens[8] != "/Im0" or tokens[9] != "Do" or tokens[10] != "Q":
            raise ImageToPdfError("画像PDF候補のcontent streamが不正です")
        numbers = tuple(float(value) for value in tokens[1:7])
        self._assert_close_tuple(numbers, expected.image_matrix, "image matrix")

    @staticmethod
    def _assert_close_tuple(
        actual: tuple[float, ...],
        expected: tuple[float, ...],
        label: str,
    ) -> None:
        if len(actual) != len(expected):
            raise ImageToPdfError(f"{label}の長さが一致しません")
        for actual_value, expected_value in zip(actual, expected, strict=True):
            if abs(actual_value - expected_value) > 0.01:
                raise ImageToPdfError(f"{label}が一致しません")

    @staticmethod
    def _exif_normalized_size(image: Image.Image) -> tuple[int, int]:
        orientation = ImageToPdfService._read_exif_orientation(image)
        width, height = image.size
        if orientation in {5, 6, 7, 8}:
            return (height, width)
        return (width, height)

    @staticmethod
    def _read_exif_orientation(image: Image.Image) -> int | None:
        try:
            exif = image.getexif()
        except Exception as exc:
            raise ImageToPdfError("EXIF orientationを読み取れません") from exc
        if not exif:
            return None
        value = exif.get(274)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value not in range(1, 9):
            raise ImageToPdfError("EXIF orientationが不正です")
        return value

    @staticmethod
    def _has_alpha(image: Image.Image) -> bool:
        return (
            image.mode in {"RGBA", "LA"}
            or ("A" in image.getbands())
            or ("transparency" in image.info)
        )

    @contextmanager
    def _tracked_resource(self, name: str) -> Iterator[None]:
        if self._resource_tracker is not None:
            self._resource_tracker(name, 1)
        try:
            yield
        finally:
            if self._resource_tracker is not None:
                self._resource_tracker(name, -1)

    @staticmethod
    def _check_cancelled(should_cancel: Callable[[], bool] | None) -> None:
        if should_cancel is not None and should_cancel():
            raise ImageToPdfCancelled("画像PDF作成をキャンセルしました")

    def _emit_progress(
        self,
        callback: Callable[[ImageToPdfProgress], None] | None,
        plan: ImageToPdfPlan,
        stage: ImageToPdfStage,
        mapping: ImageFrameRef,
        *,
        completed_pages: int,
    ) -> None:
        if callback is None:
            return
        image_input = plan.inputs[mapping.input_index]
        callback(
            ImageToPdfProgress(
                stage=stage,
                input_number=mapping.input_index + 1,
                input_count=len(plan.inputs),
                filename=image_input.label,
                frame_number=mapping.frame_index + 1,
                frame_count=image_input.frame_count,
                completed_pages=completed_pages,
                total_pages=plan.total_page_count,
                message=stage.value,
            )
        )

    @staticmethod
    def _result_inputs(plan: ImageToPdfPlan) -> tuple[ImageToPdfResultInput, ...]:
        results: list[ImageToPdfResultInput] = []
        cursor = 1
        for image_input in plan.inputs:
            start = cursor
            end = cursor + image_input.frame_count - 1
            display_range = f"{start}" if start == end else f"{start}-{end}"
            results.append(
                ImageToPdfResultInput(
                    label=image_input.label,
                    path=image_input.path,
                    frame_count=image_input.frame_count,
                    display_range=display_range,
                )
            )
            cursor = end + 1
        return tuple(results)

    @staticmethod
    def _apply_existing_target_mode(candidate_path: Path, target_path: Path) -> None:
        if not target_path.exists() or os.name == "nt":
            return
        try:
            os.chmod(candidate_path, stat.S_IMODE(target_path.stat().st_mode))
        except OSError as exc:
            raise ImageToPdfError("出力先ファイル属性の適用に失敗しました") from exc

    @staticmethod
    def _replace_atomically(candidate_path: Path, target_path: Path) -> None:
        try:
            os.replace(candidate_path, target_path)
        except OSError as exc:
            raise ImageToPdfError("検証済み画像PDFの置換に失敗しました") from exc

    @staticmethod
    def _fsync_file(path: Path) -> None:
        try:
            with path.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ImageToPdfError("画像PDF候補の同期に失敗しました") from exc

    @staticmethod
    def _fsync_parent_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            directory_handle = os.open(directory, os.O_RDONLY)
        except OSError as exc:
            logger.warning("Failed to open parent directory for fsync: %s (%s)", directory, exc)
            return
        try:
            os.fsync(directory_handle)
        except OSError as exc:
            logger.warning("Failed to fsync parent directory after image PDF replace: %s", exc)
        finally:
            os.close(directory_handle)

    @staticmethod
    def _build_fingerprint(path: Path) -> FileFingerprint:
        stat_result = path.stat()
        return FileFingerprint(
            size_bytes=stat_result.st_size,
            modified_time_ns=stat_result.st_mtime_ns,
        )

    @staticmethod
    def _cleanup_candidate(
        candidate_path: Path | None,
        *,
        primary_error: BaseException | None,
    ) -> None:
        if candidate_path is None or not candidate_path.exists():
            return
        try:
            candidate_path.unlink()
        except OSError as exc:
            logger.warning(
                "Failed to remove image PDF candidate: candidate=%s "
                "primary_error=%s cleanup_error=%s",
                candidate_path,
                type(primary_error).__name__ if primary_error is not None else None,
                exc,
            )
