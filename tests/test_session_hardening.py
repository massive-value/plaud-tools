"""Regression tests for #144 (clear() legacy file) and #145 (FileSessionStore
BOM/corrupt/partial JSON tolerance + atomic save).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plaud_tools.session import FileSessionStore, PlaudSession, SessionStore

# ---------------------------------------------------------------------------
# #145 — FileSessionStore.load() must tolerate BOM / corrupt / partial JSON
# ---------------------------------------------------------------------------


class TestFileSessionStoreLoadHardening:
    def test_load_strips_bom_and_parses(self, tmp_path: Path) -> None:
        """A UTF-8 BOM prefix (e.g. written by a PS 5.1 `Set-Content` on
        another part of the toolchain) must not defeat the JSON parse."""
        path = tmp_path / "session.json"
        payload = json.dumps({"access_token": "tok", "region": "us", "email": "u@example.com"})
        path.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))

        store = FileSessionStore(path)
        session = store.load()

        assert session is not None
        assert session.access_token == "tok"
        assert session.email == "u@example.com"

    def test_load_returns_none_on_corrupt_json(self, tmp_path: Path) -> None:
        """Garbage content must fall through to None, not raise."""
        path = tmp_path / "session.json"
        path.write_text("{not valid json at all!!", encoding="utf-8")

        store = FileSessionStore(path)
        assert store.load() is None

    def test_load_returns_none_on_partial_json(self, tmp_path: Path) -> None:
        """A truncated write (e.g. process killed mid-save) must fall through
        to None, not raise."""
        path = tmp_path / "session.json"
        full = json.dumps({"access_token": "tok", "region": "us", "email": "u@example.com"})
        path.write_text(full[: len(full) // 2], encoding="utf-8")

        store = FileSessionStore(path)
        assert store.load() is None

    def test_load_returns_none_on_non_dict_json(self, tmp_path: Path) -> None:
        """Valid JSON that isn't an object (e.g. a bare list or string) must
        also fall through gracefully."""
        path = tmp_path / "session.json"
        path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

        store = FileSessionStore(path)
        assert store.load() is None

    def test_load_returns_none_on_unexpected_shape(self, tmp_path: Path) -> None:
        """Valid JSON object but missing the required access_token field
        (PlaudSession(**data) raises TypeError) must fall through to None."""
        path = tmp_path / "session.json"
        path.write_text(json.dumps({"totally": "unexpected"}), encoding="utf-8")

        store = FileSessionStore(path)
        assert store.load() is None

    def test_corrupt_new_file_falls_through_to_valid_legacy_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the primary (new appdata) file is corrupt but the legacy
        pre-ADR-004 file is intact, load() must still recover the legacy
        session rather than treating the corrupt primary as a hard stop."""
        new_path = tmp_path / "new" / "session.json"
        new_path.parent.mkdir(parents=True)
        new_path.write_text("{corrupt", encoding="utf-8")

        legacy_dir = tmp_path / "legacy" / ".config" / "plaud-tools"
        legacy_dir.mkdir(parents=True)
        legacy_path = legacy_dir / "session.json"
        legacy_path.write_text(
            json.dumps({"access_token": "legacy-tok", "region": "eu", "email": "legacy@example.com"}),
            encoding="utf-8",
        )

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "legacy"))

        store = FileSessionStore(new_path)
        session = store.load()

        assert session is not None
        assert session.access_token == "legacy-tok"


# ---------------------------------------------------------------------------
# #145 — FileSessionStore.save() must be atomic (temp file + os.replace)
# ---------------------------------------------------------------------------


class TestFileSessionStoreSaveIsAtomic:
    def test_save_leaves_no_tmp_file_and_content_is_correct(self, tmp_path: Path) -> None:
        path = tmp_path / "session.json"
        store = FileSessionStore(path)
        store.save(PlaudSession(access_token="tok", region="us", email="u@example.com"))

        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8"))["access_token"] == "tok"
        assert not (tmp_path / "session.json.tmp").exists(), "temp file must not linger after save()"

    def test_save_uses_os_replace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the mechanism explicitly: save() must go through os.replace
        (atomic on both POSIX and Windows), not Path.rename or a direct write
        to the final path."""
        import plaud_tools.session as session_mod

        path = tmp_path / "session.json"
        store = FileSessionStore(path)

        calls: list[tuple[str, str]] = []
        real_replace = session_mod.os.replace

        def spy_replace(src, dst):
            calls.append((str(src), str(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(session_mod.os, "replace", spy_replace)

        store.save(PlaudSession(access_token="tok", region="us"))

        assert len(calls) == 1
        src, dst = calls[0]
        assert dst == str(path)
        assert src != dst, "must write to a distinct temp path before replacing"

    def test_failed_tmp_write_leaves_original_file_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the temp-file write fails partway through, the previously saved
        (complete) file must be left exactly as it was — no torn write."""
        path = tmp_path / "session.json"
        store = FileSessionStore(path)
        store.save(PlaudSession(access_token="original", region="us"))
        original_bytes = path.read_bytes()

        def boom(self, *a, **k):
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(Path, "write_text", boom)

        with pytest.raises(OSError):
            store.save(PlaudSession(access_token="new", region="us"))

        assert path.read_bytes() == original_bytes, "original file must survive a failed save() attempt"
        assert not (tmp_path / "session.json.tmp").exists(), "failed temp write must not leave a tmp file"


# ---------------------------------------------------------------------------
# #144 — SessionStore.clear() must delete the legacy pre-ADR-004 file too
# ---------------------------------------------------------------------------


class TestClearRemovesLegacyFile:
    def test_clear_deletes_legacy_session_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for #144: before the fix, clear() only unlinked the new
        appdata-path session file. A user whose FileSessionStore fallback
        session lived at the legacy ~/.config/plaud-tools/session.json path
        would sign out, then immediately have that legacy file resurrect the
        old token on the very next load()."""
        new_path = tmp_path / "new" / "session.json"
        new_path.parent.mkdir(parents=True)

        legacy_dir = tmp_path / "legacy" / ".config" / "plaud-tools"
        legacy_dir.mkdir(parents=True)
        legacy_path = legacy_dir / "session.json"
        legacy_path.write_text(
            json.dumps({"access_token": "legacy-tok", "region": "us", "email": "old@example.com"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "legacy"))

        store = FileSessionStore(new_path)
        # Sanity: legacy fallback resurrects the old session before clear().
        assert store.load() is not None

        store.clear()

        assert not legacy_path.exists()
        assert store.load() is None, "clear() must prevent the legacy file from resurrecting a session"

    def test_session_store_clear_also_removes_legacy_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same guarantee through the full SessionStore.clear() (keyring +
        dpapi + file), not just FileSessionStore.clear() directly."""
        from plaud_tools.session import PlaudSession as _PlaudSession

        legacy_dir = tmp_path / "legacy" / ".config" / "plaud-tools"
        legacy_dir.mkdir(parents=True)
        legacy_path = legacy_dir / "session.json"
        legacy_path.write_text(
            json.dumps({"access_token": "legacy-tok", "region": "us", "email": "old@example.com"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "legacy"))

        plaintext = tmp_path / "session.json"
        store = SessionStore(plaintext, service_name="plaud-tools-test-clear-legacy", dpapi_path=None)
        store.save(_PlaudSession(access_token="new-tok", region="us"))

        store.clear()

        assert not legacy_path.exists()
        assert not plaintext.exists()
