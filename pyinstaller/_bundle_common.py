# Shared settings for the three PyInstaller specs (plaud-mcp, plaud, plaud-tray).
# Each spec puts SPECPATH on sys.path and imports from here.
from pathlib import Path

# Keeps stdlib `platform` off WMI. Custom runtime hooks run before PyInstaller's
# own; see src/plaud_tools/core/platform_guard.py for why.
WMI_RUNTIME_HOOK = str(Path(__file__).parent / "rth_disable_wmi.py")

# Build/dev tooling that PyInstaller's import scan drags in through optional
# imports (pydantic's mypy plugin, cffi's distutils shim) plus PyInstaller's
# own setuptools runtime hook. Nothing in the shipped app imports these at runtime.
DEV_ONLY_EXCLUDES = [
    "setuptools",
    "pkg_resources",
    "mypy",
    "mypyc",
    "ast_serialize",
    "librt",
    "pytest",
    "_pytest",
    "PyInstaller",
    "pip",
]
