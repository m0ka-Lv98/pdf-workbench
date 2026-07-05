from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QImage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PageMetadata:
    width_points: float
    height_points: float


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

    def close(self) -> None: ...


class PdfBackendFactory(Protocol):
    def __call__(self, path: Path) -> PdfDocumentBackend: ...


class PdfRenderServiceProtocol(Protocol):
    document_loaded: Any
    document_failed: Any
    render_succeeded: Any
    render_failed: Any

    def open_document(
        self,
        document_id: str,
        path: Path,
        generation: int,
        revision: DocumentRevision,
    ) -> None: ...

    def request_render(self, request: RenderRequest) -> None: ...

    def close_document(self, document_id: str, generation: int) -> None: ...


class PdfiumDocumentBackend:
    def __init__(self, path: Path) -> None:
        self._document = pdfium.PdfDocument(str(path))

    def page_count(self) -> int:
        return len(self._document)

    def page_metadata(self, page_index: int) -> PageMetadata:
        page = self._document[page_index]
        try:
            width, height = page.get_size()
        finally:
            page.close()
        return PageMetadata(width_points=float(width), height_points=float(height))

    def render_page(
        self,
        page_index: int,
        logical_zoom: float,
        rotation: int,
        device_pixel_ratio: float,
    ) -> QImage:
        if logical_zoom <= 0:
            raise ValueError("logical_zoom must be positive")
        if device_pixel_ratio <= 0:
            raise ValueError("device_pixel_ratio must be positive")

        page = self._document[page_index]
        try:
            scale = logical_zoom * device_pixel_ratio
            bitmap = page.render(scale=scale, rotation=rotation)
            pil_image = bitmap.to_pil().convert("RGBA")
            qimage = QImage(ImageQt(pil_image)).copy()
        finally:
            page.close()

        qimage.setDevicePixelRatio(device_pixel_ratio)
        return qimage

    def close(self) -> None:
        self._document.close()


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
    render_failed = Signal(object, int, int, str)
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
        self._pending_keys: set[tuple[str, int, int, float, int, float, DocumentRevision]] = set()
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
        self._drop_pending_for_document(document_id)

        try:
            backend = self._backend_factory(path)
            pages = tuple(backend.page_metadata(index) for index in range(backend.page_count()))
        except Exception as exc:
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

    @Slot(str, int)
    def close_document(self, document_id: str, generation: int) -> None:
        context = self._documents.get(document_id)
        if context is None:
            return
        if generation < context.generation:
            return
        self._drop_pending_for_document(document_id)
        self._close_document_backend(document_id)

    @Slot()
    def shutdown(self) -> None:
        if self._shutting_down:
            self.shutdown_completed.emit()
            return

        self._shutting_down = True
        self._pending_requests.clear()
        self._pending_keys.clear()
        for document_id in list(self._documents):
            self._close_document_backend(document_id)
        self._documents.clear()
        self._cache.clear()
        self.shutdown_completed.emit()

    def _schedule_processing(self) -> None:
        if self._processing or not self._pending_requests or self._shutting_down:
            return
        self._processing = True
        QTimer.singleShot(0, self._process_next)

    def _process_next(self) -> None:
        if self._shutting_down:
            self._processing = False
            return

        next_request = self._pop_next_valid_request()
        if next_request is None:
            self._processing = False
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
                    next_request.document_id,
                    next_request.generation,
                    next_request.page_index,
                    str(exc),
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
        if self._pending_requests and not self._shutting_down:
            self._schedule_processing()

    def _pop_next_valid_request(self) -> RenderRequest | None:
        while self._pending_requests:
            queued_request = self._pending_requests.pop(0)
            request = queued_request.request
            self._pending_keys.discard(self._request_key(request))
            if self._is_request_current(request):
                return request
        return None

    def _is_request_current(self, request: RenderRequest) -> bool:
        if self._shutting_down:
            return False
        context = self._documents.get(request.document_id)
        if context is None:
            return False
        return request.generation == context.generation and request.revision == context.revision

    def _drop_pending_for_document(self, document_id: str) -> None:
        kept_requests: list[QueuedRenderRequest] = []
        self._pending_keys.clear()
        for queued_request in self._pending_requests:
            if queued_request.request.document_id == document_id:
                continue
            kept_requests.append(queued_request)
            self._pending_keys.add(self._request_key(queued_request.request))
        self._pending_requests = kept_requests

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


class PdfRenderService(QObject):
    document_loaded = Signal(object, int, object)
    document_failed = Signal(object, int, str)
    render_succeeded = Signal(object)
    render_failed = Signal(object, int, int, str)
    _open_requested = Signal(str, Path, int, object)
    _render_requested = Signal(object)
    _close_requested = Signal(str, int)
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
        self._worker.shutdown_completed.connect(self._thread.quit)
        self._open_requested.connect(self._worker.open_document)
        self._render_requested.connect(self._worker.enqueue_render)
        self._close_requested.connect(self._worker.close_document)
        self._shutdown_requested.connect(
            self._worker.shutdown,
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        self._thread.start()
        self._is_shutdown = False

    def open_document(
        self,
        document_id: str,
        path: Path,
        generation: int,
        revision: DocumentRevision,
    ) -> None:
        if self._is_shutdown:
            return
        self._open_requested.emit(document_id, path, generation, revision)

    def request_render(self, request: RenderRequest) -> None:
        if self._is_shutdown:
            return
        self._render_requested.emit(request)

    def close_document(self, document_id: str, generation: int) -> None:
        if self._is_shutdown:
            return
        self._close_requested.emit(document_id, generation)

    def shutdown(self, timeout_ms: int = 3000) -> None:
        if self._is_shutdown:
            return
        self._is_shutdown = True
        if not self._thread.isRunning():
            return
        self._shutdown_requested.emit()
        if not self._thread.wait(timeout_ms):
            logger.warning("Timed out while waiting for PdfRenderService worker thread shutdown")
