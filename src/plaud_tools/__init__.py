from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .auth import PlaudAuth
from .client import PlaudClient, PlaudRecordingQuery
from .errors import PlaudApiError, PlaudSessionExpiredError
from .mcp import build_handlers
from .session import FileSessionStore, PlaudSession, SessionManager, SessionStore

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
