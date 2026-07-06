"""Tray UI submodules for plaud_tools.

The public entry point is :func:`plaud_tools.tray.app.main` (the ``plaud-tray``
console script). Tests and the PyInstaller entry script import directly from
the relevant submodule (``tray.app``, ``tray.setup``, ``tray.toasts``, ...).
"""

from .app import main

__all__ = ["main"]
