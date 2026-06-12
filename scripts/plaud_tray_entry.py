import sys

# ---------------------------------------------------------------------------
# --diagnose-enum: frozen-import proof for Wave 2 / C4 (psutil in bundle).
#
# When this flag is present the entry point imports plaud_tools.mcp_lifecycle
# and reports which process enumerator is active (psutil vs PowerShell
# fallback), then exits 0 WITHOUT importing pystray / PIL / tkinter or
# launching any GUI.  This is intentional: CI runners have no display, so
# we must avoid any GUI import under this flag.
#
# Output format (one line to stdout):
#   enumerator=psutil
#   enumerator=powershell_fallback
#
# The bundle-smoke CI step asserts the output contains "enumerator=psutil".
# ---------------------------------------------------------------------------
if "--diagnose-enum" in sys.argv:
    import importlib.util

    _psutil_available = importlib.util.find_spec("psutil") is not None
    if _psutil_available:
        # Confirm the import actually works (not just that the spec exists).
        try:
            import psutil as _psutil  # noqa: F401

            _psutil_available = True
        except Exception:
            _psutil_available = False

    _enumerator = "psutil" if _psutil_available else "powershell_fallback"
    print(f"enumerator={_enumerator}", flush=True)

    # Also import mcp_lifecycle to ensure its full import chain resolves
    # in the frozen build (exercises the hiddenimport entries in the spec).
    try:
        import plaud_tools.mcp_lifecycle as _ml  # noqa: F401
    except Exception as _exc:
        print(f"mcp_lifecycle import failed: {_exc}", flush=True)
        sys.exit(1)

    sys.exit(0)

# ---------------------------------------------------------------------------
# Normal tray startup path.
# ---------------------------------------------------------------------------

# Eager DPAPI shadow self-heal, before importing plaud_tools.tray_app — that
# import pulls in pystray + PIL (~3-5 s in the frozen build), and an AI client
# can respawn its MCP child during the bundle swap inside that window.  Doing
# the shadow write here tightens the race so the next MCP cold-start has a
# Credential-Manager-independent path to the session.  See
# SessionStore.prime_dpapi_shadow.  Skipped on the COM toast-activation path
# because that is a short-lived helper process that exits without launching
# the tray.
if "--com-activate" not in sys.argv:
    try:
        from plaud_tools.session import SessionStore

        SessionStore().prime_dpapi_shadow()
    except Exception:
        pass

from plaud_tools.tray_app import main

main()
