"""Windows toast helpers (first-run + session-expired).

Every toast is shown via a hidden PowerShell process (see
``_show_powershell_toast``): the frozen bundle never ships the ``winrt``
package, so an in-process winrt path was always dead code in the shipped
product. It was deleted in Wave 5 (2026-07-06 audit, §7.4) — the PowerShell
path already got the DETACHED_PROCESS fix (#142, Wave 3) that made it work
reliably from the no-console frozen tray, so nothing depended on winrt ever
actually running.
"""

from __future__ import annotations

import logging
import sys

from .process_launch import POWERSHELL_EXE as _POWERSHELL_EXE
from .process_launch import launch_hidden_powershell
from .setup import APP_NAME


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


def _show_session_expired_toast() -> None:
    """Show a Windows toast notifying the user their Plaud session expired.

    Clicking the toast invokes the COM activator (issue #83), which signals
    _ACTIVATE_EVENT so the running tray opens the login window.
    """
    title = APP_NAME
    message = "Plaud session expired — click here to sign in again."
    _show_powershell_toast(title, message, "Session-expired toast dispatched via PowerShell")


def _show_install_toast() -> None:
    """Show a Windows 11 toast notification explaining the tray icon."""
    title = APP_NAME
    message = "PlaudTools is now running in your system tray — click the icon to sign in."
    _show_powershell_toast(title, message, "First-run toast dispatched via PowerShell")


def _show_update_available_toast(version: str) -> None:
    """Show a Windows toast notifying the user that a new version is available."""
    title = f"{APP_NAME} — Update Available"
    message = f"v{version} is ready — click here to install."
    _show_powershell_toast(title, message, f"Update-available toast dispatched via PowerShell (v{version})")


__all__ = ["_show_session_expired_toast", "_show_install_toast", "_show_update_available_toast"]
