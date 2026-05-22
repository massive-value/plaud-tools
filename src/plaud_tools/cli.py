from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

from . import __version__
from .auth import PlaudAuth
from .client import PlaudClient, PlaudRecordingQuery
from .errors import PlaudApiError, PlaudSessionExpiredError
from .query import filter_recordings, parse_isoish, summarize_recording
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

    folders_cmd = sub.add_parser("folders")

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
    upload_cmd.add_argument("--detach", action="store_true", help="Return immediately without waiting for transcription")
    upload_cmd.add_argument("--skip-summary", action="store_true", help="Wait for transcript only, not summary")
    upload_cmd.add_argument("--start-time", help="Recording timestamp as millisecond epoch integer or ISO 8601 string")
    upload_cmd.add_argument("--timezone-offset", type=float, help="UTC offset in hours (e.g. -7.0)")

    merge_cmd = sub.add_parser("merge")
    merge_cmd.add_argument("recording_ids", nargs="+")
    merge_cmd.add_argument("--title", required=True)

    dump_cmd = sub.add_parser("dump", help="Dump raw /file/detail API response for debugging")
    dump_cmd.add_argument("recording_id")

    login_cmd = sub.add_parser("login")
    login_cmd.add_argument("--email", required=True)
    login_cmd.add_argument("--password")
    login_cmd.add_argument("--region", choices=["us", "eu"], default="us")

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

    ping_cmd = sub.add_parser("ping")
    return parser


def _mask_token(token: str) -> str:
    if len(token) <= 12:
        return token
    return f"{token[:6]}...{token[-6:]}"


def _build_runtime_client(store: SessionStore) -> PlaudClient:
    return PlaudClient(SessionManager(store))


def run_cli(
    argv: Sequence[str],
    client: PlaudClient | None = None,
    session_store: SessionStore | None = None,
    auth: PlaudAuth | None = None,
) -> str:
    args = build_parser().parse_args(list(argv))
    store = session_store or SessionStore()
    if args.command == "login":
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

    if args.command == "session":
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

        session, source = store.load_with_source()
        if session is None:
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
                "region": session.region,
                "email": session.email,
                "status": status,
                "days_until_expiry": days,
                "token": session.access_token if args.show_token else _mask_token(session.access_token),
            },
            indent=2,
        )

    if args.command == "update":
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

    if args.command == "doctor":
        from .doctor import run_doctor_json

        return run_doctor_json(store)

    client = client or _build_runtime_client(store)

    if args.command == "list":
        has_filters = bool(args.since or args.until or args.query or args.folder_id or args.unfiled)
        since_ms = parse_isoish(args.since, "--since") if args.since else None
        until_ms = parse_isoish(args.until, "--until", end_of_day=True) if args.until else None
        if has_filters:
            recordings = client.list_recordings()
            recordings = filter_recordings(
                recordings,
                since_ms=since_ms,
                until_ms=until_ms,
                query=args.query,
                folder_id=args.folder_id,
                unfiled=args.unfiled,
            )
            recordings = recordings[: args.limit]
        else:
            recordings = client.list_recordings(
                PlaudRecordingQuery(limit=args.limit, is_trash=0, sort_by="start_time", is_desc=True)
            )
        return json.dumps([summarize_recording(r) for r in recordings], indent=2)
    if args.command == "search":
        since_ms = parse_isoish(args.since, "--since") if args.since else None
        until_ms = parse_isoish(args.until, "--until", end_of_day=True) if args.until else None
        recordings = client.list_recordings()
        recordings = filter_recordings(
            recordings,
            since_ms=since_ms,
            until_ms=until_ms,
            query=args.query,
            folder_id=args.folder_id,
            unfiled=False,
        )
        recordings = recordings[: args.limit]
        return json.dumps([summarize_recording(r) for r in recordings], indent=2)
    if args.command == "detail":
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
    if args.command == "show":
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
    if args.command == "summary":
        detail = client.get_recording(args.recording_id, include_summary=True)
        if not detail.ai_content:
            return json.dumps({"recording_id": args.recording_id, "summary": None, "note": "No summary available."}, indent=2)
        return json.dumps({"recording_id": args.recording_id, "summary": detail.ai_content}, indent=2)
    if args.command == "rename":
        client.rename_recording(args.recording_id, args.new_name)
        return json.dumps(
            {"ok": True, "recording_id": args.recording_id, "new_name": args.new_name},
            indent=2,
        )
    if args.command == "folders":
        tags = client.list_file_tags()
        return json.dumps(
            [
                {"id": tag.id, "name": tag.name, "color": tag.color, "icon": tag.icon}
                for tag in tags
            ],
            indent=2,
        )
    if args.command in ("move-to-folder", "move"):
        folder_id = None if args.folder_id == "-" else args.folder_id
        client.set_recording_folder(args.recording_id, folder_id)
        return json.dumps(
            {"ok": True, "recording_id": args.recording_id, "folder_id": folder_id},
            indent=2,
        )
    if args.command == "trash":
        if args.recording_id is not None:
            client.move_to_trash([args.recording_id])
            return json.dumps({"ok": True, "recording_id": args.recording_id, "mutation": "trash"}, indent=2)
        recordings = client.list_trash()
        return json.dumps([summarize_recording(r) for r in recordings], indent=2)
    if args.command == "restore":
        client.restore_from_trash([args.recording_id])
        return json.dumps({"ok": True, "recording_id": args.recording_id, "mutation": "restore"}, indent=2)
    if args.command == "delete":
        if not args.yes:
            raise ValueError(f"Permanent deletion of {args.recording_id!r} cannot be undone. Re-run with --yes to confirm.")
        client.delete_recordings([args.recording_id])
        return json.dumps({"ok": True, "recording_id": args.recording_id, "mutation": "delete"}, indent=2)
    if args.command == "trash-move":
        client.move_to_trash(args.recording_ids)
        return json.dumps(
            {"ok": True, "count": len(args.recording_ids), "recording_ids": args.recording_ids},
            indent=2,
        )
    if args.command == "trash-restore":
        client.restore_from_trash(args.recording_ids)
        return json.dumps(
            {"ok": True, "count": len(args.recording_ids), "recording_ids": args.recording_ids},
            indent=2,
        )
    if args.command == "rename-speaker":
        result = client.rename_speaker(args.recording_id, args.original_label, args.new_name)
        return json.dumps(
            {
                "ok": True,
                "recording_id": args.recording_id,
                "original_label": args.original_label,
                "new_name": args.new_name,
                "segments_updated": result["segments_updated"],
            },
            indent=2,
        )
    if args.command == "upload":
        from .transcode import get_file_type, transcode_to_mp3

        path = Path(args.file)
        if not path.exists():
            raise ValueError(f"file not found: {args.file}")
        file_type, needs_transcode = get_file_type(path)
        raw_bytes = path.read_bytes()
        try:
            audio_data = transcode_to_mp3(raw_bytes, path.suffix) if needs_transcode else raw_bytes
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
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
        recording = client.upload_recording(audio_data, title, file_type, start_time=start_ms, timezone_offset=args.timezone_offset)
        if args.folder_id:
            client.set_recording_folder(recording.id, args.folder_id)
        result: dict = {
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
            result["transcribed"] = True
        else:
            result["detached"] = True
        return json.dumps(result, indent=2)

    if args.command == "merge":
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

    if args.command == "transcribe":
        client.transcribe_and_summarize(args.recording_id, template_type=args.template)
        return json.dumps(
            {
                "accepted": True,
                "recording_id": args.recording_id,
                "template_type": args.template or "AUTO-SELECT",
            },
            indent=2,
        )
    if args.command == "status":
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
    if args.command == "dump":
        raw = client.dump_raw_detail(args.recording_id)
        return json.dumps(raw, indent=2)

    if args.command == "transcript":
        return client.fetch_transcript(args.recording_id)

    if args.command == "ping":
        client.get_user_info()
        return json.dumps({"ok": True}, indent=2)

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
