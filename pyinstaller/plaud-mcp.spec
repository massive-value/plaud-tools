# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for plaud-mcp.exe (MCP server).
#
# Build prerequisite: refresh PE VERSIONINFO from plaud_tools.__version__
#   python pyinstaller/version_info.py
#
# Build from the repo root:
#   pyinstaller pyinstaller/plaud-mcp.spec --distpath out/plaud-mcp
#
# Produces out/plaud-mcp/plaud-mcp/ (onedir). The Electron tray bundles this
# directory as resources/plaud-mcp/ and copies ffmpeg.exe into it so the
# sibling lookup in transcode._find_ffmpeg() resolves correctly.
# AI clients call the server as:
#   path.join(process.resourcesPath, 'plaud-mcp', 'plaud-mcp', 'plaud-mcp.exe')

from pathlib import Path

block_cipher = None
src = Path(SPECPATH).parent / 'src'
_icon = str(Path(SPECPATH).parent / 'src' / 'plaud_tools' / 'assets' / 'icon.ico')

a = Analysis(
    [str(Path(SPECPATH).parent / 'scripts' / 'plaud_mcp_entry.py')],
    pathex=[str(src)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # keyring runtime backend selection
        'keyring.backends.Windows',
        'keyring.backends.fail',
        'keyring.core',
        # anyio selects its async backend at runtime; asyncio backend is required
        'anyio._backends._asyncio',
        # mcp internal modules that may not be reached by static analysis
        'mcp.server.stdio',
        'mcp.server.lowlevel',
        'mcp.server.models',
        # pydantic v2 uses compiled validators loaded dynamically
        'pydantic.v1',
        # pywin32 / win32 used by keyring on Windows
        'win32api',
        'win32con',
        'win32cred',
        'pywintypes',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='plaud-mcp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=_icon,
    version=str(Path(SPECPATH) / 'version_info_plaud-mcp.txt'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='plaud-mcp',
)
