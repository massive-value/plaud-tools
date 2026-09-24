from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BASE_URLS = {
    "us": "https://api.plaud.ai",
    "eu": "https://api-euc1.plaud.ai",
}


def base_url(region: str) -> str:
    """Return the API base URL for a stored region.

    ``region`` is normally a ``BASE_URLS`` key. A region Plaud redirected us
    to that we have no key for is stored as its bare API host (for example
    ``api-apse1.plaud.ai``), so it maps straight back to ``https://<host>``.
    Anything else falls back to US.
    """
    if region in BASE_URLS:
        return BASE_URLS[region]
    if _is_plaud_host(region):
        return f"https://{region}"
    return BASE_URLS["us"]


def redirect_api_domain(payload: dict[str, Any]) -> str:
    """Pull ``data.domains.api`` out of a Plaud ``status: -302`` payload."""
    data = payload.get("data")
    domains = data.get("domains") if isinstance(data, dict) else None
    api = domains.get("api") if isinstance(domains, dict) else None
    return api if isinstance(api, str) else ""


def region_for_api_domain(domain: str) -> str | None:
    """Map the API domain from a Plaud ``-302`` redirect to a region to store.

    Plaud sends ``data.domains.api`` as a host such as ``api-euc1.plaud.ai``.
    Known hosts map to their ``BASE_URLS`` key; any other ``*.plaud.ai`` host
    is stored as-is (see :func:`base_url`). Returns None for anything that is
    not a Plaud host, so a bad redirect never sends the token elsewhere.
    """
    host = domain.strip().lower()
    host = host.split("://", 1)[-1].split("/", 1)[0]
    for region, url in BASE_URLS.items():
        if url.split("://", 1)[1] == host:
            return region
    return host if _is_plaud_host(host) else None


def _is_plaud_host(host: str) -> bool:
    """True for an ASCII hostname under plaud.ai with no empty labels."""
    if not host.isascii() or not host.endswith(".plaud.ai"):
        return False
    labels = host.split(".")
    return all(label and all(ch.isalnum() or ch == "-" for ch in label) for label in labels)


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class Recording:
    id: str
    filename: str
    start_time: int = 0
    duration: int = 0
    is_trash: bool = False
    is_trans: bool = False
    is_summary: bool = False
    filetag_id_list: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RecordingDetail:
    id: str
    filename: str
    start_time: int = 0
    duration: int = 0
    folder_id: str | None = None
    is_trash: bool = False
    is_trans: bool = False
    is_summary: bool = False
    scene: int | None = None
    transcript: str = ""
    # The individual utterances behind ``transcript``, each a dict with
    # speaker/content/timestamps as Plaud returns them. Kept alongside the
    # formatted string so callers can paginate on utterance boundaries instead
    # of slicing mid-word through the joined text.
    transcript_segments: list[dict[str, Any]] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    # Which transcript blocks Plaud has finished generating for this recording,
    # in TRANSCRIPT_BLOCKS order. Populated only when a transcript was
    # requested. Lets a caller that asked for an unavailable block (e.g.
    # "transaction_polish" on a recording Plaud never polished) report what it
    # *could* have asked for instead of an unexplained empty transcript.
    transcript_blocks_available: list[str] = field(default_factory=list)
    ai_content: str | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FileTag:
    id: str
    name: str = ""
    color: str = ""
    icon: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TaskStatus:
    file_id: str
    task_id: str
    task_type: str
    task_status: int
    is_complete: bool
    sum_type: str = ""
    sum_type_type: str = ""
    post_id: int = 0
    ppc_status: int = 0
    is_chatllm: bool = False
    auto_save: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
