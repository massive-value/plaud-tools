from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .. import __version__
from ..core.auth import PlaudAuth
from ..core.client import (
    AUDIO_URL_TTL_S,
    DEFAULT_TRANSCRIPT_BLOCK,
    PlaudClient,
    PlaudRecordingQuery,
)
from ..core.errors import PlaudApiError, PlaudSessionExpiredError
from ..core.query import (
    BROWSE_PAGE_SIZE,
    collect_filtered_paged,
    detail_summary_dict,
    folder_dict,
    parse_isoish,
    structured_segments,
    summarize_recording,
    transcript_fingerprint,
)
from ..core.session import PlaudSession, SessionManager, SessionStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plaud-tools")
    parser.add_argument("--version", action="version", version=f"plaud-tools {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list")
    _add_limit_or_all(list_cmd)
    list_cmd.add_argument("--since")
    list_cmd.add_argument("--until")
    list_cmd.add_argument("--query")
    list_cmd.add_argument("--folder-id")
    list_cmd.add_argument("--unfiled", action="store_true")

    search_cmd = sub.add_parser(
        "search",
        help="Shorthand for 'list --query QUERY' (identical filtering, positional query arg).",
    )
    search_cmd.add_argument("query")
    _add_limit_or_all(search_cmd)
    search_cmd.add_argument("--since")
    search_cmd.add_argument("--until")
    search_cmd.add_argument("--folder-id")
    search_cmd.add_argument("--unfiled", action="store_true")

    detail_cmd = sub.add_parser("detail")
    detail_cmd.add_argument("recording_id")
    detail_cmd.add_argument("--include-transcript", action="store_true")

    show_cmd = sub.add_parser("show")
    show_cmd.add_argument("recording_id")

    transcript_cmd = sub.add_parser("transcript")
    transcript_cmd.add_argument("recording_id")
    transcript_cmd.add_argument(
        "--polish",
        action="store_true",
        help=(
            "Return Plaud's AI-cleaned transcript (filler words removed, punctuation "
            "repaired) instead of the raw one. Errors if the recording has no polished block."
        ),
    )
    transcript_cmd.add_argument(
        "--segments",
        action="store_true",
        help=(
            "Print JSON with every utterance's index, speaker, text and start/end time "
            "(ms from recording start), plus a fingerprint of the whole block, instead of plain text."
        ),
    )

    summary_cmd = sub.add_parser("summary")
    summary_cmd.add_argument("recording_id")

    audio_cmd = sub.add_parser(
        "audio", help="Get a temporary download URL for a recording's audio, or save the file."
    )
    audio_cmd.add_argument("recording_id")
    audio_cmd.add_argument(
        "-o",
        "--output",
        help=(
            "Download the audio to this path instead of printing the URL. "
            "Pass a directory to use '<recording-id>.mp3' inside it."
        ),
    )

    rename_cmd = sub.add_parser("rename")
    rename_cmd.add_argument("recording_id")
    rename_cmd.add_argument("new_name")

    folders_cmd = sub.add_parser("folders")  # noqa: F841  # side-effect: registers subparser

    folder_cmd = sub.add_parser("folder", help="Create, edit, or delete folders.")
    folder_sub = folder_cmd.add_subparsers(dest="folder_command", required=True)

    folder_create = folder_sub.add_parser("create")
    folder_create.add_argument("name")
    folder_create.add_argument("--color", help="Hex color, e.g. '#4c8eff'")
    folder_create.add_argument("--icon", help="Icon glyph codepoint, e.g. 'e627'")

    folder_edit = folder_sub.add_parser("edit")
    folder_edit.add_argument("folder_id")
    folder_edit.add_argument("--name")
    folder_edit.add_argument("--color")
    folder_edit.add_argument("--icon")

    folder_delete = folder_sub.add_parser("delete")
    folder_delete.add_argument("folder_id")
    folder_delete.add_argument("--yes", action="store_true")

    move_cmd = sub.add_parser("move", aliases=["move-to-folder"])
    move_cmd.add_argument("recording_id")
    move_cmd.add_argument("folder_id")

    rename_speaker_cmd = sub.add_parser("rename-speaker")
    rename_speaker_cmd.add_argument("recording_id")
    rename_speaker_cmd.add_argument("original_label")
    rename_speaker_cmd.add_argument("new_name")

    correct_transcript_cmd = sub.add_parser("correct-transcript")
    correct_transcript_cmd.add_argument("recording_id")
    correct_transcript_cmd.add_argument("find")
    correct_transcript_cmd.add_argument("replace")

    correct_summary_cmd = sub.add_parser("correct-summary")
    correct_summary_cmd.add_argument("recording_id")
    correct_summary_cmd.add_argument("find")
    correct_summary_cmd.add_argument("replace")

    set_summary_cmd = sub.add_parser("set-summary")
    set_summary_cmd.add_argument("recording_id")
    set_summary_group = set_summary_cmd.add_mutually_exclusive_group(required=True)
    set_summary_group.add_argument("--content", help="New summary markdown")
    set_summary_group.add_argument(
        "--content-file", help="Path to a file containing the new summary markdown"
    )

    transcribe_cmd = sub.add_parser("transcribe")
    transcribe_cmd.add_argument("recording_id")
    transcribe_cmd.add_argument("--template")
    transcribe_cmd.add_argument("--language", help="Language code, e.g. 'en' (default: auto-detect).")
    transcribe_cmd.add_argument(
        "--diarization",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable/disable speaker diarization (default: Plaud's default).",
    )
    transcribe_cmd.add_argument("--llm", help="LLM to use for summarization (default: auto).")
    transcribe_cmd.add_argument(
        "--wait",
        choices=["none", "transcript", "summary"],
        default="none",
        help=(
            "Block until the given stage completes before returning "
            "(default: none — accept and return immediately)."
        ),
    )

    status_cmd = sub.add_parser("status")
    status_cmd.add_argument("recording_id", nargs="?")

    trash_cmd = sub.add_parser(
        "trash", help="Move a recording to trash, or list recordings already in trash."
    )
    trash_cmd.add_argument("recording_id", nargs="?", default=None, help="Recording to move to trash.")
    trash_cmd.add_argument(
        "--list",
        dest="list_trash",
        action="store_true",
        help="List recordings currently in trash (required instead of a bare 'trash' with no ID).",
    )

    restore_cmd = sub.add_parser("restore")
    restore_cmd.add_argument("recording_id")

    delete_cmd = sub.add_parser("delete")
    delete_cmd.add_argument("recording_id")
    delete_cmd.add_argument("--yes", action="store_true")

    trash_move_cmd = sub.add_parser("trash-move")
    trash_move_cmd.add_argument("recording_ids", nargs="+")

    trash_restore_cmd = sub.add_parser("trash-restore")
    trash_restore_cmd.add_argument("recording_ids", nargs="+")

    upload_cmd = sub.add_parser("upload")
    upload_cmd.add_argument("file")
    upload_cmd.add_argument("--title")
    upload_cmd.add_argument("--folder-id")
    upload_cmd.add_argument(
        "--detach", action="store_true", help="Return immediately without waiting for transcription"
    )
    upload_cmd.add_argument(
        "--skip-summary", action="store_true", help="Wait for transcript only, not summary"
    )
    upload_cmd.add_argument(
        "--start-time",
        help=(
            "Recording timestamp: an ISO 8601 date/datetime string (e.g. "
            "'2025-01-15' or '2025-01-15T09:30'), or an epoch integer -- a "
            "10-digit value (seconds, e.g. from `date +%%s`) is scaled up "
            "automatically; a 13-digit value is treated as milliseconds."
        ),
    )
    upload_cmd.add_argument("--timezone-offset", type=float, help="UTC offset in hours (e.g. -7.0)")

    merge_cmd = sub.add_parser("merge")
    merge_cmd.add_argument("recording_ids", nargs="+")
    merge_cmd.add_argument("--title", required=True)

    dump_cmd = sub.add_parser("dump", help="Dump raw /file/detail API response for debugging")
    dump_cmd.add_argument("recording_id")

    login_cmd = sub.add_parser("login")
    login_cmd.add_argument("--email", required=True)
    login_cmd.add_argument(
        "--password",
        help=(
            "WARNING: passing a password on the command line exposes it via process listings "
            "(ps, Task Manager) and shell history. "
            "For scripting, prefer the PLAUD_ACCESS_TOKEN environment variable or "
            "'session set --token <token>' instead. "
            "If omitted, you will be prompted securely."
        ),
    )
    login_cmd.add_argument("--region", choices=["us", "eu"], default="us")

    # 'refresh' is 'login' with email/region defaulted from the stored session,
    # for re-authing an expired/expiring token without retyping them.  Plaud has
    # no refresh-token grant, so this is still a full credential re-auth.
    refresh_cmd = sub.add_parser(
        "refresh",
        help="Re-authenticate the stored session (reuses saved email/region; prompts for password).",
    )
    refresh_cmd.add_argument("--email", help="Override the stored email.")
    refresh_cmd.add_argument("--password", help="If omitted, you will be prompted securely.")
    refresh_cmd.add_argument("--region", choices=["us", "eu"], help="Override the stored region.")

    session_cmd = sub.add_parser("session")
    session_sub = session_cmd.add_subparsers(dest="session_command", required=True)

    session_show = session_sub.add_parser("show")
    session_show.add_argument("--show-token", action="store_true")

    session_set = session_sub.add_parser("set")
    session_set.add_argument("--token", required=True)
    session_set.add_argument("--region", choices=["us", "eu"], default="us")
    session_set.add_argument("--email")

    session_sub.add_parser("clear")

    sub.add_parser("update", help="Upgrade plaud-tools via pip (pip users only).")

    sub.add_parser(
        "doctor",
        help="Print a self-diagnosis JSON document for support and debugging.",
        description=(
            "Collects the local install state — version, executable paths, session status, "
            "and AI client wiring — and prints it as JSON. "
            "The session token is never included; only masked metadata is surfaced."
        ),
    )

    ping_cmd = sub.add_parser("ping")  # noqa: F841  # side-effect: registers subparser
    return parser


# `list`/`search` return this many recordings unless --limit or --all says otherwise.
DEFAULT_LIST_LIMIT = 20


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {raw!r}")
    return value


def _add_limit_or_all(cmd: argparse.ArgumentParser) -> None:
    """Add the mutually exclusive --limit N / --all pair shared by list and search."""
    group = cmd.add_mutually_exclusive_group()
    group.add_argument("--limit", type=_positive_int, default=DEFAULT_LIST_LIMIT)
    group.add_argument(
        "--all",
        action="store_true",
        help="Page through the whole library (honoring filters) until it is exhausted.",
    )


def _mask_token(token: str) -> str:
    if len(token) <= 12:
        return token
    return f"{token[:6]}...{token[-6:]}"


def _build_runtime_client(store: SessionStore) -> PlaudClient:
    return PlaudClient(SessionManager(store))


# ---------------------------------------------------------------------------
# Per-command handler functions
# ---------------------------------------------------------------------------
# Handlers that do NOT need a PlaudClient (pre-client dispatch).


def _handle_login(
    args: argparse.Namespace,
    store: SessionStore,
    auth: PlaudAuth | None,
) -> str:
    password = args.password or getpass.getpass("Plaud password: ")
    login_auth = auth or PlaudAuth(store)
    session = login_auth.login(args.email, password, args.region)
    return json.dumps(
        {
            "ok": True,
            "email": session.email,
            "region": session.region,
            "status": "stored",
        },
        indent=2,
    )


def _handle_refresh(
    args: argparse.Namespace,
    store: SessionStore,
    auth: PlaudAuth | None,
) -> str:
    stored = store.load()
    email = args.email or (stored.email if stored else None)
    if not email:
        raise ValueError("No stored email to refresh; run 'plaud-tools login --email ...' instead.")
    region = args.region or (stored.region if stored else "us")
    password = args.password or getpass.getpass(f"Plaud password for {email}: ")
    session = (auth or PlaudAuth(store)).login(email, password, region)
    return json.dumps(
        {"ok": True, "email": session.email, "region": session.region, "status": "refreshed"},
        indent=2,
    )


def _session_storage_path(store: SessionStore, source: str) -> str | None:
    """Return the on-disk path *source* actually landed in, or None for keyring.

    ``keyring`` has no filesystem path of its own to report.
    """
    if source == "file":
        return str(store.file_store.path)
    if source == "dpapi_file" and store.dpapi_path is not None:
        return str(store.dpapi_path)
    return None


def _handle_session(args: argparse.Namespace, store: SessionStore) -> str:
    if args.session_command == "set":
        store.save(PlaudSession(access_token=args.token, region=args.region, email=args.email))
        # store.save() tries the keyring first, then a DPAPI shadow file, and
        # only falls back to the plaintext file store if both are
        # unavailable -- report where it actually landed (via
        # load_with_source(), the same lookup `session show` uses) instead of
        # always claiming the file-store path.
        _, source = store.load_with_source()
        result: dict[str, Any] = {
            "ok": True,
            "source": source,
            "region": args.region,
            "email": args.email,
        }
        path = _session_storage_path(store, source)
        if path is not None:
            result["path"] = path
        return json.dumps(result, indent=2)
    if args.session_command == "clear":
        store.clear()
        return json.dumps({"ok": True}, indent=2)

    session2, source = store.load_with_source()
    if session2 is None:
        return json.dumps({"session": None, "path": str(store.file_store.path), "source": source}, indent=2)
    manager = SessionManager(store)
    try:
        manager.require()
        status = "valid"
    except PlaudSessionExpiredError as exc:
        status = exc.code
    days = manager.days_until_expiry()
    return json.dumps(
        {
            "path": str(store.file_store.path),
            "source": source,
            "region": session2.region,
            "email": session2.email,
            "status": status,
            "days_until_expiry": days,
            "token": session2.access_token if args.show_token else _mask_token(session2.access_token),
        },
        indent=2,
    )


def _handle_update(args: argparse.Namespace) -> str:  # noqa: ARG001  # never returns — calls sys.exit
    # #158: in a PyInstaller-frozen bundle, sys.executable is the frozen exe
    # itself — there is no real Python interpreter behind it, so re-invoking
    # `[sys.executable, "-m", "pip", ...]` just re-launches this same exe with
    # "-m pip install --upgrade plaud-tools" as bogus CLI arguments instead of
    # upgrading anything. This command is pip-install-only; the frozen bundle
    # has its own tray-driven updater.
    if getattr(sys, "frozen", False):
        print(
            "'update' is for pip installs only. This is the bundled PlaudTools app — "
            "use the tray's built-in updater, or re-run install.ps1 to get the latest version.",
            file=sys.stderr,
        )
        sys.exit(1)

    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "plaud-tools"],
        stdout=None,
        stderr=None,
    )
    if result.returncode == 0:
        print(
            "\nNote: pipx, uv, and conda users should use their own package manager's"
            " upgrade command, not this one."
        )
    sys.exit(result.returncode)


def _handle_doctor(args: argparse.Namespace, store: SessionStore) -> str:  # noqa: ARG001
    from .doctor import run_doctor_json

    return run_doctor_json(store)


# Handlers that DO need a PlaudClient (post-client dispatch).


def _list_recordings_filtered(
    client: PlaudClient,
    *,
    limit: int | None,
    since: str | None,
    until: str | None,
    query: str | None,
    folder_id: str | None,
    unfiled: bool,
) -> list[Any]:
    """Shared filtering/paging logic behind both ``list`` and ``search``.

    ``search`` is a positional-argument shorthand for ``list --query`` — it
    has no ranking of its own, so it delegates here instead of re-implementing
    the same paged-filter call (previously duplicated verbatim).

    ``limit=None`` (``--all``) pages upstream until it runs dry, filtered or
    not, so the result is the complete matching set rather than one page.
    """
    has_filters = bool(since or until or query or folder_id or unfiled) or limit is None
    since_ms = parse_isoish(since, "--since") if since else None
    until_ms = parse_isoish(until, "--until", end_of_day=True) if until else None
    if has_filters:
        recordings, _ = collect_filtered_paged(
            lambda skip, page_size: client.list_recordings(
                PlaudRecordingQuery(
                    skip=skip,
                    limit=page_size,
                    is_trash=0,
                    sort_by="start_time",
                    is_desc=True,
                )
            ),
            BROWSE_PAGE_SIZE,
            since_ms=since_ms,
            until_ms=until_ms,
            query=query,
            folder_id=folder_id,
            unfiled=unfiled,
            after=0,
            limit=limit,
        )
        return recordings
    return client.list_recordings(
        PlaudRecordingQuery(limit=limit, is_trash=0, sort_by="start_time", is_desc=True)
    )


def _handle_list_or_search(args: argparse.Namespace, client: PlaudClient) -> str:
    """Shared handler for `list` and `search`.

    `search` is defined as `list --query QUERY` with the query as a
    positional argument instead of a flag (see `_list_recordings_filtered`'s
    docstring) — both subparsers land the query in ``args.query``, so
    dispatch never needs to know which subcommand it was called for.
    """
    recordings = _list_recordings_filtered(
        client,
        limit=None if args.all else args.limit,
        since=args.since,
        until=args.until,
        query=args.query,
        folder_id=args.folder_id,
        unfiled=args.unfiled,
    )
    return json.dumps([summarize_recording(r) for r in recordings], indent=2)


def _handle_detail(args: argparse.Namespace, client: PlaudClient) -> str:
    # Always fetch the summary (like `summary <id>` does) so "summary" reflects
    # whether Plaud actually has one, instead of the null it would get here
    # from a bare GET that never asked for it. The "transcript" key is only
    # included when requested — an always-present `"transcript": null` implied
    # absence when it just wasn't fetched.
    detail = client.get_recording(
        args.recording_id,
        include_transcript=args.include_transcript,
        include_summary=True,
    )
    payload: dict[str, Any] = {
        "id": detail.id,
        "filename": detail.filename,
        "is_trans": detail.is_trans,
        "is_summary": detail.is_summary,
        "summary": detail.ai_content,
    }
    if args.include_transcript:
        # null, not "", when there is no transcript to return — an empty
        # string would read as a successfully transcribed silent recording.
        has_block = DEFAULT_TRANSCRIPT_BLOCK in detail.transcript_blocks_available
        payload["transcript"] = detail.transcript if has_block else None
    return json.dumps(payload, indent=2)


def _handle_show(args: argparse.Namespace, client: PlaudClient) -> str:
    detail = client.get_recording(args.recording_id, include_transcript=True)
    output = detail_summary_dict(detail)
    output["speakers"] = detail.speakers
    return json.dumps(output, indent=2)


def _handle_summary(args: argparse.Namespace, client: PlaudClient) -> str:
    detail = client.get_recording(args.recording_id, include_summary=True)
    if not detail.ai_content:
        return json.dumps(
            {"recording_id": args.recording_id, "summary": None, "note": "No summary available."},
            indent=2,
        )
    return json.dumps({"recording_id": args.recording_id, "summary": detail.ai_content}, indent=2)


def _handle_rename(args: argparse.Namespace, client: PlaudClient) -> str:
    client.rename_recording(args.recording_id, args.new_name)
    return json.dumps(
        {"ok": True, "recording_id": args.recording_id, "new_name": args.new_name},
        indent=2,
    )


def _handle_folders(args: argparse.Namespace, client: PlaudClient) -> str:  # noqa: ARG001
    tags = client.list_file_tags()
    return json.dumps([folder_dict(tag) for tag in tags], indent=2)


def _handle_folder(args: argparse.Namespace, client: PlaudClient) -> str:
    if args.folder_command == "create":
        tag = client.create_folder(args.name, color=args.color, icon=args.icon)
        return json.dumps({"ok": True, "action": "create", "folder": folder_dict(tag)}, indent=2)
    if args.folder_command == "edit":
        if args.name is None and args.color is None and args.icon is None:
            raise ValueError("folder edit requires at least one of --name, --color, --icon")
        tag = client.update_folder(args.folder_id, name=args.name, color=args.color, icon=args.icon)
        return json.dumps({"ok": True, "action": "edit", "folder": folder_dict(tag)}, indent=2)
    if args.folder_command == "delete":
        if not args.yes:
            raise ValueError(
                f"Deleting folder {args.folder_id!r} cannot be undone (recordings inside are kept "
                f"but become unfiled). Re-run with --yes to confirm."
            )
        client.delete_folder(args.folder_id)
        return json.dumps({"ok": True, "action": "delete", "folder_id": args.folder_id}, indent=2)
    raise AssertionError(f"unhandled folder command: {args.folder_command}")


def _handle_move(args: argparse.Namespace, client: PlaudClient) -> str:
    folder_id = None if args.folder_id == "-" else args.folder_id
    client.set_recording_folder(args.recording_id, folder_id)
    return json.dumps(
        {"ok": True, "recording_id": args.recording_id, "folder_id": folder_id},
        indent=2,
    )


def _handle_trash(args: argparse.Namespace, client: PlaudClient) -> str:
    # A bare `trash` (no ID, no --list) used to silently list trash — a
    # dropped/mistyped recording_id argument turned an intended mutation into
    # a no-op listing with no error. Listing now requires --list explicitly;
    # trashing requires a recording_id explicitly. Combining both is rejected.
    if args.list_trash:
        if args.recording_id is not None:
            raise ValueError("trash: --list cannot be combined with a recording ID")
        recordings = client.list_trash()
        return json.dumps([summarize_recording(r) for r in recordings], indent=2)
    if args.recording_id is None:
        raise ValueError("trash requires a recording ID, or use 'trash --list' to list trashed recordings")
    client.move_to_trash([args.recording_id])
    return json.dumps({"ok": True, "recording_id": args.recording_id, "mutation": "trash"}, indent=2)


def _handle_restore(args: argparse.Namespace, client: PlaudClient) -> str:
    client.restore_from_trash([args.recording_id])
    return json.dumps({"ok": True, "recording_id": args.recording_id, "mutation": "restore"}, indent=2)


def _handle_delete(args: argparse.Namespace, client: PlaudClient) -> str:
    if not args.yes:
        raise ValueError(
            f"Permanent deletion of {args.recording_id!r} cannot be undone. Re-run with --yes to confirm."
        )
    client.delete_recordings([args.recording_id])
    return json.dumps({"ok": True, "recording_id": args.recording_id, "mutation": "delete"}, indent=2)


def _handle_trash_move(args: argparse.Namespace, client: PlaudClient) -> str:
    client.move_to_trash(args.recording_ids)
    return json.dumps(
        {"ok": True, "count": len(args.recording_ids), "recording_ids": args.recording_ids},
        indent=2,
    )


def _handle_trash_restore(args: argparse.Namespace, client: PlaudClient) -> str:
    client.restore_from_trash(args.recording_ids)
    return json.dumps(
        {"ok": True, "count": len(args.recording_ids), "recording_ids": args.recording_ids},
        indent=2,
    )


def _handle_rename_speaker(args: argparse.Namespace, client: PlaudClient) -> str:
    rename_result = client.rename_speaker(args.recording_id, args.original_label, args.new_name)
    return json.dumps(
        {
            "ok": True,
            "recording_id": args.recording_id,
            "original_label": args.original_label,
            "new_name": args.new_name,
            "segments_updated": rename_result["segments_updated"],
        },
        indent=2,
    )


def _handle_correct_transcript(args: argparse.Namespace, client: PlaudClient) -> str:
    correct_result = client.correct_transcript(args.recording_id, args.find, args.replace)
    return json.dumps(
        {
            "ok": True,
            "recording_id": args.recording_id,
            "find": args.find,
            "replace": args.replace,
            "replacements": correct_result["replacements"],
            "segments_changed": correct_result["segments_changed"],
        },
        indent=2,
    )


def _handle_correct_summary(args: argparse.Namespace, client: PlaudClient) -> str:
    result = client.correct_summary(args.recording_id, args.find, args.replace)
    return json.dumps(
        {
            "ok": True,
            "recording_id": args.recording_id,
            "find": args.find,
            "replace": args.replace,
            "replacements": result["replacements"],
        },
        indent=2,
    )


def _handle_set_summary(args: argparse.Namespace, client: PlaudClient) -> str:
    if args.content_file:
        path = Path(args.content_file)
        if not path.exists():
            raise ValueError(f"file not found: {args.content_file}")
        content = path.read_text(encoding="utf-8")
    else:
        content = args.content
    client.set_summary(args.recording_id, content)
    return json.dumps(
        {"ok": True, "recording_id": args.recording_id, "mutation": "set-summary"},
        indent=2,
    )


# A millisecond epoch below this threshold would land before 1973 -- not a
# plausible recording date -- so an integer --start-time smaller than this is
# assumed to be a *seconds* epoch (e.g. from `date +%s`, 10 digits) and scaled
# up instead. Silently treating it as milliseconds used to produce a 1970
# timestamp for both 10-digit seconds epochs and ISO basic dates like
# "20250115" (parsed as an int, not a date).
_MIN_PLAUSIBLE_START_TIME_MS = 100_000_000_000


def _parse_start_time(raw: str) -> int:
    """Parse --start-time as an ISO 8601 date/datetime first, then as an epoch int."""
    try:
        return parse_isoish(raw, "--start-time")
    except ValueError:
        pass
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid --start-time value: {raw}") from exc
    if value < _MIN_PLAUSIBLE_START_TIME_MS:
        return value * 1000
    return value


def _handle_upload(args: argparse.Namespace, client: PlaudClient) -> str:
    from ..core.transcode import upload_with_transcode

    path = Path(args.file)
    title = args.title or path.stem
    start_ms: int | None = None
    if args.start_time is not None:
        start_ms = _parse_start_time(str(args.start_time))

    # ValueError (missing file / unsupported format) and RuntimeError (ffmpeg
    # failure) propagate to main()'s except clause, which already prints and
    # exits non-zero for both.
    outcome = upload_with_transcode(
        client,
        path,
        title,
        start_time=start_ms,
        timezone_offset=args.timezone_offset,
        folder_id=args.folder_id,
    )
    recording = outcome.recording
    upload_result: dict[str, Any] = {
        "ok": True,
        "recording_id": recording.id,
        "filename": recording.filename,
        "transcoded": outcome.transcoded,
    }
    if outcome.folder_error is not None:
        # #149: upload succeeded but the post-upload folder move failed — the
        # recording id must still reach the caller so it isn't re-uploaded.
        upload_result["folder_error"] = outcome.folder_error
    if not args.detach:
        # The recording already exists at this point; everything below is
        # just waiting on it. Print the id now, before the wait, so a
        # timeout/5xx/Ctrl+C during transcribe-and-wait still leaves the
        # user with the id on stderr instead of nothing to go on but a
        # stack trace and a strong temptation to re-upload (duplicating it).
        print(
            f"uploaded recording_id={recording.id!r} - waiting for transcription "
            "(re-running upload on failure would duplicate this recording)",
            file=sys.stderr,
        )
        client.transcribe_and_summarize(recording.id)
        client.wait_for_transcription(recording.id)
        if not args.skip_summary:
            client.wait_for_summary(recording.id)
        upload_result["transcribed"] = True
    else:
        upload_result["detached"] = True
    return json.dumps(upload_result, indent=2)


def _handle_merge(args: argparse.Namespace, client: PlaudClient) -> str:
    # merge_recordings() submits the combine job and polls it to completion
    # in one call, so its new recording id isn't known until it returns — if
    # the poll times out or fails partway, there is nothing to print after
    # the fact. Print the inputs before the (potentially long) call so a
    # failure still leaves the user with what was submitted, instead of
    # nothing to go on but a stack trace.
    print(
        f"merging {args.recording_ids!r} into title={args.title!r} "
        "(re-running on failure may duplicate the merge job)",
        file=sys.stderr,
    )
    detail = client.merge_recordings(args.recording_ids, args.title)
    return json.dumps(
        {
            "ok": True,
            "recording_id": detail.id,
            "filename": detail.filename,
            "source_ids": args.recording_ids,
        },
        indent=2,
    )


def _handle_transcribe(args: argparse.Namespace, client: PlaudClient) -> str:
    client.transcribe_and_summarize(
        args.recording_id,
        template_type=args.template,
        language=args.language,
        diarization=args.diarization,
        llm=args.llm,
    )
    result: dict[str, Any] = {
        "accepted": True,
        "recording_id": args.recording_id,
        "template_type": args.template or "AUTO-SELECT",
    }
    if args.wait == "none":
        return json.dumps(result, indent=2)
    client.wait_for_transcription(args.recording_id)
    if args.wait == "summary":
        client.wait_for_summary(args.recording_id)
    detail = client.get_recording(args.recording_id)
    result["is_trans"] = detail.is_trans
    result["is_summary"] = detail.is_summary
    return json.dumps(result, indent=2)


def _handle_status(args: argparse.Namespace, client: PlaudClient) -> str:
    tasks = client.get_task_status(args.recording_id)
    return json.dumps(
        [
            {
                "file_id": task.file_id,
                "task_id": task.task_id,
                "task_type": task.task_type,
                "task_status": task.task_status,
                "is_complete": task.is_complete,
                "sum_type": task.sum_type,
                "sum_type_type": task.sum_type_type,
            }
            for task in tasks
        ],
        indent=2,
    )


def _handle_dump(args: argparse.Namespace, client: PlaudClient) -> str:
    raw = client.dump_raw_detail(args.recording_id)
    return json.dumps(raw, indent=2)


def _handle_transcript(args: argparse.Namespace, client: PlaudClient) -> str:
    block = "transaction_polish" if args.polish else DEFAULT_TRANSCRIPT_BLOCK
    detail = client.get_recording(args.recording_id, include_transcript=True, transcript_block=block)
    if block not in detail.transcript_blocks_available:
        # Fail (exit 1) instead of printing an empty string: a script must be
        # able to tell "no transcript" from a successfully empty one, which
        # exits 0 with empty output.
        if not detail.transcript_blocks_available:
            raise ValueError(
                f"No transcript for {args.recording_id} yet. Run 'plaud-tools transcribe' first."
            )
        available = ", ".join(detail.transcript_blocks_available)
        raise ValueError(
            f"No AI-polished transcript for {args.recording_id} "
            f"(available blocks: {available}). Retry without --polish."
        )
    if args.segments:
        segments = detail.transcript_segments
        return json.dumps(
            {
                "recording_id": args.recording_id,
                "transcript_block": block,
                "utterance_count": len(segments),
                "fingerprint": transcript_fingerprint(segments),
                "segments": structured_segments(segments),
            },
            indent=2,
            ensure_ascii=False,
        )
    return detail.transcript


def _handle_audio(args: argparse.Namespace, client: PlaudClient) -> str:
    if args.output:
        destination = Path(args.output)
        # `Path.is_dir()` is False for a directory that doesn't exist yet, so
        # `-o ./downloads/` (not yet created) would otherwise write a file
        # literally named "downloads". A trailing separator in the raw
        # string is the user's way of saying "this is a directory" even
        # before it exists, so check for that too — `download_audio()`
        # creates the parent directory either way.
        looks_like_dir = args.output.endswith(("/", "\\"))
        if destination.is_dir() or looks_like_dir:
            destination = destination / f"{args.recording_id}.mp3"
        saved = client.download_audio(args.recording_id, destination)
        return json.dumps(
            {
                "ok": True,
                "recording_id": args.recording_id,
                "path": str(saved),
                "bytes": saved.stat().st_size,
            },
            indent=2,
        )
    url = client.get_audio_url(args.recording_id)
    if url is None:
        raise ValueError(
            f"recording {args.recording_id} has no downloadable audio "
            "(it may not have finished syncing from the device)"
        )
    return json.dumps(
        {
            "recording_id": args.recording_id,
            "audio_url": url,
            "expires_in_s": AUDIO_URL_TTL_S,
        },
        indent=2,
    )


def _handle_ping(args: argparse.Namespace, client: PlaudClient) -> str:  # noqa: ARG001
    client.get_user_info()
    return json.dumps({"ok": True}, indent=2)


# ---------------------------------------------------------------------------
# Dispatch registries
# ---------------------------------------------------------------------------

# Commands that DO require a PlaudClient.
# Signature: (args, client) -> str
_CLIENT_HANDLERS: dict[str, Callable[[argparse.Namespace, PlaudClient], str]] = {
    "list": _handle_list_or_search,
    "search": _handle_list_or_search,
    "detail": _handle_detail,
    "show": _handle_show,
    "summary": _handle_summary,
    "rename": _handle_rename,
    "folders": _handle_folders,
    "folder": _handle_folder,
    "move-to-folder": _handle_move,
    "move": _handle_move,
    "trash": _handle_trash,
    "restore": _handle_restore,
    "delete": _handle_delete,
    "trash-move": _handle_trash_move,
    "trash-restore": _handle_trash_restore,
    "rename-speaker": _handle_rename_speaker,
    "correct-transcript": _handle_correct_transcript,
    "correct-summary": _handle_correct_summary,
    "set-summary": _handle_set_summary,
    "upload": _handle_upload,
    "merge": _handle_merge,
    "transcribe": _handle_transcribe,
    "status": _handle_status,
    "dump": _handle_dump,
    "transcript": _handle_transcript,
    "audio": _handle_audio,
    "ping": _handle_ping,
}


def run_cli(
    argv: Sequence[str],
    client: PlaudClient | None = None,
    session_store: SessionStore | None = None,
    auth: PlaudAuth | None = None,
) -> str:
    args = build_parser().parse_args(list(argv))
    store = session_store or SessionStore()

    # --- Pre-client commands (no PlaudClient needed) ---
    if args.command == "login":
        return _handle_login(args, store, auth)
    if args.command == "refresh":
        return _handle_refresh(args, store, auth)
    if args.command == "session":
        return _handle_session(args, store)
    if args.command == "update":
        return _handle_update(args)  # never returns — calls sys.exit
    if args.command == "doctor":
        return _handle_doctor(args, store)

    # Build the client for all remaining commands.
    client = client or _build_runtime_client(store)

    # --- Client-requiring commands ---
    handler = _CLIENT_HANDLERS.get(args.command)
    if handler is not None:
        return handler(args, client)

    raise AssertionError(f"unhandled CLI command: {args.command}")


def _reconfigure_stdout_utf8() -> None:
    """Force stdout to UTF-8 so non-ASCII output survives redirection.

    Most commands' JSON output is ASCII-safe (json.dumps escapes non-ASCII
    by default), but `transcript` prints raw transcript text (and
    `transcript --segments` keeps it unescaped in its JSON).  On Windows a
    piped/redirected stdout often falls back to the legacy cp1252 console
    code page, which raises UnicodeEncodeError on non-Latin-1 characters
    (issue #155).  reconfigure() is a no-op when stdout is already UTF-8 and
    is safely skipped when unsupported (e.g. a test harness's captured stream).
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8")
    except (OSError, ValueError):
        pass


# §6.2: a session-expired error must name the remedy so a stuck user (or an
# AI client relaying the message) knows what to do next, instead of a bare
# "session expired" with no next step. "plaud-tools" is the canonical CLI name
# — it is the actual [project.scripts] entry point (alongside the "pt" short
# alias); this file previously drifted to the nonexistent "plaud" in one spot
# (the old `refresh` error message, fixed alongside this).
_SESSION_EXPIRED_REMEDY = "Run 'plaud-tools refresh' or open the PlaudTools tray to sign in again."


def _with_session_expired_remedy(message: str) -> str:
    return f"{message} {_SESSION_EXPIRED_REMEDY}"


# ---------------------------------------------------------------------------
# Exit codes.  Every failure used to collapse to 1, so a script could not tell
# "go sign in again" from "the network flaked" without matching on stderr text.
# PlaudApiError.classify() already computes the distinction — this just stops
# discarding it at the process boundary.
#
#   0  success
#   1  invalid arguments, not-found, or an unclassified API/local error
#   2  authentication failed — session expired or 401; run 'plaud-tools refresh'
#   3  transient network or server error (429/5xx, connection failure) — retry
#   4  timed out waiting on a job that is still running server-side — poll again
#
# argparse's own failures (bad flags, missing args) exit 2 inside parse_args,
# which is argparse's convention and NOT our "auth failed" 2.  Left alone
# deliberately: overriding it means subclassing ArgumentParser to reroute
# every parser error, which is a lot of machinery to renumber one exit code
# that only ever appears alongside a usage message on stderr.
# ponytail: revisit only if a script is observed keying on exit 2 from a
# malformed invocation — the usage text on stderr already disambiguates.
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 2
EXIT_NETWORK = 3
EXIT_TIMEOUT = 4
# Conventional shell exit code for a process killed by SIGINT (128 + 2),
# not part of the 0-4 taxonomy above since it's not something Plaud raised.
EXIT_SIGINT = 130

_EXIT_CODE_BY_ERROR_CODE = {
    "session_expired": EXIT_AUTH,
    "transient": EXIT_NETWORK,
}


def main(argv: Sequence[str] | None = None) -> int:
    _reconfigure_stdout_utf8()
    args = list(argv) if argv is not None else sys.argv[1:]
    try:
        output = run_cli(args)
    except KeyboardInterrupt:
        # Ctrl+C mid-command (e.g. during a long upload/merge wait) used to
        # print a raw traceback. Exit with the conventional signal code
        # instead — a script checking the exit code sees the same thing a
        # shell pipeline would for any other Ctrl+C'd process.
        return EXIT_SIGINT
    except BrokenPipeError:
        # A downstream reader closed early (e.g. `plaud-tools list | head`).
        # This isn't a failure of the command itself, so exit quietly rather
        # than dumping a traceback. Redirect stdout to devnull first so the
        # interpreter's exit-time flush of the now-unreadable pipe doesn't
        # print its own "Exception ignored" noise; guarded because a test
        # harness's captured stdout may not back a real file descriptor.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return EXIT_OK
    except PlaudSessionExpiredError as exc:
        print(_with_session_expired_remedy(str(exc)), file=sys.stderr)
        return EXIT_AUTH
    except PlaudApiError as exc:
        error_code, _retryable = exc.classify()
        message = str(exc)
        if error_code == "session_expired":
            message = _with_session_expired_remedy(message)
        print(message, file=sys.stderr)
        # A soft-deadline timeout (#151) classifies as a plain "api_error"
        # because it carries no HTTP status, but it is materially different
        # from a hard failure: the transcription/merge/upload is still running
        # on Plaud's side, so the caller should poll rather than treat the
        # command as failed.  Check it before the classify() mapping.
        if exc.is_soft_deadline_timeout():
            return EXIT_TIMEOUT
        return _EXIT_CODE_BY_ERROR_CODE.get(error_code, EXIT_ERROR)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        # A filesystem error, e.g. `set-summary --content-file <a directory>`
        # or `audio -o` targeting an unwritable path (BrokenPipeError, a
        # subclass of OSError, is already handled above). Previously this hit
        # no handler and printed a raw traceback.
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    print(output)
    return EXIT_OK
