# PyInstaller runtime hook: keep stdlib `platform` off WMI in the frozen MCP server.
#
# Custom runtime hooks run before PyInstaller's own ones, and pyi_rth_setuptools
# imports setuptools -> distutils -> platform.system() at startup, long before
# plaud_tools.mcp_pt.server.main() runs. See plaud_tools/core/platform_guard.py.
from plaud_tools.core.platform_guard import disable_wmi_queries

disable_wmi_queries()
