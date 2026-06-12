"""Tray UI submodules for plaud_tools.

The public entry point is :func:`plaud_tools.tray.app.main`.  This package is
the home of the tray code; ``plaud_tools.tray_app`` is a thin compatibility
shim that re-exports the public surface for tests, PyInstaller, and the rest
of the codebase.
"""

from .app import main

__all__ = ["main"]
