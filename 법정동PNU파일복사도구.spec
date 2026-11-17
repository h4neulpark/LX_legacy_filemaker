# -*- mode: python ; coding: utf-8 -*-
import runpy
from pathlib import Path

app_version = runpy.run_path(str(Path(SPECPATH) / 'version.py'))['APP_VERSION']

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('법정동코드 전체자료.txt', '.'), ('assets/ocr', 'assets/ocr')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f'법정동PNU파일복사도구_v{app_version}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
