from __future__ import annotations

from PySide6.QtCore import QSettings

from pdf_workbench.core.app_paths import APP_NAME, ensure_app_directories


def configure_qsettings() -> QSettings:
    paths = ensure_app_directories()
    settings_path = paths.config_dir / f"{APP_NAME}.ini"
    return QSettings(str(settings_path), QSettings.Format.IniFormat)
