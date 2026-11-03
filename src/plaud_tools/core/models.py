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
    """One recording as Plaud 4.0 lists it.

    ``id`` is the 4.0 file id: ``of_<hex>`` for recordings made before the
    account migrated, ``f_<hex>`` for newer ones.  Plaud files each recording
    as a file node inside its own per-recording container folder
    (``wrapper_id``), which in turn sits in a user or system folder.
    ``folder_id`` is that user folder, or None when the recording sits in a
    system folder (Recordings, Import), which is what "unfiled" means now.
    """

    id: str
    filename: str
    start_time: int = 0
    duration: int = 0
    folder_id: str | None = None
    node_id: str = ""
    wrapper_id: str = ""
    version_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RecordingNote:
    """An extra summary stored next to a recording (Plaud 4.0 "note").

    ``content`` is the raw markdown, or None when Plaud has not finished it.
    ``note_id`` is the note's file id, what ``edit_summary`` takes to edit it.
    """

    note_id: str
    name: str
    content: str | None = None


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
    # Transcript language Plaud recorded for this recording, when it has one.
    language: str | None = None
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
    # Extra summaries filed beside the recording; filled with the summary.
    notes: list[RecordingNote] = field(default_factory=list)
    # Node bookkeeping for writes: the file node, its per-recording container
    # (empty when the file sits directly in a folder), and the node version
    # Plaud requires on every change.
    node_id: str = ""
    wrapper_id: str = ""
    version_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ContentMatch:
    """One recording returned by Plaud's full-text search, with its best hit.

    Plaud returns a single best-scoring chunk per recording, taken from either
    the transcript or the AI summary; ``source`` says which.  ``start_ms`` is
    where a transcript hit begins, in ms from the start of the recording
    (``None`` for summary hits, which have no timing).
    """

    id: str
    title: str
    start_time: int = 0
    source: str = ""
    snippet: str = ""
    start_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Folder:
    """A user folder in the Plaud 4.0 folder tree (system folders excluded)."""

    id: str
    name: str = ""
    color: str = ""
    icon: str = ""
    parent_id: str = ""
    version_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VocabularyWord:
    """One entry in the account-wide custom vocabulary that steers transcription."""

    id: str
    word: str
    # The misheard form Plaud should correct to ``word``; empty when none.
    heard_as: str = ""
    language: str = ""
    # How many transcript corrections Plaud has made with this entry.
    correction_count: int = 0
    created_at_ms: int = 0


@dataclass(slots=True)
class ShareLink:
    """A recording's public share link and what it exposes."""

    url: str
    key: str
    # ISO UTC time the link stops working; None for a link that never expires.
    expires_at: str | None = None
    audio: bool = False
    transcript: bool = False
    summary: bool = False
    created_at: str = ""


@dataclass(slots=True)
class Attachment:
    """A document, image or text file attached to a recording.

    It sits in the recording's container folder next to the audio.
    ``file_type`` is Plaud's own label for it (the REC and MD kinds are the
    recording and its notes, so never an attachment).
    """

    id: str
    filename: str
    file_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AudioLink:
    """A presigned, short-lived download link for a recording's audio."""

    url: str
    mime_type: str = ""

    @property
    def extension(self) -> str:
        """File extension matching the audio format, e.g. ``.mp3`` or ``.ogg``."""
        return {"audio/ogg": ".ogg", "audio/opus": ".opus", "audio/wav": ".wav"}.get(self.mime_type, ".mp3")


@dataclass(slots=True)
class TaskStatus:
    """One in-progress Plaud job on a recording (transcription, summary, ...)."""

    file_id: str
    task_id: str
    task_type: str
    # Each stage as Plaud reports it, e.g. {"code": "TRANSCRIPT", "state": ...}.
    stages: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
