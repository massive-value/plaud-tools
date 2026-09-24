# PyInstaller runtime hook: keep stdlib `platform` off WMI in every frozen bundle.
#
# Custom runtime hooks run before any of the frozen app's own code, so the patch
# is in place before something imports keyring -> jaraco.context -> platform.
# system(), long before plaud_tools.mcp_pt.server.main() runs. The tray and CLI
# get it too: the tray is long-lived and loads keyring. See
# plaud_tools/core/platform_guard.py.
from plaud_tools.core.platform_guard import disable_wmi_queries

disable_wmi_queries()
