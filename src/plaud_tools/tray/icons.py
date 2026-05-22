"""Tray icon loading — bundled PNGs with a Pillow ellipse fallback."""
from __future__ import annotations

from PIL import Image

from .setup import _assets_path


def _load_icon(filename: str, fallback_color: str) -> Image.Image:
    from PIL import ImageDraw
    path = _assets_path() / filename
    if path.exists():
        return Image.open(path).convert("RGBA")
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([4, 4, 60, 60], fill=fallback_color)
    return img


def _load_icons() -> dict[str, Image.Image]:
    return {
        "signed-out": _load_icon("tray-signed-out.png", "#888888"),
        "signed-in":  _load_icon("tray-signed-in.png",  "#27ae60"),
        "expiring":   _load_icon("tray-expiring.png",   "#f39c12"),
        "expired":    _load_icon("tray-expired.png",    "#e74c3c"),
    }


__all__ = ["_load_icon", "_load_icons"]
