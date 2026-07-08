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
    speakers: list[str] = field(default_factory=list)
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
