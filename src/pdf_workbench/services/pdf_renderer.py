from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QImage


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
_shared_cache = RenderImageCache(DEFAULT_RENDER_CACHE_BYTES)


def shared_render_cache() -> RenderImageCache:
    return _shared_cache


class PdfRenderWorker(QObject):
    document_loaded = Signal(object, int, object)
    document_failed = Signal(object, int, str)
    render_succeeded = Signal(object)
    render_failed = Signal(object, int, int, str)

    def __init__(
        self,
        backend_factory: PdfBackendFactory,
        cache: RenderImageCache,
    ) -> None:
        super().__init__()
        self._backend_factory = backend_factory
        self._cache = cache
        self._backend: PdfDocumentBackend | None = None
        self._document_id: str | None = None
        self._generation = -1
        self._closed = False
        self._pending_requests: list[RenderRequest] = []
        self._processing = False

    @Slot(str, Path, int, object)
    def open_document(
        self,
        document_id: str,
        path: Path,
        generation: int,
        revision: object,
    ) -> None:
        if self._closed:
            self.document_failed.emit(document_id, generation, "renderer is shutting down")
            return
        if not isinstance(revision, DocumentRevision):
            raise TypeError("revision must be DocumentRevision")

        self._clear_pending()
        self._close_backend()
        self._document_id = document_id
        self._generation = generation
        try:
            backend = self._backend_factory(path)
            pages = tuple(backend.page_metadata(index) for index in range(backend.page_count()))
            metadata = DocumentMetadata(revision=revision, pages=pages)
        except Exception as exc:
            self._backend = None
            self.document_failed.emit(document_id, generation, str(exc))
            return

        self._backend = backend
        self.document_loaded.emit(document_id, generation, metadata)

    @Slot(object)
    def enqueue_render(self, request: object) -> None:
        if self._closed:
            return
        if not isinstance(request, RenderRequest):
            raise TypeError("request must be RenderRequest")
        if request.document_id != self._document_id or request.generation != self._generation:
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

        for index, pending in enumerate(self._pending_requests):
            if (
                pending.document_id == request.document_id
                and pending.generation == request.generation
                and pending.page_index == request.page_index
            ):
                if request.priority < pending.priority:
                    self._pending_requests[index] = request
                break
        else:
            self._pending_requests.append(request)
        self._pending_requests.sort(key=lambda item: (item.priority, item.page_index))
        self._schedule_processing()

    @Slot(str, int)
    def close_document(self, document_id: str, generation: int) -> None:
        if document_id == self._document_id and generation >= self._generation:
            self._generation = generation
            self._clear_pending()
            self._close_backend()
            self._document_id = None

    @Slot()
    def shutdown(self) -> None:
        self._closed = True
        self._clear_pending()
        self._close_backend()

    def _schedule_processing(self) -> None:
        if self._processing or not self._pending_requests:
            return
        self._processing = True
        QTimer.singleShot(0, self._process_next)

    def _process_next(self) -> None:
        if self._closed:
            self._processing = False
            return
        if self._backend is None or self._document_id is None:
            self._processing = False
            return
        while self._pending_requests:
            request = self._pending_requests.pop(0)
            if request.document_id != self._document_id or request.generation != self._generation:
                continue
            try:
                image = self._backend.render_page(
                    request.page_index,
                    request.logical_zoom,
                    request.rotation,
                    request.device_pixel_ratio,
                )
            except Exception as exc:
                self.render_failed.emit(
                    request.document_id,
                    request.generation,
                    request.page_index,
                    str(exc),
                )
            else:
                self._cache.put(request.cache_key, image)
                self.render_succeeded.emit(
                    RenderResult(
                        document_id=request.document_id,
                        generation=request.generation,
                        page_index=request.page_index,
                        image=image,
                        cache_key=request.cache_key,
                    )
                )
            break

        self._processing = False
        if self._pending_requests:
            self._schedule_processing()

    def _clear_pending(self) -> None:
        self._pending_requests.clear()

    def _close_backend(self) -> None:
        if self._backend is None:
            return
        self._backend.close()
        self._backend = None


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
        self._thread = QThread()
        self._worker = PdfRenderWorker(
            backend_factory=backend_factory or PdfiumDocumentBackend,
            cache=cache or shared_render_cache(),
        )
        self._worker.moveToThread(self._thread)
        self._worker.document_loaded.connect(self.document_loaded)
        self._worker.document_failed.connect(self.document_failed)
        self._worker.render_succeeded.connect(self.render_succeeded)
        self._worker.render_failed.connect(self.render_failed)
        self._open_requested.connect(self._worker.open_document)
        self._render_requested.connect(self._worker.enqueue_render)
        self._close_requested.connect(self._worker.close_document)
        self._shutdown_requested.connect(self._worker.shutdown)
        self._thread.start()

    def open_document(
        self,
        document_id: str,
        path: Path,
        generation: int,
        revision: DocumentRevision,
    ) -> None:
        self._open_requested.emit(document_id, path, generation, revision)

    def request_render(self, request: RenderRequest) -> None:
        self._render_requested.emit(request)

    def close_document(self, document_id: str, generation: int) -> None:
        self._close_requested.emit(document_id, generation)

    def shutdown(self, timeout_ms: int = 3000) -> None:
        if not self._thread.isRunning():
            return
        self._shutdown_requested.emit()
        self._thread.quit()
        self._thread.wait(timeout_ms)
