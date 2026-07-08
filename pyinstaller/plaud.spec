# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for plaud.exe (CLI).
#
# Build prerequisite: refresh PE VERSIONINFO from plaud_tools.__version__
#   python pyinstaller/version_info.py
#
# Build from the repo root:
#   pyinstaller pyinstaller/plaud.spec --distpath out/plaud-cli
#
# Produces out/plaud-cli/plaud/ (onedir). The Electron tray bundles this
# directory as resources/plaud-cli/ and calls it as:
#   path.join(process.resourcesPath, 'plaud-cli', 'plaud', 'plaud.exe')

from pathlib import Path
from PyInstaller.utils.hooks import copy_metadata

block_cipher = None
src = Path(SPECPATH).parent / 'src'

# Bundle the plaud-tools dist-info so importlib.metadata.version() resolves
# at runtime (used by `plaud --version`).
_plaud_metadata = copy_metadata('plaud-tools')

a = Analysis(
    [str(Path(SPECPATH).parent / 'scripts' / 'plaud_entry.py')],
    pathex=[str(src)],
    binaries=[],
    datas=[*_plaud_metadata],
    hiddenimports=[
        # transcode is imported inside an if-branch; static analysis misses it
        'plaud_tools.core.transcode',
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
    name='plaud-tools',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    version=str(Path(SPECPATH) / 'version_info_plaud.txt'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='plaud-tools',
)
