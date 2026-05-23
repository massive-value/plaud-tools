import sys

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
