from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BASE_URLS = {
    "us": "https://api.plaud.ai",
    "eu": "https://api-euc1.plaud.ai",
}

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
