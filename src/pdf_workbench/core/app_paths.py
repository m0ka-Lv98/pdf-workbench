from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir, user_log_dir

APP_NAME = "PDF Workbench"
APP_AUTHOR = "m0ka-Lv98"


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    log_dir: Path


def get_app_paths() -> AppPaths:
    return AppPaths(
        config_dir=Path(user_config_dir(APP_NAME, APP_AUTHOR)),
        log_dir=Path(user_log_dir(APP_NAME, APP_AUTHOR)),
    )


def ensure_app_directories() -> AppPaths:
    paths = get_app_paths()
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    return paths
