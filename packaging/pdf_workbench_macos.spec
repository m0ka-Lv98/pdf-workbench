# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from tomllib import load as toml_load

from PyInstaller.utils.hooks import collect_all


def _project_version() -> str:
    pyproject_path = Path(SPECPATH).parent / "pyproject.toml"
    with pyproject_path.open("rb") as stream:
        project = toml_load(stream)["project"]
    return str(project["version"])


project_root = Path(SPECPATH).parent
project_version = _project_version()
pypdfium_datas, pypdfium_binaries, pypdfium_hiddenimports = collect_all("pypdfium2")
theme_datas, _, theme_hiddenimports = collect_all("pdf_workbench.ui.styles")

analysis = Analysis(
    [str(project_root / "src" / "pdf_workbench" / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=pypdfium_binaries,
    datas=[*pypdfium_datas, *theme_datas],
    hiddenimports=[*pypdfium_hiddenimports, *theme_hiddenimports],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PDF Workbench",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
app = BUNDLE(
    COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        name="PDF Workbench",
    ),
    name="PDF Workbench.app",
    bundle_identifier="com.m0kalv98.pdfworkbench",
    info_plist={
        "CFBundleName": "PDF Workbench",
        "CFBundleDisplayName": "PDF Workbench",
        "CFBundleShortVersionString": project_version,
        "CFBundleVersion": project_version,
        "NSPrincipalClass": "NSApplication",
        "NSHighResolutionCapable": True,
    },
    icon=None,
    argv_emulation=False,
)
