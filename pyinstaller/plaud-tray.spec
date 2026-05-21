# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for PlaudTools.exe (system tray app).
#
# Build prerequisite: refresh PE VERSIONINFO from plaud_tools.__version__
#   python pyinstaller/version_info.py
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
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

block_cipher = None
src = Path(SPECPATH).parent / 'src'

_assets = str(Path(SPECPATH).parent / 'src' / 'plaud_tools' / 'assets')
_completions = str(Path(SPECPATH).parent / 'src' / 'plaud_tools' / 'completions')
_icon = str(Path(SPECPATH).parent / 'src' / 'plaud_tools' / 'assets' / 'icon.ico')

# sv_ttk ships a Tcl theme file (sun-valley.tcl + sibling .tcl files) as
# package data — collect it explicitly so the frozen build can apply the
# theme at runtime.
_sv_ttk_data = collect_data_files('sv_ttk')

# Bundle the plaud-tools dist-info so importlib.metadata.version() resolves
# at runtime — without this the tray footer shows "v0.0.0+dev".
_plaud_metadata = copy_metadata('plaud-tools')

a = Analysis(
    [str(Path(SPECPATH).parent / 'scripts' / 'plaud_tray_entry.py')],
    pathex=[str(src)],
    binaries=[],
    datas=[(_assets, 'assets'), (_completions, 'completions'), *_sv_ttk_data, *_plaud_metadata],
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
        # sv_ttk applies a Tcl theme at runtime
        'sv_ttk',
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
    upx=False,
    console=False,
    uac_admin=False,
    icon=_icon,
    version=str(Path(SPECPATH) / 'version_info_plaud-tray.txt'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='PlaudTools',
)
