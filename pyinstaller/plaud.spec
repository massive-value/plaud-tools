# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for plaud.exe (CLI).
#
# Build from the repo root:
#   pyinstaller pyinstaller/plaud.spec --distpath out/plaud-cli
#
# Produces out/plaud-cli/plaud/ (onedir). The Electron tray bundles this
# directory as resources/plaud-cli/ and calls it as:
#   path.join(process.resourcesPath, 'plaud-cli', 'plaud', 'plaud.exe')

from pathlib import Path

block_cipher = None
src = Path(SPECPATH).parent / 'src'

a = Analysis(
    [str(src / 'plaud_tools' / '__main__.py')],
    pathex=[str(src)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # transcode is imported inside an if-branch; static analysis misses it
        'plaud_tools.transcode',
        # keyring selects its backend at runtime
        'keyring.backends.Windows',
        'keyring.backends.fail',
        'keyring.core',
    ],
    hookspath=[],
    runtime_hooks=[],
    # mcp and its heavy async stack are not needed for the CLI
    excludes=['mcp', 'anyio', 'starlette', 'pydantic', 'httpx', 'uvicorn'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='plaud',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='plaud',
)
