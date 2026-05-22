"""Windows toast helpers (first-run + session-expired).

Each helper tries the modern ``winrt`` / ``winsdk`` package first, then falls
back to a hidden PowerShell snippet.  Any failure is logged and silently
ignored — toasts are a nicety, not a hard dependency.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from typing import Callable

from .setup import APP_NAME


def _show_session_expired_toast(on_click: "Callable | None" = None) -> None:
    """Show a Windows toast notifying the user their Plaud session expired.

    The ``on_click`` callback (if provided) is invoked when the toast is
    activated — note: winrt activation callbacks require WinRT message-loop
    integration that is not available here, so on_click is only honoured by
    callers who invoke LoginWindow directly after the toast.
    """
    title = APP_NAME
    message = "Plaud session expired — click the tray icon to sign in again."

    # --- attempt 1: winrt / winsdk ---
    try:
        from winrt.windows.ui.notifications import (  # type: ignore[import]
            ToastNotificationManager,
            ToastNotification,
        )
        from winrt.windows.data.xml.dom import XmlDocument  # type: ignore[import]

        app_id = "PlaudTools.TrayApp"
        xml_str = (
            "<toast>"
            f"<visual><binding template='ToastGeneric'>"
            f"<text>{title}</text>"
            f"<text>{message}</text>"
            "</binding></visual>"
            "</toast>"
        )
        doc = XmlDocument()
        doc.load_xml(xml_str)
        notifier = ToastNotificationManager.create_toast_notifier(app_id)
        notifier.show(ToastNotification(doc))
        logging.info("Session-expired toast shown via winrt")
        return
    except Exception:
        logging.debug("winrt toast unavailable, falling back to PowerShell", exc_info=True)

    # --- attempt 2: hidden PowerShell snippet ---
    if sys.platform != "win32":
        return
    try:
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null\n"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null\n"
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
            "$xml.LoadXml('<toast>"
            "<visual><binding template=\"ToastGeneric\">"
            f"<text>{title}</text>"
            f"<text>{message}</text>"
            "</binding></visual></toast>')\n"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)\n"
            "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PlaudTools.TrayApp')\n"
            "$notifier.Show($toast)\n"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        logging.info("Session-expired toast dispatched via PowerShell")
    except Exception:
        logging.warning("Could not show session-expired toast notification", exc_info=True)


def _show_install_toast() -> None:
    """Show a Windows 11 toast notification explaining the tray icon.

    Any failure is logged and silently ignored so it never blocks the tray
    from starting.
    """
    title = APP_NAME
    message = (
        "PlaudTools is now running in your system tray — "
        "click the icon to sign in."
    )

    # --- attempt 1: winrt / winsdk ---
    try:
        from winrt.windows.ui.notifications import (  # type: ignore[import]
            ToastNotificationManager,
            ToastNotification,
        )
        from winrt.windows.data.xml.dom import XmlDocument  # type: ignore[import]

        app_id = "PlaudTools.TrayApp"
        xml_str = (
            "<toast>"
            f"<visual><binding template='ToastGeneric'>"
            f"<text>{title}</text>"
            f"<text>{message}</text>"
            "</binding></visual>"
            "</toast>"
        )
        doc = XmlDocument()
        doc.load_xml(xml_str)
        notifier = ToastNotificationManager.create_toast_notifier(app_id)
        notifier.show(ToastNotification(doc))
        logging.info("First-run toast shown via winrt")
        return
    except Exception:
        logging.debug("winrt toast unavailable, falling back to PowerShell", exc_info=True)

    # --- attempt 2: hidden PowerShell snippet ---
    if sys.platform != "win32":
        return
    try:
        ps_script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null\n"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime] | Out-Null\n"
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
            "$xml.LoadXml('<toast>"
            "<visual><binding template=\"ToastGeneric\">"
            f"<text>{title}</text>"
            f"<text>{message}</text>"
            "</binding></visual></toast>')\n"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)\n"
            "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PlaudTools.TrayApp')\n"
            "$notifier.Show($toast)\n"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        logging.info("First-run toast dispatched via PowerShell")
    except Exception:
        logging.warning("Could not show first-run toast notification", exc_info=True)


__all__ = ["_show_session_expired_toast", "_show_install_toast"]
