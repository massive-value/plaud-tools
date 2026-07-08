from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .core.auth import PlaudAuth
from .core.client import PlaudClient, PlaudRecordingQuery
from .core.errors import PlaudApiError, PlaudSessionExpiredError
from .core.session import FileSessionStore, PlaudSession, SessionManager, SessionStore
from .mcp_pt.mcp import build_handlers

try:
    __version__ = _pkg_version("plaud-tools")
except PackageNotFoundError:
    __version__ = "0.0.0+dev"

__all__ = [
    "FileSessionStore",
    "PlaudAuth",
    "PlaudApiError",
    "PlaudClient",
    "PlaudRecordingQuery",
    "PlaudSession",
    "PlaudSessionExpiredError",
    "SessionManager",
    "SessionStore",
    "__version__",
    "build_handlers",
]
