# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

project_root = Path(SPECPATH).parent
pypdfium_datas, pypdfium_binaries, pypdfium_hiddenimports = collect_all("pypdfium2")
style_datas, _style_binaries, style_hiddenimports = collect_all("pdf_workbench.ui.styles")
icon_datas, _icon_binaries, icon_hiddenimports = collect_all("pdf_workbench.ui.icons")

analysis = Analysis(
    [str(project_root / "src" / "pdf_workbench" / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=pypdfium_binaries,
    datas=pypdfium_datas + style_datas + icon_datas,
    hiddenimports=pypdfium_hiddenimports + style_hiddenimports + icon_hiddenimports,
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
    analysis.binaries,
    analysis.datas,
    [],
    name="PDF Workbench",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
