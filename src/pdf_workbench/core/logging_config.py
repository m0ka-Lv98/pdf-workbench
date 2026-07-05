from __future__ import annotations

import logging
from pathlib import Path

from pdf_workbench.core.app_paths import ensure_app_directories


def configure_logging() -> Path:
    log_path = ensure_app_directories().log_dir / "pdf-workbench.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return log_path
