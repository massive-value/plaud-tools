# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for PlaudTools.exe (system tray app).
#
# Build from the repo root (version is read automatically from pyproject.toml):
#   pip install -e . --no-deps
#   pyinstaller pyinstaller/plaud-tray.spec --noconfirm
#
# Produces dist/PlaudTools/ (onedir). The release workflow then:
#   1. Copies out/plaud-mcp/plaud-mcp/ into dist/PlaudTools/mcp/
#   2. Places ffmpeg.exe into dist/PlaudTools/mcp/
#   3. Zips dist/PlaudTools/ -> PlaudTools.zip
#
# The tray app locates plaud-mcp.exe via:
#   Path(sys.executable).parent / "mcp" / "plaud-mcp.exe"

import tomllib
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

# --- Version from pyproject.toml (single source of truth) ---
_repo = Path(SPECPATH).parent
with open(str(_repo / 'pyproject.toml'), 'rb') as _f:
    _ver_str = tomllib.load(_f)['project']['version']

def _ver_tuple(v: str) -> tuple:
    parts = v.split('+')[0].split('.')
    nums = [int(p) if p.isdigit() else 0 for p in parts[:4]]
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums)

_vt = _ver_tuple(_ver_str)
_vi_content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_vt}, prodvers={_vt},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Plaud Tools'),
      StringStruct('FileDescription', 'Plaud Tools Tray App'),
      StringStruct('FileVersion', '{_ver_str}'),
      StringStruct('InternalName', 'PlaudTools'),
      StringStruct('OriginalFilename', 'PlaudTools.exe'),
      StringStruct('ProductName', 'Plaud Tools'),
      StringStruct('ProductVersion', '{_ver_str}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
_vi_path = _repo / 'pyinstaller' / 'version_info_plaud-tray.txt'
_vi_path.write_text(_vi_content, encoding='utf-8')

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
