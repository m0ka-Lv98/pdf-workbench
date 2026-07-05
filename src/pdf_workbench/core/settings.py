from __future__ import annotations

from PySide6.QtCore import QSettings

from pdf_workbench.core.app_paths import APP_NAME, ensure_app_directories


def configure_qsettings() -> QSettings:
    paths = ensure_app_directories()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(paths.config_dir))
    return QSettings(QSettings.Scope.UserScope, APP_NAME, APP_NAME)
