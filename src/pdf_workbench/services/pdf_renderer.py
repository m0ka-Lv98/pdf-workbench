from __future__ import annotations

import logging
import math
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL.ImageQt import ImageQt
from pypdf import PdfReader
from PySide6.QtCore import QEventLoop, QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QImage

from pdf_workbench.services.page_coordinates import PageGeometry, PageMetadata, PdfRect

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentRevision:
    resolved_path: str
    file_size: int
    mtime_ns: int

    @classmethod
    def from_path(cls, path: Path) -> DocumentRevision:
        resolved_path = path.expanduser().resolve()
        stat_result = resolved_path.stat()
        return cls(
            resolved_path=str(resolved_path),
            file_size=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
        )


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    revision: DocumentRevision
    pages: tuple[PageMetadata, ...]

    @property
    def page_count(self) -> int:
        return len(self.pages)


@dataclass(frozen=True, slots=True)
class TextCharacterBox:
    pdfium_index: int
    text: str
    box: PdfRect | None
    line_index: int | None = None


@dataclass(frozen=True, slots=True)
class PageTextIndex:
    revision: DocumentRevision
    page_index: int
    characters: tuple[TextCharacterBox, ...]
    text: str
    has_image_content: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedPageText:
    text: str
    source_character_offsets: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SearchMatch:
    page_index: int
    start_pdfium_index: int
    end_pdfium_index: int
    text: str
    boxes: tuple[PdfRect, ...]


@dataclass(frozen=True, slots=True)
class RenderCacheKey:
    revision: DocumentRevision
    page_index: int
    logical_zoom: float
    rotation: int
    device_pixel_ratio: float


@dataclass(frozen=True, slots=True)
class RenderRequest:
    document_id: str
    generation: int
    page_index: int
    logical_zoom: float
    rotation: int
    device_pixel_ratio: float
    priority: int
    revision: DocumentRevision

    @property
    def cache_key(self) -> RenderCacheKey:
        return RenderCacheKey(
            revision=self.revision,
            page_index=self.page_index,
            logical_zoom=self.logical_zoom,
            rotation=self.rotation,
            device_pixel_ratio=self.device_pixel_ratio,
        )


@dataclass(frozen=True, slots=True)
class RenderResult:
    document_id: str
    generation: int
    page_index: int
    image: QImage
    cache_key: RenderCacheKey


@dataclass(frozen=True, slots=True)
class RenderFailure:
    document_id: str
    generation: int
    page_index: int
    cache_key: RenderCacheKey
    message: str


@dataclass(slots=True)
class WorkerDocumentContext:
    backend: PdfDocumentBackend
    generation: int
    revision: DocumentRevision


@dataclass(frozen=True, slots=True)
class QueuedRenderRequest:
    sequence: int
    request: RenderRequest


class PdfDocumentBackend(Protocol):
    def page_count(self) -> int: ...

    def page_metadata(self, page_index: int) -> PageMetadata: ...

    def render_page(
        self,
        page_index: int,
        logical_zoom: float,
        rotation: int,
        device_pixel_ratio: float,
    ) -> QImage: ...

    def extract_text_page(
        self,
        page_index: int,
        revision: DocumentRevision,
    ) -> PageTextIndex: ...

    def close(self) -> None: ...


class PdfBackendFactory(Protocol):
    def __call__(self, path: Path) -> PdfDocumentBackend: ...


class PdfRenderServiceProtocol(Protocol):
    document_loaded: Any
    document_failed: Any
    render_succeeded: Any
    render_failed: Any
    text_page_indexed: Any
    text_index_progress: Any
    text_index_completed: Any
    text_index_failed: Any

    def open_document(
        self,
        document_id: str,
        path: Path,
        generation: int,
        revision: DocumentRevision,
    ) -> None: ...

    def request_render(self, request: RenderRequest) -> None: ...

    def close_document(self, document_id: str, generation: int) -> None: ...

    def release_document(
        self,
        document_id: str,
        generation: int,
        timeout_ms: int = 3000,
    ) -> bool: ...

    def transition_cache_revision(
        self,
        old_revision: DocumentRevision,
        new_revision: DocumentRevision,
        *,
        affected_pages: frozenset[int],
    ) -> None: ...

    def update_document_generation(
        self,
        document_id: str,
        generation: int,
        revision: DocumentRevision,
    ) -> None: ...


class _PdfiumTextPageProtocol(Protocol):
    def count_chars(self) -> int: ...

    def count_rects(self, index: int = 0, count: int = -1) -> int: ...

    def get_rect(self, index: int) -> tuple[float, float, float, float]: ...

    def get_text_range(self, index: int = 0, count: int = -1, errors: str = "ignore") -> str: ...

    def get_charbox(
        self,
        index: int,
        loose: bool = False,
    ) -> tuple[float, float, float, float]: ...

    def close(self) -> None: ...


class PdfiumDocumentBackend:
    def __init__(self, path: Path) -> None:
        self._document = pdfium.PdfDocument(str(path))
        self._reader = PdfReader(str(path))

    def page_count(self) -> int:
        return len(self._document)

    def page_metadata(self, page_index: int) -> PageMetadata:
        page = self._document[page_index]
        try:
            geometry = PageGeometry.from_pdfium_page(page)
        finally:
            page.close()
        return PageMetadata(geometry=geometry)

    def render_page(
        self,
        page_index: int,
        logical_zoom: float,
        rotation: int,
        device_pixel_ratio: float,
    ) -> QImage:
        if not math.isfinite(logical_zoom) or logical_zoom <= 0:
            raise ValueError("logical_zoom must be finite and positive")
        if not math.isfinite(device_pixel_ratio) or device_pixel_ratio <= 0:
            raise ValueError("device_pixel_ratio must be finite and positive")
        if rotation not in {0, 90, 180, 270}:
            raise ValueError("rotation must be one of 0, 90, 180, 270")

        page = self._document[page_index]
        bitmap: Any | None = None
        source_image: Any | None = None
        rgba_image: Any | None = None
        try:
            scale = logical_zoom * device_pixel_ratio
            bitmap = page.render(scale=scale, rotation=rotation)
            source_image = bitmap.to_pil()
            rgba_image = source_image.convert("RGBA")
            qimage = QImage(ImageQt(rgba_image)).copy()
        finally:
            self._close_render_resource(rgba_image, "temporary RGBA PIL image")
            self._close_render_resource(source_image, "temporary source PIL image")
            self._close_render_resource(bitmap, "temporary PDFium bitmap")
            self._close_render_resource(page, "PDFium page")

        qimage.setDevicePixelRatio(device_pixel_ratio)
        return qimage

    @staticmethod
    def _close_render_resource(resource: object | None, label: str) -> None:
        if resource is None:
            return
        try:
            close = resource.close  # type: ignore[attr-defined]
            close()
        except Exception:
            logger.exception("Failed to close %s after PDF render", label)

    def extract_text_page(
        self,
        page_index: int,
        revision: DocumentRevision,
    ) -> PageTextIndex:
        page = self._document[page_index]
        try:
            typed_text_page: _PdfiumTextPageProtocol = page.get_textpage()
            try:
                count = typed_text_page.count_chars()
                line_rects = self._text_line_rects(typed_text_page, count)
                characters: list[TextCharacterBox] = []
                text_parts: list[str] = []
                for index in range(count):
                    try:
                        piece = typed_text_page.get_text_range(index, 1)
                    except Exception:
                        piece = ""
                    piece = piece.replace("\x00", "")
                    text_parts.append(piece)
                    box: PdfRect | None
                    try:
                        char_box = typed_text_page.get_charbox(index)
                    except Exception:
                        box = None
                    else:
                        try:
                            box = PdfRect.normalized(
                                (
                                    float(char_box[0]),
                                    float(char_box[1]),
                                    float(char_box[2]),
                                    float(char_box[3]),
                                )
                            )
                        except Exception:
                            box = None
                    characters.append(
                        TextCharacterBox(
                            pdfium_index=index,
                            text=piece,
                            box=box,
                            line_index=self._line_index_for_box(box, line_rects),
                        )
                    )
                text = "".join(text_parts)
                return PageTextIndex(
                    revision=revision,
                    page_index=page_index,
                    characters=tuple(characters),
                    text=text,
                    has_image_content=self._page_has_images(page_index),
                )
            finally:
                typed_text_page.close()
        finally:
            page.close()

    def close(self) -> None:
        self._document.close()

    @staticmethod
    def _text_line_rects(
        text_page: _PdfiumTextPageProtocol,
        count: int,
    ) -> tuple[PdfRect, ...]:
        if count <= 0:
            return ()
        try:
            rect_count = text_page.count_rects(0, count)
        except Exception:
            return ()
        rects: list[PdfRect] = []
        for index in range(rect_count):
            try:
                rect = text_page.get_rect(index)
            except Exception:
                continue
            try:
                rects.append(
                    PdfRect.normalized(
                        (
                            float(rect[0]),
                            float(rect[1]),
                            float(rect[2]),
                            float(rect[3]),
                        )
                    )
                )
            except Exception:
                continue
        return tuple(rects)

    @staticmethod
    def _line_index_for_box(box: PdfRect | None, line_rects: tuple[PdfRect, ...]) -> int | None:
        if box is None or not line_rects:
            return None
        center = box.center
        best_index: int | None = None
        best_overlap = 0.0
        for index, line_rect in enumerate(line_rects):
            intersection = box.intersection(line_rect)
            if intersection is not None:
                overlap_area = intersection.width * intersection.height
                if overlap_area > best_overlap:
                    best_overlap = overlap_area
                    best_index = index
                    continue
            if (
                line_rect.left <= center.x <= line_rect.right
                and line_rect.bottom <= center.y <= line_rect.top
            ):
                return index
        return best_index

    def _page_has_images(self, page_index: int) -> bool:
        try:
            page = self._reader.pages[page_index]
        except Exception:
            return False
        images = getattr(page, "images", None)
        if images is not None:
            try:
                return any(True for _ in images)
            except Exception:
                return False
        return False


class RenderImageCache:
    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._items: OrderedDict[RenderCacheKey, QImage] = OrderedDict()
        self._total_bytes = 0

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def clear(self) -> None:
        self._items.clear()
        self._total_bytes = 0

    def get(self, key: RenderCacheKey) -> QImage | None:
        image = self._items.get(key)
        if image is None:
            return None
        self._items.move_to_end(key)
        return image

    def put(self, key: RenderCacheKey, image: QImage) -> None:
        image_bytes = self._image_bytes(image)
        if key in self._items:
            existing = self._items.pop(key)
            self._total_bytes -= self._image_bytes(existing)
        self._items[key] = image
        self._total_bytes += image_bytes
        self._evict_if_needed()

    def transition_revision(
        self,
        old_revision: DocumentRevision,
        new_revision: DocumentRevision,
        *,
        affected_pages: frozenset[int],
    ) -> None:
        if old_revision == new_revision:
            for key in list(self._items):
                if key.revision == old_revision and key.page_index in affected_pages:
                    image = self._items.pop(key)
                    self._total_bytes -= self._image_bytes(image)
            return

        updated: OrderedDict[RenderCacheKey, QImage] = OrderedDict()
        total_bytes = 0
        for key, image in self._items.items():
            next_key = key
            if key.revision == old_revision:
                if key.page_index in affected_pages:
                    continue
                next_key = RenderCacheKey(
                    revision=new_revision,
                    page_index=key.page_index,
                    logical_zoom=key.logical_zoom,
                    rotation=key.rotation,
                    device_pixel_ratio=key.device_pixel_ratio,
                )
            if next_key in updated:
                total_bytes -= self._image_bytes(updated[next_key])
                updated.pop(next_key)
            updated[next_key] = image
            total_bytes += self._image_bytes(image)
        self._items = updated
        self._total_bytes = total_bytes
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        while len(self._items) > 1 and self._total_bytes > self._max_bytes:
            _, image = self._items.popitem(last=False)
            self._total_bytes -= self._image_bytes(image)

    @staticmethod
    def _image_bytes(image: QImage) -> int:
        return max(1, image.sizeInBytes())


DEFAULT_RENDER_CACHE_BYTES = 128 * 1024 * 1024


class PdfRenderWorker(QObject):
    document_loaded = Signal(object, int, object)
    document_failed = Signal(object, int, str)
    render_succeeded = Signal(object)
    render_failed = Signal(object)
    text_page_indexed = Signal(object, object, object)
    text_index_progress = Signal(object, object, int, int, int)
    text_index_completed = Signal(object, object, int, int)
    text_index_failed = Signal(object, object, int, str)
    document_closed = Signal(str, int)
    shutdown_completed = Signal()

    def __init__(
        self,
        backend_factory: PdfBackendFactory,
        cache: RenderImageCache,
    ) -> None:
        super().__init__()
        self._backend_factory = backend_factory
        self._cache = cache
        self._documents: dict[str, WorkerDocumentContext] = {}
        self._pending_requests: list[QueuedRenderRequest] = []
        self._pending_text_requests: list[tuple[str, int, int, DocumentRevision]] = []
        self._processed_text_pages: dict[tuple[str, DocumentRevision], set[int]] = {}
        self._failed_text_pages: dict[tuple[str, DocumentRevision], set[int]] = {}
        self._pending_keys: set[tuple[str, int, int, float, int, float, DocumentRevision]] = set()
        self._pending_text_keys: set[tuple[str, int, int, DocumentRevision]] = set()
        self._sequence = 0
        self._processing = False
        self._shutting_down = False

    @Slot(str, Path, int, object)
    def open_document(
        self,
        document_id: str,
        path: Path,
        generation: int,
        revision: object,
    ) -> None:
        if self._shutting_down:
            self.document_failed.emit(document_id, generation, "renderer is shutting down")
            return
        if not isinstance(revision, DocumentRevision):
            raise TypeError("revision must be DocumentRevision")

        self._close_document_backend(document_id)
        self._drop_pending_render_for_document(document_id)
        self._drop_pending_text_for_document(document_id)

        backend: PdfDocumentBackend | None = None
        try:
            backend = self._backend_factory(path)
            pages = tuple(backend.page_metadata(index) for index in range(backend.page_count()))
        except Exception as exc:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    logger.exception("Failed to close backend after document open failure")
            self.document_failed.emit(document_id, generation, str(exc))
            return

        self._documents[document_id] = WorkerDocumentContext(
            backend=backend,
            generation=generation,
            revision=revision,
        )
        self.document_loaded.emit(
            document_id,
            generation,
            DocumentMetadata(revision=revision, pages=pages),
        )
        self._queue_text_index_requests(document_id, generation, revision, len(pages))

    @Slot(object)
    def enqueue_render(self, request: object) -> None:
        if self._shutting_down:
            return
        if not isinstance(request, RenderRequest):
            raise TypeError("request must be RenderRequest")

        context = self._documents.get(request.document_id)
        if context is None:
            return
        if request.generation != context.generation or request.revision != context.revision:
            return

        cached = self._cache.get(request.cache_key)
        if cached is not None:
            self.render_succeeded.emit(
                RenderResult(
                    document_id=request.document_id,
                    generation=request.generation,
                    page_index=request.page_index,
                    image=cached,
                    cache_key=request.cache_key,
                )
            )
            return

        request_key = self._request_key(request)
        if request_key in self._pending_keys:
            return

        queued_request = QueuedRenderRequest(sequence=self._sequence, request=request)
        self._sequence += 1
        self._pending_requests.append(queued_request)
        self._pending_keys.add(request_key)
        self._pending_requests.sort(key=lambda item: (item.request.priority, item.sequence))
        self._schedule_processing()

    @Slot(str, int, int, object)
    def enqueue_text_index(
        self, document_id: str, generation: int, page_index: int, revision: object
    ) -> None:
        if self._shutting_down:
            return
        if not isinstance(revision, DocumentRevision):
            raise TypeError("revision must be DocumentRevision")
        context = self._documents.get(document_id)
        if context is None:
            return
        if revision != context.revision:
            return
        key = (document_id, generation, page_index, revision)
        if key in self._pending_text_keys:
            return
        self._pending_text_requests.append(key)
        self._pending_text_keys.add(key)
        self._schedule_processing()

    @Slot(str, int)
    def close_document(self, document_id: str, generation: int) -> None:
        context = self._documents.get(document_id)
        if context is None:
            self.document_closed.emit(document_id, generation)
            return
        if generation < context.generation:
            self.document_closed.emit(document_id, context.generation)
            return
        self._drop_pending_render_for_document(document_id)
        self._drop_pending_text_for_document(document_id)
        self._close_document_backend(document_id)
        self.document_closed.emit(document_id, generation)

    @Slot(str, int, object)
    def update_document_generation(
        self,
        document_id: str,
        generation: int,
        revision: object,
    ) -> None:
        if self._shutting_down:
            return
        if not isinstance(revision, DocumentRevision):
            raise TypeError("revision must be DocumentRevision")

        context = self._documents.get(document_id)
        if context is None:
            return
        if context.revision != revision:
            return
        if generation < context.generation:
            return

        self._drop_pending_render_for_document(document_id)
        context.generation = generation

    @Slot(object, object, object)
    def transition_cache_revision(
        self,
        old_revision: object,
        new_revision: object,
        affected_pages: object,
    ) -> None:
        if not isinstance(old_revision, DocumentRevision):
            raise TypeError("old_revision must be DocumentRevision")
        if not isinstance(new_revision, DocumentRevision):
            raise TypeError("new_revision must be DocumentRevision")
        if not isinstance(affected_pages, frozenset):
            raise TypeError("affected_pages must be frozenset[int]")
        self._cache.transition_revision(
            old_revision,
            new_revision,
            affected_pages=affected_pages,
        )

    @Slot()
    def shutdown(self) -> None:
        if self._shutting_down:
            self.shutdown_completed.emit()
            return

        self._shutting_down = True
        self._pending_requests.clear()
        self._pending_keys.clear()
        self._pending_text_requests.clear()
        self._pending_text_keys.clear()
        self._processed_text_pages.clear()
        self._failed_text_pages.clear()
        for document_id in list(self._documents):
            self._close_document_backend(document_id)
        self._documents.clear()
        self._cache.clear()
        self.shutdown_completed.emit()

    def _schedule_processing(self) -> None:
        if (
            self._processing
            or self._shutting_down
            or not (self._pending_requests or self._pending_text_requests)
        ):
            return
        self._processing = True
        QTimer.singleShot(0, self._process_next)

    def _process_next(self) -> None:
        if self._shutting_down:
            self._processing = False
            return

        next_request = self._pop_next_valid_request()
        if next_request is None:
            next_text_request = self._pop_next_valid_text_request()
            if next_text_request is None:
                self._processing = False
                return
            document_id, _generation, page_index, requested_revision = next_text_request
            context = self._documents.get(document_id)
            if context is None or context.revision != requested_revision or self._shutting_down:
                self._processing = False
                self._schedule_processing()
                return
            try:
                text_index = context.backend.extract_text_page(page_index, requested_revision)
            except Exception as exc:
                self._failed_text_pages.setdefault(
                    (document_id, requested_revision),
                    set(),
                ).add(page_index)
                current_context = self._documents.get(document_id)
                if (
                    current_context is None
                    or current_context.revision != requested_revision
                    or self._shutting_down
                ):
                    self._processing = False
                    self._schedule_processing()
                    return
                self.text_index_failed.emit(
                    document_id,
                    requested_revision,
                    page_index,
                    str(exc),
                )
            else:
                self._processed_text_pages.setdefault(
                    (document_id, requested_revision),
                    set(),
                ).add(page_index)
                current_context = self._documents.get(document_id)
                if (
                    current_context is not None
                    and current_context.revision == requested_revision
                    and not self._shutting_down
                ):
                    self.text_page_indexed.emit(document_id, requested_revision, text_index)
            current_context = self._documents.get(document_id)
            if (
                current_context is None
                or current_context.revision != requested_revision
                or self._shutting_down
            ):
                self._processing = False
                self._schedule_processing()
                return
            processed = len(
                self._processed_text_pages.get((document_id, requested_revision), set())
            )
            failed = len(self._failed_text_pages.get((document_id, requested_revision), set()))
            self.text_index_progress.emit(
                document_id,
                requested_revision,
                processed,
                current_context.backend.page_count(),
                failed,
            )
            if processed + failed >= current_context.backend.page_count():
                self.text_index_completed.emit(
                    document_id,
                    requested_revision,
                    processed,
                    failed,
                )
            self._processing = False
            if self._pending_requests or self._pending_text_requests:
                self._schedule_processing()
            return

        context = self._documents.get(next_request.document_id)
        if context is None:
            self._processing = False
            self._schedule_processing()
            return

        try:
            image = context.backend.render_page(
                next_request.page_index,
                next_request.logical_zoom,
                next_request.rotation,
                next_request.device_pixel_ratio,
            )
        except Exception as exc:
            if self._is_request_current(next_request):
                self.render_failed.emit(
                    RenderFailure(
                        document_id=next_request.document_id,
                        generation=next_request.generation,
                        page_index=next_request.page_index,
                        cache_key=next_request.cache_key,
                        message=str(exc),
                    )
                )
        else:
            if self._is_request_current(next_request):
                self._cache.put(next_request.cache_key, image)
                self.render_succeeded.emit(
                    RenderResult(
                        document_id=next_request.document_id,
                        generation=next_request.generation,
                        page_index=next_request.page_index,
                        image=image,
                        cache_key=next_request.cache_key,
                    )
                )

        self._processing = False
        if (self._pending_requests or self._pending_text_requests) and not self._shutting_down:
            self._schedule_processing()

    def _pop_next_valid_request(self) -> RenderRequest | None:
        while self._pending_requests:
            queued_request = self._pending_requests.pop(0)
            request = queued_request.request
            self._pending_keys.discard(self._request_key(request))
            if self._is_request_current(request):
                return request
        return None

    def _pop_next_valid_text_request(self) -> tuple[str, int, int, DocumentRevision] | None:
        while self._pending_text_requests:
            request = self._pending_text_requests.pop(0)
            self._pending_text_keys.discard(request)
            document_id, _generation, _page_index, revision = request
            if self._is_text_request_current(document_id, revision):
                return request
        return None

    def _is_request_current(self, request: RenderRequest) -> bool:
        if self._shutting_down:
            return False
        context = self._documents.get(request.document_id)
        if context is None:
            return False
        return request.generation == context.generation and request.revision == context.revision

    def _is_text_request_current(self, document_id: str, revision: DocumentRevision) -> bool:
        if self._shutting_down:
            return False
        context = self._documents.get(document_id)
        if context is None:
            return False
        return context.revision == revision

    def _drop_pending_render_for_document(self, document_id: str) -> None:
        kept_requests: list[QueuedRenderRequest] = []
        self._pending_keys.clear()
        for queued_request in self._pending_requests:
            if queued_request.request.document_id == document_id:
                continue
            kept_requests.append(queued_request)
            self._pending_keys.add(self._request_key(queued_request.request))
        self._pending_requests = kept_requests

    def _drop_pending_text_for_document(self, document_id: str) -> None:
        self._pending_text_requests = [
            request for request in self._pending_text_requests if request[0] != document_id
        ]
        self._pending_text_keys = set(self._pending_text_requests)
        for key in list(self._processed_text_pages):
            if key[0] == document_id:
                self._processed_text_pages.pop(key, None)
        for key in list(self._failed_text_pages):
            if key[0] == document_id:
                self._failed_text_pages.pop(key, None)

    def _close_document_backend(self, document_id: str) -> None:
        context = self._documents.pop(document_id, None)
        if context is None:
            return
        context.backend.close()

    @staticmethod
    def _request_key(
        request: RenderRequest,
    ) -> tuple[str, int, int, float, int, float, DocumentRevision]:
        return (
            request.document_id,
            request.generation,
            request.page_index,
            request.logical_zoom,
            request.rotation,
            request.device_pixel_ratio,
            request.revision,
        )

    def _queue_text_index_requests(
        self,
        document_id: str,
        generation: int,
        revision: DocumentRevision,
        page_count: int,
    ) -> None:
        for page_index in range(page_count):
            key = (document_id, generation, page_index, revision)
            if key in self._pending_text_keys:
                continue
            self._pending_text_requests.append(key)
            self._pending_text_keys.add(key)
        self._schedule_processing()


class PdfRenderService(QObject):
    document_loaded = Signal(object, int, object)
    document_failed = Signal(object, int, str)
    render_succeeded = Signal(object)
    render_failed = Signal(object)
    text_page_indexed = Signal(object, object, object)
    text_index_progress = Signal(object, object, int, int, int)
    text_index_completed = Signal(object, object, int, int)
    text_index_failed = Signal(object, object, int, str)
    _open_requested = Signal(str, Path, int, object)
    _render_requested = Signal(object)
    _close_requested = Signal(str, int)
    _update_generation_requested = Signal(str, int, object)
    _transition_cache_requested = Signal(object, object, object)
    _shutdown_requested = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        backend_factory: PdfBackendFactory | None = None,
        cache: RenderImageCache | None = None,
    ) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._worker = PdfRenderWorker(
            backend_factory=backend_factory or PdfiumDocumentBackend,
            cache=cache or RenderImageCache(DEFAULT_RENDER_CACHE_BYTES),
        )
        self._worker.moveToThread(self._thread)
        self._worker.document_loaded.connect(self.document_loaded)
        self._worker.document_failed.connect(self.document_failed)
        self._worker.render_succeeded.connect(self.render_succeeded)
        self._worker.render_failed.connect(self.render_failed)
        self._worker.text_page_indexed.connect(self.text_page_indexed)
        self._worker.text_index_progress.connect(self.text_index_progress)
        self._worker.text_index_completed.connect(self.text_index_completed)
        self._worker.text_index_failed.connect(self.text_index_failed)
        self._worker.shutdown_completed.connect(
            self._thread.quit,
            Qt.ConnectionType.DirectConnection,
        )
        self._open_requested.connect(self._worker.open_document)
        self._render_requested.connect(self._worker.enqueue_render)
        self._close_requested.connect(self._worker.close_document)
        self._update_generation_requested.connect(self._worker.update_document_generation)
        self._transition_cache_requested.connect(self._worker.transition_cache_revision)
        self._shutdown_requested.connect(self._worker.shutdown)
        self._thread.start()
        self._shutdown_requested_once = False

    def open_document(
        self,
        document_id: str,
        path: Path,
        generation: int,
        revision: DocumentRevision,
    ) -> None:
        if self._shutdown_requested_once or not self._thread.isRunning():
            return
        self._open_requested.emit(document_id, path, generation, revision)

    def request_render(self, request: RenderRequest) -> None:
        if self._shutdown_requested_once or not self._thread.isRunning():
            return
        self._render_requested.emit(request)

    def close_document(self, document_id: str, generation: int) -> None:
        if self._shutdown_requested_once or not self._thread.isRunning():
            return
        self._close_requested.emit(document_id, generation)

    def release_document(self, document_id: str, generation: int, timeout_ms: int = 3000) -> bool:
        if self._shutdown_requested_once or not self._thread.isRunning():
            return True

        event_loop = QEventLoop(self)
        timeout = QTimer(self)
        timeout.setSingleShot(True)
        timed_out = False

        def handle_closed(closed_document_id: str, _closed_generation: int) -> None:
            if closed_document_id != document_id:
                return
            timeout.stop()
            event_loop.quit()

        def handle_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            event_loop.quit()

        self._worker.document_closed.connect(handle_closed)
        timeout.timeout.connect(handle_timeout)
        timeout.start(timeout_ms)
        self._close_requested.emit(document_id, generation)
        event_loop.exec()
        with suppress(RuntimeError, TypeError):
            self._worker.document_closed.disconnect(handle_closed)
        return not timed_out

    def update_document_generation(
        self,
        document_id: str,
        generation: int,
        revision: DocumentRevision,
    ) -> None:
        if self._shutdown_requested_once or not self._thread.isRunning():
            return
        self._update_generation_requested.emit(document_id, generation, revision)

    def transition_cache_revision(
        self,
        old_revision: DocumentRevision,
        new_revision: DocumentRevision,
        *,
        affected_pages: frozenset[int],
    ) -> None:
        if self._shutdown_requested_once or not self._thread.isRunning():
            return
        self._transition_cache_requested.emit(old_revision, new_revision, affected_pages)

    def shutdown(self, timeout_ms: int = 3000) -> bool:
        if not self._thread.isRunning():
            return True
        if not self._shutdown_requested_once:
            self._shutdown_requested_once = True
            self._shutdown_requested.emit()
        if not self._thread.wait(timeout_ms):
            logger.warning("Timed out while waiting for PdfRenderService worker thread shutdown")
            return False
        return True
