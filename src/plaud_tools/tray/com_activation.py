"""COM activation server for Windows toast-click events.

When the user clicks a Plaud Tools toast notification, Windows consults
``CustomActivator`` under the AUMID registry key, finds our CLSID, and
launches ``PlaudTools.exe --com-activate``. That process calls
``run_com_activator()``, which:

  1. Registers a COM class factory for ``_COM_ACTIVATOR_CLSID``
  2. Pumps Windows messages until ``INotificationActivationCallback.Activate``
     is called by the notification platform
  3. Signals ``_ACTIVATE_EVENT`` so the running tray instance opens its window
  4. Returns (process exits)

Non-Windows builds: every public symbol in this module is a no-op so that
``app.py`` can import it unconditionally.
"""
from __future__ import annotations

import ctypes
import logging
import sys

from .setup import _ACTIVATE_EVENT, _setup_logging

# IID for INotificationActivationCallback (Windows SDK NotificationActivationCallback.h)
_IID_INotificationActivationCallback = "{53E31837-6600-4A81-9395-75CFFE746F94}"

# Stable CLSID for PlaudTools notification activator — never regenerate at runtime.
_COM_ACTIVATOR_CLSID = "{DC6F6422-E7ED-4F4E-BBDE-8332A399DBD5}"


def _signal_activate_event() -> None:
    if sys.platform != "win32":
        return
    h = ctypes.windll.kernel32.OpenEventW(0x0002, False, _ACTIVATE_EVENT)  # EVENT_MODIFY_STATE
    if h:
        ctypes.windll.kernel32.SetEvent(h)
        ctypes.windll.kernel32.CloseHandle(h)
        logging.info("COM activator: signalled %s", _ACTIVATE_EVENT)
    else:
        logging.warning("COM activator: could not open %s (tray not running?)", _ACTIVATE_EVENT)


if sys.platform == "win32":
    import comtypes
    import comtypes.server.localserver

    class _INotificationActivationCallback(comtypes.IUnknown):
        _iid_ = comtypes.GUID(_IID_INotificationActivationCallback)
        _methods_ = [
            comtypes.COMMETHOD(
                [],
                ctypes.HRESULT,
                "Activate",
                (["in"], ctypes.c_wchar_p, "appUserModelId"),
                (["in"], ctypes.c_wchar_p, "invokedArgs"),
                (["in"], ctypes.c_void_p, "data"),
                (["in"], ctypes.c_ulong, "dataCount"),
            )
        ]

    class _PlaudToolsActivator(comtypes.CoClass):
        _reg_clsid_ = comtypes.GUID(_COM_ACTIVATOR_CLSID)
        _reg_progid_ = "PlaudTools.NotificationActivator"
        _com_interfaces_ = [_INotificationActivationCallback]
        _reg_threading_ = "Both"
        _reg_clsctx_ = comtypes.CLSCTX_SERVER  # required by ClassFactory._register_class()

        def Activate(self, appUserModelId, invokedArgs, data, dataCount):
            logging.info(
                "COM activator: Activate called (aumid=%r args=%r)",
                appUserModelId,
                invokedArgs,
            )
            _signal_activate_event()
            return 0  # S_OK


def run_com_activator() -> None:
    """Register the COM class factory, wait for Activate(), then return.

    Must be called from the ``--com-activate`` startup path in ``main()``.
    Blocks until the notification platform calls ``Activate()`` and releases
    the COM object, which triggers the message-loop exit.
    """
    if sys.platform != "win32":
        return
    _setup_logging()
    logging.info("COM activator: starting local server (CLSID %s)", _COM_ACTIVATOR_CLSID)
    try:
        comtypes.server.localserver.run([_PlaudToolsActivator])
        logging.info("COM activator: local server exited normally")
    except Exception:
        logging.exception("COM activator: local server failed; signalling event as fallback")
        _signal_activate_event()


__all__ = ["_COM_ACTIVATOR_CLSID", "run_com_activator"]
