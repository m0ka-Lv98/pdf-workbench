from __future__ import annotations

from pathlib import Path

import pytest

from pdf_workbench.core import app_paths


@pytest.fixture(autouse=True)
def isolate_app_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "app-data"
    monkeypatch.setattr(app_paths, "user_cache_dir", lambda *_args: str(root / "cache"))
    monkeypatch.setattr(app_paths, "user_config_dir", lambda *_args: str(root / "config"))
    monkeypatch.setattr(app_paths, "user_log_dir", lambda *_args: str(root / "logs"))
