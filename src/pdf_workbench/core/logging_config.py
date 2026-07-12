from __future__ import annotations

import logging
from pathlib import Path

from pdf_workbench.core.app_paths import ensure_app_directories


def configure_logging() -> Path:
    log_path = ensure_app_directories().log_dir / "pdf-workbench.log"
    stream_handler = logging.StreamHandler()
    handlers: list[logging.Handler] = [stream_handler]

    try:
        handlers.insert(0, logging.FileHandler(log_path, encoding="utf-8"))
    except OSError as exc:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=handlers,
            force=True,
        )
        logging.getLogger(__name__).warning(
            "File logging disabled for %s: %s",
            log_path,
            exc,
        )
        return log_path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_path
