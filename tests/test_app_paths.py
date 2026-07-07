from __future__ import annotations

import logging
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from pdf_workbench.core import app_paths
from pdf_workbench.core.app_paths import APP_NAME, ensure_app_directories
from pdf_workbench.core.logging_config import configure_logging
from pdf_workbench.core.settings import configure_qsettings


def test_configure_logging_writes_to_user_log_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app_paths, "user_config_dir", lambda *_args: str(tmp_path / "config"))
    monkeypatch.setattr(app_paths, "user_log_dir", lambda *_args: str(tmp_path / "logs"))

    log_path = configure_logging()

    assert log_path.parent == ensure_app_directories().log_dir
    assert log_path.name == "pdf-workbench.log"
    assert log_path.parent.exists()


def test_configure_logging_falls_back_to_stream_when_file_handler_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app_paths, "user_config_dir", lambda *_args: str(tmp_path / "config"))
    monkeypatch.setattr(app_paths, "user_log_dir", lambda *_args: str(tmp_path / "logs"))

    def fail_file_handler(*_args: object, **_kwargs: object) -> logging.Handler:
        raise PermissionError("denied")

    monkeypatch.setattr(logging, "FileHandler", fail_file_handler)

    log_path = configure_logging()

    assert log_path == ensure_app_directories().log_dir / "pdf-workbench.log"
    assert any(
        isinstance(handler, logging.StreamHandler) for handler in logging.getLogger().handlers
    )


def test_configure_qsettings_uses_user_config_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(app_paths, "user_config_dir", lambda *_args: str(tmp_path / "config"))
    monkeypatch.setattr(app_paths, "user_log_dir", lambda *_args: str(tmp_path / "logs"))

    settings = configure_qsettings()

    expected_dir = ensure_app_directories().config_dir
    assert Path(settings.fileName()) == expected_dir / f"{APP_NAME}.ini"
    assert Path(settings.fileName()).parent == expected_dir
    assert settings.format() == QSettings.Format.IniFormat
