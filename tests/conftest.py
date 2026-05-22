import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Stub `[tray]` extras so `plaud_tools.tray_app` is importable in CI environments
# that only install `[dev]`. tray_app does `import pystray` and `from PIL import ...`
# at module top, which means any test that imports tray_app would otherwise fail
# with ModuleNotFoundError. Per-test patches can still override these stubs.
for _name in (
    "pystray",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
):
    sys.modules.setdefault(_name, MagicMock())
