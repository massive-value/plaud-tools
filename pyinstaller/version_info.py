"""Generate Windows PE VERSIONINFO files for PyInstaller from plaud_tools.__version__.

Run this before invoking pyinstaller:

    python pyinstaller/version_info.py
    pyinstaller pyinstaller/plaud-mcp.spec --distpath out/plaud-mcp

It writes three files (gitignored) that each .spec passes to EXE(version=...).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from plaud_tools import __version__  # noqa: E402


def _parse_version(v: str) -> tuple[int, int, int, int]:
    parts = v.split("+")[0].split(".")
    nums: list[int] = []
    for p in parts[:4]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 4:
        nums.append(0)
    return (nums[0], nums[1], nums[2], nums[3])


def render(
    *,
    product_name: str,
    internal_name: str,
    file_description: str,
    original_filename: str,
) -> str:
    v = _parse_version(__version__)
    v_str = ", ".join(str(n) for n in v)
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v_str}),
    prodvers=({v_str}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'Plaud Tools'),
         StringStruct('FileDescription', '{file_description}'),
         StringStruct('FileVersion', '{__version__}'),
         StringStruct('InternalName', '{internal_name}'),
         StringStruct('OriginalFilename', '{original_filename}'),
         StringStruct('ProductName', '{product_name}'),
         StringStruct('ProductVersion', '{__version__}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def write_for(
    name: str,
    *,
    product_name: str,
    internal_name: str,
    file_description: str,
    original_filename: str,
) -> Path:
    out = Path(__file__).parent / f"version_info_{name}.txt"
    out.write_text(
        render(
            product_name=product_name,
            internal_name=internal_name,
            file_description=file_description,
            original_filename=original_filename,
        ),
        encoding="utf-8",
    )
    return out


def main() -> None:
    write_for(
        "plaud-mcp",
        product_name="Plaud Tools MCP",
        internal_name="plaud-mcp",
        file_description="Plaud Tools MCP Server",
        original_filename="plaud-mcp.exe",
    )
    write_for(
        "plaud-tray",
        product_name="Plaud Tools",
        internal_name="PlaudTools",
        file_description="Plaud Tools Tray App",
        original_filename="PlaudTools.exe",
    )
    write_for(
        "plaud",
        product_name="Plaud Tools CLI",
        internal_name="plaud-tools",
        file_description="Plaud Tools CLI",
        original_filename="plaud-tools.exe",
    )
    print(f"Wrote version_info files for version {__version__}")


if __name__ == "__main__":
    main()
