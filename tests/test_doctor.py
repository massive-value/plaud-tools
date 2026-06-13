"""Unit tests for plaud_tools.doctor.

Every section of the JSON is independently testable via monkeypatching.
No network calls or real filesystem state is required.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from plaud_tools import doctor as _doctor_mod
from plaud_tools.doctor import (
    _ai_clients_section,
    _executables_section,
    _ffmpeg_path,
    _install_dir,
    _mcp_exe_path,
    _session_section,
    run_doctor,
    run_doctor_json,
)
from plaud_tools.session import PlaudSession, SessionStore

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


class _MemoryStore:
    """Minimal SessionStore stand-in backed by an in-memory dict."""

    def __init__(self, session: PlaudSession | None = None, source: str = "file"):
        self._session = session
        self._source = source

    def load_with_source(self):
        return self._session, self._source

    def load(self):
        return self._session


# ---------------------------------------------------------------------------
# install_dir
# ---------------------------------------------------------------------------


class TestInstallDir:
    def test_frozen_returns_parent_of_parent(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "cli" / "plaud-tools.exe"
        fake_exe.parent.mkdir()
        fake_exe.touch()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        result = _install_dir()
        assert result == tmp_path

    def test_unfrozen_returns_python_scripts_dir(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "python.exe"
        fake_exe.touch()
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        result = _install_dir()
        assert result == tmp_path


# ---------------------------------------------------------------------------
# Executable path helpers
# ---------------------------------------------------------------------------


class TestExecutablePaths:
    def test_mcp_exe_frozen(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "cli" / "plaud-tools.exe"
        fake_exe.parent.mkdir()
        fake_exe.touch()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        expected = tmp_path / "mcp" / "plaud-mcp.exe"
        assert _mcp_exe_path() == expected

    def test_mcp_exe_unfrozen_uses_which(self, monkeypatch, tmp_path):
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        which_path = str(tmp_path / "plaud-mcp")
        monkeypatch.setattr("shutil.which", lambda name: which_path if name == "plaud-mcp" else None)
        assert _mcp_exe_path() == Path(which_path)

    def test_ffmpeg_frozen(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "cli" / "plaud-tools.exe"
        fake_exe.parent.mkdir()
        fake_exe.touch()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(fake_exe))
        expected = tmp_path / "mcp" / "ffmpeg.exe"
        assert _ffmpeg_path() == expected

    def test_ffmpeg_unfrozen_uses_which(self, monkeypatch, tmp_path):
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        which_path = str(tmp_path / "ffmpeg")
        monkeypatch.setattr("shutil.which", lambda name: which_path if name == "ffmpeg" else None)
        assert _ffmpeg_path() == Path(which_path)


# ---------------------------------------------------------------------------
# _executables_section
# ---------------------------------------------------------------------------


class TestExecutablesSection:
    def test_all_exist_flagged_true(self, monkeypatch, tmp_path):
        cli = tmp_path / "plaud-tools.exe"
        mcp = tmp_path / "mcp" / "plaud-mcp.exe"
        ffmpeg = tmp_path / "mcp" / "ffmpeg.exe"
        for p in (cli, mcp, ffmpeg):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()

        monkeypatch.setattr(_doctor_mod, "_cli_exe_path", lambda: cli)
        monkeypatch.setattr(_doctor_mod, "_mcp_exe_path", lambda: mcp)
        monkeypatch.setattr(_doctor_mod, "_ffmpeg_path", lambda: ffmpeg)

        section = _executables_section()
        assert section["plaud-tools"]["exists"] is True
        assert section["plaud-mcp"]["exists"] is True
        assert section["ffmpeg"]["exists"] is True

    def test_missing_exe_flagged_false(self, monkeypatch, tmp_path):
        cli = tmp_path / "plaud-tools.exe"  # does NOT exist
        mcp = tmp_path / "plaud-mcp.exe"  # does NOT exist
        ffmpeg = tmp_path / "ffmpeg.exe"  # does NOT exist

        monkeypatch.setattr(_doctor_mod, "_cli_exe_path", lambda: cli)
        monkeypatch.setattr(_doctor_mod, "_mcp_exe_path", lambda: mcp)
        monkeypatch.setattr(_doctor_mod, "_ffmpeg_path", lambda: ffmpeg)

        section = _executables_section()
        assert section["plaud-tools"]["exists"] is False
        assert section["plaud-mcp"]["exists"] is False
        assert section["ffmpeg"]["exists"] is False

    def test_paths_are_strings(self, monkeypatch, tmp_path):
        p = tmp_path / "exe"
        monkeypatch.setattr(_doctor_mod, "_cli_exe_path", lambda: p)
        monkeypatch.setattr(_doctor_mod, "_mcp_exe_path", lambda: p)
        monkeypatch.setattr(_doctor_mod, "_ffmpeg_path", lambda: p)
        section = _executables_section()
        assert isinstance(section["plaud-tools"]["path"], str)
        assert isinstance(section["plaud-mcp"]["path"], str)
        assert isinstance(section["ffmpeg"]["path"], str)


# ---------------------------------------------------------------------------
# _session_section
# ---------------------------------------------------------------------------


class TestSessionSection:
    def test_no_session(self):
        store = _MemoryStore(session=None, source="missing")
        result = _session_section(store)  # type: ignore[arg-type]
        assert result == {"present": False, "source": "missing"}

    def test_valid_session_never_includes_token(self, monkeypatch):
        # Build a fake JWT that expires far in the future
        import base64
        import time

        exp = int(time.time()) + 365 * 86400
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
        fake_jwt = f"header.{payload}.sig"

        session = PlaudSession(access_token=fake_jwt, region="us", email="test@example.com")
        store = _MemoryStore(session=session, source="keyring")
        result = _session_section(store)  # type: ignore[arg-type]

        assert result["present"] is True
        assert result["source"] == "keyring"
        assert result["region"] == "us"
        assert result["status"] == "valid"
        assert "access_token" not in result
        assert "token" not in result
        assert isinstance(result["days_until_expiry"], int)
        assert result["days_until_expiry"] > 0

    def test_expired_session_status(self, monkeypatch):
        import base64
        import time

        # Token expired 10 days ago
        exp = int(time.time()) - 10 * 86400
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
        fake_jwt = f"header.{payload}.sig"

        session = PlaudSession(access_token=fake_jwt, region="eu", email="old@example.com")
        store = _MemoryStore(session=session, source="file")
        result = _session_section(store)  # type: ignore[arg-type]

        assert result["present"] is True
        # status should be non-"valid" (expired or expiring-soon)
        assert result["status"] != "valid"


# ---------------------------------------------------------------------------
# _ai_clients_section
# ---------------------------------------------------------------------------


class TestAiClientsSection:
    def test_not_detected_when_no_config_files(self, monkeypatch, tmp_path):
        # Point all client config paths at non-existent files
        fake_paths = {
            "claude-desktop": tmp_path / "nonexistent.json",
            "claude-code": tmp_path / "nonexistent2.json",
            "codex": tmp_path / "nonexistent.toml",
        }
        monkeypatch.setattr("plaud_tools.ai_clients._client_paths", lambda: fake_paths)
        monkeypatch.setattr(_doctor_mod, "_mcp_exe_path", lambda: tmp_path / "plaud-mcp.exe")

        section = _ai_clients_section()
        for client_id in ("claude-desktop", "claude-code", "codex"):
            assert section[client_id]["detected"] is False
            assert section[client_id]["status"] == "not-detected"
            assert "mcp_command" not in section[client_id]

    def test_connected_when_config_matches(self, monkeypatch, tmp_path):
        mcp_exe = tmp_path / "plaud-mcp.exe"
        mcp_exe.touch()

        config = tmp_path / "claude.json"
        config.write_text(
            json.dumps({"mcpServers": {"plaud": {"command": str(mcp_exe)}}}),
            encoding="utf-8",
        )
        fake_paths = {
            "claude-desktop": config,
            "claude-code": tmp_path / "nonexistent.json",
            "codex": tmp_path / "nonexistent.toml",
        }
        monkeypatch.setattr("plaud_tools.ai_clients._client_paths", lambda: fake_paths)
        monkeypatch.setattr(_doctor_mod, "_mcp_exe_path", lambda: mcp_exe)

        section = _ai_clients_section()
        assert section["claude-desktop"]["detected"] is True
        assert section["claude-desktop"]["status"] == "connected"
        assert section["claude-desktop"]["mcp_command"] == str(mcp_exe)

    def test_stale_when_mcp_command_differs(self, monkeypatch, tmp_path):
        old_mcp = tmp_path / "old" / "plaud-mcp.exe"
        new_mcp = tmp_path / "new" / "plaud-mcp.exe"
        new_mcp.parent.mkdir(parents=True)
        new_mcp.touch()

        config = tmp_path / "claude.json"
        config.write_text(
            json.dumps({"mcpServers": {"plaud": {"command": str(old_mcp)}}}),
            encoding="utf-8",
        )
        fake_paths = {
            "claude-desktop": config,
            "claude-code": tmp_path / "nonexistent.json",
            "codex": tmp_path / "nonexistent.toml",
        }
        monkeypatch.setattr("plaud_tools.ai_clients._client_paths", lambda: fake_paths)
        monkeypatch.setattr(_doctor_mod, "_mcp_exe_path", lambda: new_mcp)

        section = _ai_clients_section()
        assert section["claude-desktop"]["status"] == "stale"
        assert section["claude-desktop"]["mcp_command"] == str(old_mcp)


# ---------------------------------------------------------------------------
# run_doctor (full document)
# ---------------------------------------------------------------------------


class TestRunDoctor:
    def _stub_env(self, monkeypatch, tmp_path):
        """Monkeypatch all I/O so run_doctor works hermetically."""
        fake_exe = tmp_path / "plaud-tools.exe"
        fake_mcp = tmp_path / "plaud-mcp.exe"
        fake_ffmpeg = tmp_path / "ffmpeg.exe"
        for p in (fake_exe, fake_mcp, fake_ffmpeg):
            p.touch()

        monkeypatch.setattr(_doctor_mod, "_install_dir", lambda: tmp_path)
        monkeypatch.setattr(_doctor_mod, "_cli_exe_path", lambda: fake_exe)
        monkeypatch.setattr(_doctor_mod, "_mcp_exe_path", lambda: fake_mcp)
        monkeypatch.setattr(_doctor_mod, "_ffmpeg_path", lambda: fake_ffmpeg)
        monkeypatch.setattr(_doctor_mod, "_log_path", lambda: tmp_path / "tray.log")

        # Point AI clients at nonexistent config files (not-detected)
        fake_client_paths = {
            "claude-desktop": tmp_path / "nonexistent.json",
            "claude-code": tmp_path / "nonexistent2.json",
            "codex": tmp_path / "nonexistent.toml",
        }
        monkeypatch.setattr("plaud_tools.ai_clients._client_paths", lambda: fake_client_paths)

    def test_schema_keys_present(self, monkeypatch, tmp_path):
        self._stub_env(monkeypatch, tmp_path)
        store = _MemoryStore(session=None, source="missing")
        result = run_doctor(store)  # type: ignore[arg-type]

        assert "version" in result
        assert "frozen" in result
        assert "install_dir" in result
        assert "executables" in result
        assert "session" in result
        assert "ai_clients" in result
        assert "log_path" in result
        assert "mcp_lifecycle" in result

    def test_version_is_string(self, monkeypatch, tmp_path):
        self._stub_env(monkeypatch, tmp_path)
        store = _MemoryStore(session=None, source="missing")
        result = run_doctor(store)  # type: ignore[arg-type]
        assert isinstance(result["version"], str)

    def test_token_never_in_output(self, monkeypatch, tmp_path):
        import base64
        import time

        self._stub_env(monkeypatch, tmp_path)
        exp = int(time.time()) + 365 * 86400
        payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
        fake_jwt = f"header.{payload}.sig"
        session = PlaudSession(access_token=fake_jwt, region="us", email="a@b.com")
        store = _MemoryStore(session=session, source="keyring")

        result = run_doctor(store)  # type: ignore[arg-type]
        output_str = json.dumps(result)
        assert fake_jwt not in output_str

    def test_run_doctor_json_is_valid_json(self, monkeypatch, tmp_path):
        self._stub_env(monkeypatch, tmp_path)
        store = _MemoryStore(session=None, source="missing")
        raw = run_doctor_json(store)  # type: ignore[arg-type]
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)

    def test_executables_all_present_when_files_exist(self, monkeypatch, tmp_path):
        self._stub_env(monkeypatch, tmp_path)
        store = _MemoryStore(session=None, source="missing")
        result = run_doctor(store)  # type: ignore[arg-type]
        exes = result["executables"]
        assert exes["plaud-tools"]["exists"] is True
        assert exes["plaud-mcp"]["exists"] is True
        assert exes["ffmpeg"]["exists"] is True

    def test_frozen_field_matches_sys(self, monkeypatch, tmp_path):
        self._stub_env(monkeypatch, tmp_path)
        store = _MemoryStore(session=None, source="missing")
        # default: not frozen
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)
        result = run_doctor(store)  # type: ignore[arg-type]
        assert result["frozen"] is False


# ---------------------------------------------------------------------------
# CLI integration: plaud-tools doctor prints valid JSON
# ---------------------------------------------------------------------------


class TestDoctorCli:
    def _stub_env(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "plaud-tools.exe"
        fake_mcp = tmp_path / "plaud-mcp.exe"
        fake_ffmpeg = tmp_path / "ffmpeg.exe"
        for p in (fake_exe, fake_mcp, fake_ffmpeg):
            p.touch()
        monkeypatch.setattr(_doctor_mod, "_install_dir", lambda: tmp_path)
        monkeypatch.setattr(_doctor_mod, "_cli_exe_path", lambda: fake_exe)
        monkeypatch.setattr(_doctor_mod, "_mcp_exe_path", lambda: fake_mcp)
        monkeypatch.setattr(_doctor_mod, "_ffmpeg_path", lambda: fake_ffmpeg)
        monkeypatch.setattr(_doctor_mod, "_log_path", lambda: tmp_path / "tray.log")
        fake_client_paths = {
            "claude-desktop": tmp_path / "nonexistent.json",
            "claude-code": tmp_path / "nonexistent2.json",
            "codex": tmp_path / "nonexistent.toml",
        }
        monkeypatch.setattr("plaud_tools.ai_clients._client_paths", lambda: fake_client_paths)

    def test_doctor_returns_valid_json(self, monkeypatch, tmp_path):
        from plaud_tools.cli import run_cli

        self._stub_env(monkeypatch, tmp_path)

        store = SessionStore.__new__(SessionStore)  # bypass __init__
        store.file_store = type("_FS", (), {"path": tmp_path / "session.json"})()

        # Stub load_with_source on the instance
        store.load_with_source = lambda: (None, "missing")  # type: ignore[method-assign]
        store.load = lambda: None  # type: ignore[method-assign]

        output = run_cli(["doctor"], session_store=store)
        parsed = json.loads(output)
        assert parsed["version"]
        assert "executables" in parsed
        assert "session" in parsed
        assert "ai_clients" in parsed


# ---------------------------------------------------------------------------
# mcp_lifecycle field
# ---------------------------------------------------------------------------

_VALID_ENUMERATORS = {"psutil", "wmic", "powershell", "none"}


class TestMcpLifecycleField:
    def _stub_env(self, monkeypatch, tmp_path):
        fake_exe = tmp_path / "plaud-tools.exe"
        fake_mcp = tmp_path / "plaud-mcp.exe"
        fake_ffmpeg = tmp_path / "ffmpeg.exe"
        for p in (fake_exe, fake_mcp, fake_ffmpeg):
            p.touch()
        monkeypatch.setattr(_doctor_mod, "_install_dir", lambda: tmp_path)
        monkeypatch.setattr(_doctor_mod, "_cli_exe_path", lambda: fake_exe)
        monkeypatch.setattr(_doctor_mod, "_mcp_exe_path", lambda: fake_mcp)
        monkeypatch.setattr(_doctor_mod, "_ffmpeg_path", lambda: fake_ffmpeg)
        monkeypatch.setattr(_doctor_mod, "_log_path", lambda: tmp_path / "tray.log")
        fake_client_paths = {
            "claude-desktop": tmp_path / "nonexistent.json",
            "claude-code": tmp_path / "nonexistent2.json",
            "codex": tmp_path / "nonexistent.toml",
        }
        monkeypatch.setattr("plaud_tools.ai_clients._client_paths", lambda: fake_client_paths)

    def test_mcp_lifecycle_field_present(self, monkeypatch, tmp_path):
        self._stub_env(monkeypatch, tmp_path)
        store = _MemoryStore(session=None, source="missing")
        result = run_doctor(store)  # type: ignore[arg-type]
        assert "mcp_lifecycle" in result
        lifecycle = result["mcp_lifecycle"]
        assert isinstance(lifecycle, dict)
        assert "enumerator" in lifecycle

    def test_mcp_lifecycle_enumerator_is_valid(self, monkeypatch, tmp_path):
        self._stub_env(monkeypatch, tmp_path)
        store = _MemoryStore(session=None, source="missing")
        result = run_doctor(store)  # type: ignore[arg-type]
        enumerator = result["mcp_lifecycle"]["enumerator"]
        assert enumerator in _VALID_ENUMERATORS, f"unexpected enumerator value: {enumerator!r}"

    def test_mcp_lifecycle_psutil_when_available(self, monkeypatch, tmp_path):
        """When psutil can be imported, enumerator must be 'psutil'."""
        import types

        self._stub_env(monkeypatch, tmp_path)
        # Inject a fake psutil so the ImportError branch is skipped.
        fake_psutil = types.ModuleType("psutil")
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

        import plaud_tools.mcp_lifecycle as _lc

        monkeypatch.setattr(_lc, "active_enumerator_name", lambda: "psutil")
        monkeypatch.setattr(_doctor_mod, "active_enumerator_name", lambda: "psutil")

        store = _MemoryStore(session=None, source="missing")
        result = run_doctor(store)  # type: ignore[arg-type]
        assert result["mcp_lifecycle"]["enumerator"] == "psutil"

    def test_mcp_lifecycle_none_when_no_psutil_posix(self, monkeypatch, tmp_path):
        """On non-Windows without psutil, enumerator must be 'none'."""
        self._stub_env(monkeypatch, tmp_path)
        monkeypatch.setattr(_doctor_mod, "active_enumerator_name", lambda: "none")

        store = _MemoryStore(session=None, source="missing")
        result = run_doctor(store)  # type: ignore[arg-type]
        assert result["mcp_lifecycle"]["enumerator"] == "none"


# ---------------------------------------------------------------------------
# _mcp_exe_path dev-fallback platform awareness
# ---------------------------------------------------------------------------


class TestMcpExeDevFallback:
    """The dev-fallback path must omit .exe on POSIX."""

    def _clear_frozen(self, monkeypatch):
        if hasattr(sys, "frozen"):
            monkeypatch.delattr(sys, "frozen", raising=False)

    def test_dev_fallback_no_exe_on_posix(self, monkeypatch):
        """When sys.platform is not win32 and no mcp exe is found, path has no .exe suffix."""
        self._clear_frozen(monkeypatch)
        # Make shutil.which return None so layout falls through to dev fallback.
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(sys, "platform", "linux")

        result = _mcp_exe_path()
        # result is a Path or None; dev fallback always returns a Path
        assert result is not None
        assert result.suffix != ".exe", f"Expected no .exe suffix on POSIX, got: {result}"

    def test_dev_fallback_has_exe_on_windows(self, monkeypatch):
        """When sys.platform is win32 and no mcp exe is found, path has .exe suffix."""
        self._clear_frozen(monkeypatch)
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(sys, "platform", "win32")

        result = _mcp_exe_path()
        assert result is not None
        assert result.suffix == ".exe", f"Expected .exe suffix on Windows, got: {result}"
