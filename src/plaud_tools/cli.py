from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .auth import PlaudAuth
from .client import PlaudClient, PlaudRecordingQuery
from .errors import PlaudApiError, PlaudSessionExpiredError
from .query import BROWSE_PAGE_SIZE, collect_filtered_paged, parse_isoish, summarize_recording
from .session import PlaudSession, SessionManager, SessionStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plaud-tools")
    parser.add_argument("--version", action="version", version=f"plaud-tools {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--limit", type=int, default=20)
    list_cmd.add_argument("--since")
    list_cmd.add_argument("--until")
    list_cmd.add_argument("--query")
    list_cmd.add_argument("--folder-id")
    list_cmd.add_argument("--unfiled", action="store_true")

    search_cmd = sub.add_parser("search")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--limit", type=int, default=20)
    search_cmd.add_argument("--since")
    search_cmd.add_argument("--until")
    search_cmd.add_argument("--folder-id")

    detail_cmd = sub.add_parser("detail")
    detail_cmd.add_argument("recording_id")
    detail_cmd.add_argument("--include-transcript", action="store_true")

    show_cmd = sub.add_parser("show")
    show_cmd.add_argument("recording_id")

    transcript_cmd = sub.add_parser("transcript")
    transcript_cmd.add_argument("recording_id")

    summary_cmd = sub.add_parser("summary")
    summary_cmd.add_argument("recording_id")

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

    move_to_folder_cmd = sub.add_parser("move-to-folder")
    move_to_folder_cmd.add_argument("recording_id")
    move_to_folder_cmd.add_argument("folder_id")

    move_cmd = sub.add_parser("move")
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

    status_cmd = sub.add_parser("status")
    status_cmd.add_argument("recording_id", nargs="?")

    trash_cmd = sub.add_parser("trash")
    trash_cmd.add_argument("recording_id", nargs="?", default=None)

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
        "--start-time", help="Recording timestamp as millisecond epoch integer or ISO 8601 string"
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
        raise ValueError("No stored email to refresh; run 'plaud login --email ...' instead.")
    region = args.region or (stored.region if stored else "us")
    password = args.password or getpass.getpass(f"Plaud password for {email}: ")
    session = (auth or PlaudAuth(store)).login(email, password, region)
    return json.dumps(
        {"ok": True, "email": session.email, "region": session.region, "status": "refreshed"},
        indent=2,
    )


def _handle_session(args: argparse.Namespace, store: SessionStore) -> str:
    if args.session_command == "set":
        store.save(PlaudSession(access_token=args.token, region=args.region, email=args.email))
        return json.dumps(
            {
                "ok": True,
                "path": str(store.file_store.path),
                "region": args.region,
                "email": args.email,
            },
            indent=2,
        )
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


def _handle_list(args: argparse.Namespace, client: PlaudClient) -> str:
    has_filters = bool(args.since or args.until or args.query or args.folder_id or args.unfiled)
    since_ms = parse_isoish(args.since, "--since") if args.since else None
    until_ms = parse_isoish(args.until, "--until", end_of_day=True) if args.until else None
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
            query=args.query,
            folder_id=args.folder_id,
            unfiled=args.unfiled,
            after=0,
            limit=args.limit,
        )
    else:
        recordings = client.list_recordings(
            PlaudRecordingQuery(limit=args.limit, is_trash=0, sort_by="start_time", is_desc=True)
        )
    return json.dumps([summarize_recording(r) for r in recordings], indent=2)


def _handle_search(args: argparse.Namespace, client: PlaudClient) -> str:
    since_ms = parse_isoish(args.since, "--since") if args.since else None
    until_ms = parse_isoish(args.until, "--until", end_of_day=True) if args.until else None
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
        query=args.query,
        folder_id=args.folder_id,
        unfiled=False,
        after=0,
        limit=args.limit,
    )
    return json.dumps([summarize_recording(r) for r in recordings], indent=2)


def _handle_detail(args: argparse.Namespace, client: PlaudClient) -> str:
    detail = client.get_recording(args.recording_id, include_transcript=args.include_transcript)
    return json.dumps(
        {
            "id": detail.id,
            "filename": detail.filename,
            "is_trans": detail.is_trans,
            "is_summary": detail.is_summary,
            "transcript": detail.transcript if args.include_transcript else None,
            "summary": detail.ai_content,
        },
        indent=2,
    )


def _handle_show(args: argparse.Namespace, client: PlaudClient) -> str:
    detail = client.get_recording(args.recording_id, include_transcript=True)
    extra = detail.extra_data or {}
    headline = (extra.get("aiContentHeader") or {}).get("headline")
    return json.dumps(
        {
            "id": detail.id,
            "title": detail.filename,
            "date": datetime.fromtimestamp(detail.start_time / 1000).isoformat()[:16],
            "duration_minutes": round(detail.duration / 60000),
            "folder_id": detail.folder_id,
            "is_trans": detail.is_trans,
            "is_summary": detail.is_summary,
            "speakers": detail.speakers,
            "headline": headline,
        },
        indent=2,
    )


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
    return json.dumps(
        [{"id": tag.id, "name": tag.name, "color": tag.color, "icon": tag.icon} for tag in tags],
        indent=2,
    )


def _handle_folder(args: argparse.Namespace, client: PlaudClient) -> str:
    def _shape(tag: Any) -> dict[str, Any]:
        return {"id": tag.id, "name": tag.name, "color": tag.color, "icon": tag.icon}

    if args.folder_command == "create":
        tag = client.create_folder(args.name, color=args.color, icon=args.icon)
        return json.dumps({"ok": True, "action": "create", "folder": _shape(tag)}, indent=2)
    if args.folder_command == "edit":
        if args.name is None and args.color is None and args.icon is None:
            raise ValueError("folder edit requires at least one of --name, --color, --icon")
        tag = client.update_folder(args.folder_id, name=args.name, color=args.color, icon=args.icon)
        return json.dumps({"ok": True, "action": "edit", "folder": _shape(tag)}, indent=2)
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
    if args.recording_id is not None:
        client.move_to_trash([args.recording_id])
        return json.dumps({"ok": True, "recording_id": args.recording_id, "mutation": "trash"}, indent=2)
    recordings = client.list_trash()
    return json.dumps([summarize_recording(r) for r in recordings], indent=2)


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


def _handle_upload(args: argparse.Namespace, client: PlaudClient) -> str:
    import tempfile

    from .transcode import get_file_type, transcode_to_mp3_path

    path = Path(args.file)
    if not path.exists():
        raise ValueError(f"file not found: {args.file}")
    file_type, needs_transcode = get_file_type(path)
    title = args.title or path.stem
    start_ms: int | None = None
    if args.start_time is not None:
        raw_st = str(args.start_time)
        if "-" in raw_st or "T" in raw_st:
            start_ms = parse_isoish(raw_st, "--start-time")
        else:
            try:
                start_ms = int(raw_st)
            except ValueError as exc:
                raise ValueError(f"Invalid --start-time value: {args.start_time}") from exc

    if needs_transcode:
        # Transcode to a temp MP3 on disk, then upload from that path
        # so the transcoded bytes never round-trip through Python memory.
        tmp_fd, tmp_mp3 = tempfile.mkstemp(suffix=".mp3", prefix="plaud-upload-")
        os.close(tmp_fd)
        tmp_mp3_path = Path(tmp_mp3)
        try:
            try:
                transcode_to_mp3_path(path, tmp_mp3_path)
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
            recording = client.upload_recording(
                tmp_mp3_path, title, file_type, start_time=start_ms, timezone_offset=args.timezone_offset
            )
        finally:
            try:
                tmp_mp3_path.unlink()
            except OSError:
                pass
    else:
        recording = client.upload_recording(
            path, title, file_type, start_time=start_ms, timezone_offset=args.timezone_offset
        )

    if args.folder_id:
        client.set_recording_folder(recording.id, args.folder_id)
    upload_result: dict[str, Any] = {
        "ok": True,
        "recording_id": recording.id,
        "filename": recording.filename,
        "transcoded": needs_transcode,
    }
    if not args.detach:
        client.transcribe_and_summarize(recording.id)
        client.wait_for_transcription(recording.id)
        if not args.skip_summary:
            client.wait_for_summary(recording.id)
        upload_result["transcribed"] = True
    else:
        upload_result["detached"] = True
    return json.dumps(upload_result, indent=2)


def _handle_merge(args: argparse.Namespace, client: PlaudClient) -> str:
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
    client.transcribe_and_summarize(args.recording_id, template_type=args.template)
    return json.dumps(
        {
            "accepted": True,
            "recording_id": args.recording_id,
            "template_type": args.template or "AUTO-SELECT",
        },
        indent=2,
    )


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
    return client.fetch_transcript(args.recording_id)


def _handle_ping(args: argparse.Namespace, client: PlaudClient) -> str:  # noqa: ARG001
    client.get_user_info()
    return json.dumps({"ok": True}, indent=2)


# ---------------------------------------------------------------------------
# Dispatch registries
# ---------------------------------------------------------------------------

# Commands that do not require a PlaudClient.
# Signature: (args, store, auth) -> str  (auth is only used by login)
_PRE_CLIENT_HANDLERS: dict[str, Callable[..., str]] = {
    "login": _handle_login,
    "refresh": _handle_refresh,
    "session": _handle_session,
    "update": _handle_update,
    "doctor": _handle_doctor,
}

# Commands that DO require a PlaudClient.
# Signature: (args, client) -> str
_CLIENT_HANDLERS: dict[str, Callable[[argparse.Namespace, PlaudClient], str]] = {
    "list": _handle_list,
    "search": _handle_search,
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


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    try:
        output = run_cli(args)
    except (PlaudApiError, PlaudSessionExpiredError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(output)
    return 0
