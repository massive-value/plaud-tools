"""Windows toast helpers (first-run + session-expired).

The frozen bundle ships without the ``winrt`` package, so every shipped
install uses the PowerShell fallback. When ``winrt`` is importable (dev
environments where a contributor has installed ``winrt-runtime``) the
in-process path is used instead — slightly faster and avoids spawning a
hidden PowerShell process per notification.

``winrt`` availability is detected **once** at module load time. Earlier
versions attempted the import per call and logged the ImportError traceback
at DEBUG level, which produced multi-line tracebacks in ``tray.log`` for
every toast on bundles without ``winrt``.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

from .process_launch import POWERSHELL_EXE as _POWERSHELL_EXE
from .process_launch import launch_hidden_powershell
from .setup import APP_NAME

# ---------------------------------------------------------------------------
# One-shot winrt detection
# ---------------------------------------------------------------------------

try:
    from winrt.windows.data.xml.dom import XmlDocument as _WINRT_XML  # type: ignore[import]
    from winrt.windows.ui.notifications import (
        ToastNotification as _WINRT_TN,
    )
    from winrt.windows.ui.notifications import (  # type: ignore[import]
        ToastNotificationManager as _WINRT_TNM,
    )

    _WINRT_AVAILABLE = True
except Exception:
    _WINRT_TNM = None  # type: ignore[assignment]
    _WINRT_TN = None  # type: ignore[assignment]
    _WINRT_XML = None  # type: ignore[assignment]
    _WINRT_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _show_winrt_toast(title: str, message: str, info_log: str) -> bool:
    """Try the in-process winrt path. Return True on success.

    Returns False (and silently bails) when ``winrt`` is not importable. If
    ``winrt`` IS importable but blew up at runtime, that's anomalous and is
    logged as a warning before returning False so the caller can fall back.
    """
    if not _WINRT_AVAILABLE:
        return False
    try:
        xml_str = (
            "<toast>"
            f"<visual><binding template='ToastGeneric'>"
            f"<text>{title}</text>"
            f"<text>{message}</text>"
            "</binding></visual>"
            "</toast>"
        )
        doc = _WINRT_XML()  # type: ignore[misc]
        doc.load_xml(xml_str)
        notifier = _WINRT_TNM.create_toast_notifier("PlaudTools.TrayApp")  # type: ignore[union-attr]
        notifier.show(_WINRT_TN(doc))  # type: ignore[misc]
        logging.info(info_log)
        return True
    except Exception:
        logging.warning("winrt toast attempt failed; falling back to PowerShell", exc_info=True)
        return False


def _show_powershell_toast(title: str, message: str, info_log: str) -> None:
    if sys.platform != "win32":
        return
    try:
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null\n"  # noqa: E501
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null\n"  # noqa: E501
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
            "$xml.LoadXml('<toast>"
            '<visual><binding template="ToastGeneric">'
            f"<text>{title}</text>"
            f"<text>{message}</text>"
            "</binding></visual></toast>')\n"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)\n"
            "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PlaudTools.TrayApp')\n"  # noqa: E501
            "$notifier.Show($toast)\n"
        )
        # DETACHED_PROCESS gives this no-console frozen tray's child NULL
        # stdio, crashing PowerShell before it shows the toast -- no toast
        # ever appeared in the shipped bundle (#142). launch_hidden_powershell
        # uses CREATE_NO_WINDOW + explicit DEVNULL handles instead.
        launch_hidden_powershell(
            [_POWERSHELL_EXE, "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script]
        )
        logging.info(info_log)
    except Exception:
        logging.warning("Could not show toast notification", exc_info=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _show_session_expired_toast(on_click: Callable | None = None) -> None:
    """Show a Windows toast notifying the user their Plaud session expired.

    Clicking the toast invokes the COM activator (issue #83), which signals
    _ACTIVATE_EVENT so the running tray opens the login window.  The
    ``on_click`` parameter is retained for callers that pass it positionally.
    """
    title = APP_NAME
    message = "Plaud session expired — click here to sign in again."
    if _show_winrt_toast(title, message, "Session-expired toast shown via winrt"):
        return
    _show_powershell_toast(title, message, "Session-expired toast dispatched via PowerShell")


def _show_install_toast() -> None:
    """Show a Windows 11 toast notification explaining the tray icon."""
    title = APP_NAME
    message = "PlaudTools is now running in your system tray — click the icon to sign in."
    if _show_winrt_toast(title, message, "First-run toast shown via winrt"):
        return
    _show_powershell_toast(title, message, "First-run toast dispatched via PowerShell")


def _show_update_available_toast(version: str) -> None:
    """Show a Windows toast notifying the user that a new version is available."""
    title = f"{APP_NAME} — Update Available"
    message = f"v{version} is ready — click here to install."
    if _show_winrt_toast(title, message, f"Update-available toast shown via winrt (v{version})"):
        return
    _show_powershell_toast(title, message, f"Update-available toast dispatched via PowerShell (v{version})")


__all__ = ["_show_session_expired_toast", "_show_install_toast", "_show_update_available_toast"]
