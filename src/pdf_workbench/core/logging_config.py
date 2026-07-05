from __future__ import annotations

import logging
from pathlib import Path

from platformdirs import user_log_dir


def configure_logging() -> Path:
    log_dir = Path(user_log_dir("PDF Workbench", "m0ka-Lv98"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "pdf-workbench.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    return log_path
