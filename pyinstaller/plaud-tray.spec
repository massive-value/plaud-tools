# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for PlaudTools.exe (system tray app).
#
# Build from the repo root:
#   pyinstaller pyinstaller/plaud-tray.spec --distpath out/plaud-tray --noconfirm
#
# Produces out/plaud-tray/PlaudTools/ (onedir). The release workflow then:
#   1. Copies out/plaud-mcp/plaud-mcp/ into out/plaud-tray/PlaudTools/mcp/
#   2. Places ffmpeg.exe into out/plaud-tray/PlaudTools/mcp/
#   3. Zips out/plaud-tray/PlaudTools/ -> PlaudTools.zip
#
# The tray app locates plaud-mcp.exe via:
#   Path(sys.executable).parent / "mcp" / "plaud-mcp.exe"

from pathlib import Path

block_cipher = None
src = Path(SPECPATH).parent / 'src'

_assets = str(Path(SPECPATH).parent / 'src' / 'plaud_tools' / 'assets')
_icon = str(Path(SPECPATH).parent / 'src' / 'plaud_tools' / 'assets' / 'icon.ico')

a = Analysis(
    [str(Path(SPECPATH).parent / 'scripts' / 'plaud_tray_entry.py')],
    pathex=[str(src)],
    binaries=[],
    datas=[(_assets, 'assets')],
    hiddenimports=[
        # pystray selects its platform backend at runtime
        'pystray._win32',
        'pystray._util.win32',
        # Pillow plugins loaded at runtime
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.PngImagePlugin',
        'PIL.BmpImagePlugin',
        # keyring selects its backend at runtime
        'keyring.backends.Windows',
        'keyring.backends.fail',
        'keyring.core',
        # ai_clients imported inside tray_app at module level
        'plaud_tools.ai_clients',
        # pywin32 / win32 used by keyring and pystray on Windows
        'win32api',
        'win32con',
        'win32cred',
        'pywintypes',
    ],
    hookspath=[],
    runtime_hooks=[],
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
    name='PlaudTools',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    uac_admin=False,
    icon=_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PlaudTools',
)
