# ADR 004 — Install Layout, App Data, and the Absence of a Services Layer

**Status:** accepted (2026-05-24)

## Problem

An architecture review surfaced a cluster of helpers annexed by `mcp.py` and
`transport.py` that did not feel facade-local:

- `_http_error_to_api_error` in `transport.py` reads Plaud's `msg`/`code`
  envelope and builds `PlaudApiError` — Plaud-domain logic inside what is
  named a generic HTTP shim.
- `_decode_jwt_header_safe` in `mcp.py` decodes a JWT header.  `SessionManager`
  in `session.py` already has `_decode_expiry` doing the same shape of work
  on the payload.  Two implementations of "tear a UT JWT apart" lived
  next door to each other.
- `_classify_api_error` in `mcp.py` maps HTTP status to a `(code, retryable)`
  tuple — generic Plaud-API logic embedded in the MCP façade.
- `_write_event` in `mcp.py` and `_events_path` in both `mcp.py` and
  `tray/background.py` constitute cross-process IPC, with the path
  duplicated on both sides.
- `_install_dir`, `_cli_exe_path`, `_mcp_exe_path`, `_ffmpeg_path`,
  `_log_path` in `doctor.py`; `_install_dir` in `tray/setup.py`; and
  the `%LOCALAPPDATA%\PlaudTools\` path reconstructed inline in
  `ai_clients.py`, `mcp.py`, `server.py`, `session.py`, `tray/app.py`,
  `tray/setup.py`, `tray/uninstaller.py` (9+ sites).  Two of these
  (`doctor._install_dir` vs `tray/setup._install_dir`) return different
  paths under manual-extraction installs, a latent bug.

The initial hypothesis was that a shared "domain services" layer was
missing below the façades (cli / mcp / server) and above `client.py`,
where all of this could live.

## Decisions

### No new services layer

The Plaud-protocol annexed pieces are relocated to their natural homes,
not lifted into a new shared module:

| Annexed code | Old home | New home |
|---|---|---|
| `_http_error_to_api_error` | `transport.py` | `errors.py` as `PlaudApiError.from_http_error(exc)` classmethod |
| `_classify_api_error` | `mcp.py` | `errors.py` as `PlaudApiError.classify() -> (code, retryable)` method |
| `_decode_jwt_header_safe` | `mcp.py` | `session.py` beside `_decode_expiry` |
| session-y fields of `_diagnose_session_state` | `mcp.py` | `SessionManager.diagnose() -> dict` on `session.py` |

`mcp.py` retains a thin `_emit_session_expired` wrapper that calls
`SessionManager.diagnose()` and adds MCP-process-local fields (`mcp_pid`,
`mcp_version`, `env_token_present`).

After this move:
- `transport.py` is truly generic — it knows nothing about Plaud's error
  envelope.  Callers see `PlaudApiError` via `PlaudApiError.from_http_error`.
- `errors.py` becomes the deep error module: a lot of behaviour
  (envelope parsing, status classification, retryability) behind a
  small interface (`raise PlaudApiError.from_http_error(exc)` and
  `code, retryable = exc.classify()`).
- `session.py` owns all UT-JWT introspection along the axis it already
  grew (it had `_decode_expiry`; now it also has `_decode_header_safe`
  and `diagnose()`).
- `mcp.py` drops ~80 lines of helpers.

The deletion test passes for each move: removing the helper from its
old location concentrates the logic at the one place callers already
think about that concern, instead of distributing it.

### New modules: `layout.py` and `appdata.py`

The local-install annexed pieces *do* warrant new modules, and along two
axes rather than one:

**`layout.py`** — owns `InstallLayout`, a frozen dataclass representing the
on-disk arrangement of binaries for the *running* install.  Branches on
`sys.platform × getattr(sys, "frozen", False)`.

```python
@dataclass(frozen=True)
class InstallLayout:
    channel: Literal["bundle", "pip", "dev"]
    install_root: Path | None     # None for pip/dev
    cli_exe: Path                 # always set (shutil.which fallback)
    mcp_exe: Path | None          # None for pip when not on PATH
    ffmpeg_exe: Path | None       # None for pip when not on PATH

    @classmethod
    def detect(cls) -> InstallLayout: ...
```

Consumers: `doctor`, `tray/setup`, `tray/updater`, `tray/uninstaller`,
`tray/background`, `ai_clients` (mcp_exe wiring), `mcp_lifecycle`
(process-scope `install_root`).

**`appdata.py`** — owns the per-user data directory and known files inside
it (session storage, tray log, MCP log, events).  Channel-agnostic.
Branches on `sys.platform` only.

```python
def data_dir() -> Path: ...
def tray_log() -> Path: ...
def mcp_log() -> Path: ...
def events_path() -> Path: ...
def session_path() -> Path: ...
```

Consumers: `session` (DPAPI shadow path; `FileSessionStore` default),
`mcp` (events write), `server` (mcp.log), `tray/setup` (tray.log),
`tray/background` (events read), `tray/uninstaller` (deletion targets),
`doctor` (reporting).

### Running install, not canonical install

`InstallLayout` represents the running install, derived from
`sys.executable`, not the canonical install path the installer uses.
Every Python-side consumer wants "this install" semantics:

- `mcp_lifecycle.shutdown_mcp_children` already takes an `install_dir`
  to scope process enumeration to this install (per ADR-003).
- `tray/updater` extracts the new zip over the running install.
- `tray/uninstaller` deletes the running install.
- `tray/setup._set_autostart` writes a registry entry pointing at the
  running install's `cli_exe`.
- `ai_clients.connect` wires AI clients to the running install's `mcp_exe`.
- `doctor` reports reality, which is the running install.

The canonical install path `%LOCALAPPDATA%\Programs\PlaudTools\` survives
only in `scripts/install.ps1`.  `tray/setup.py:_install_dir`'s hardcoded
canonical path is removed; it is replaced by `InstallLayout.detect()
.install_root`.  This closes the latent bug where a manually-extracted
bundle would have its autostart registry entry pointing at the empty
canonical location.

### `platformdirs` for cross-platform `appdata.py`

Linux and macOS bundles are on the roadmap.  `appdata.py` uses
`platformdirs.user_data_dir("PlaudTools", appauthor=False)` for the
data directory.  Verified to return the existing
`%LOCALAPPDATA%\PlaudTools\` on Windows, so the change is a no-op for
current users.

All log and event files share `data_dir()`; we deliberately do not split
out a `user_log_dir`.  Reasons:

- Windows: keeps `tray.log` and `mcp.log` exactly where they are.
- macOS: convention drift (`~/Library/Logs/` is the platform norm) is
  forgivable for a young project; can be split later if it becomes a
  real complaint.
- Linux: keeps everything under a single `XDG_DATA_HOME` subdir.

`FileSessionStore`'s default path moves from `~/.config/plaud-tools/
session.json` to `appdata.data_dir() / "session.json"`.  This is a
breaking change in principle, but `FileSessionStore` is the
last-resort fallback when both the keyring and DPAPI shadow have
failed — extremely rare in practice — so the migration cost is
near-zero.

### `events.jsonl` stays as just a path

`appdata.events_path()` is the single source of truth for the file location.
MCP and tray continue to hand-roll JSON serialisation.

A dedicated `events.py` module with `publish()` / `drain()` was considered
and rejected: with one event type (`session_expired`), one writer (MCP),
and one reader (tray), it would be a hypothetical seam.  If a second event
type lands the module earns itself; until then, the deletion test fails.

The read→truncate race in `tray/background.py:_event_poll_loop` (events
written between read and truncate are lost) is a real bug but is a bug
to fix in `tray/background.py`, not a reason to invent a module.

## Alternatives rejected

- **A single shared `services.py` layer below the façades** — rejected
  because the four annexed concerns split cleanly along two
  unrelated axes (Plaud-protocol vs. local-install) with different
  invalidation triggers and test surfaces.  A unified module would
  have been a god module by construction.
- **One unified `paths.py` covering install + appdata** — rejected
  because install paths branch on `sys.platform × sys.frozen` (6+
  combinations) while appdata branches on `sys.platform` only.
  Unifying would force every appdata consumer (7+ files) to depend
  on channel-detection complexity it does not need.  The deletion
  test passes for the split, not the union.
- **Roll-your-own platform branches in `appdata.py`** — rejected in
  favour of `platformdirs`.  We are not in the business of maintaining
  XDG / Apple / snap edge cases when a 20 KB BSD-licensed dependency
  with no transitive deps already does it.
- **`platformdirs.user_log_dir` for logs** — rejected.  Would move
  `tray.log` and `mcp.log` to a `Logs\` subdirectory on Windows,
  orphaning existing users' log discovery and offering minimal upside.
- **Canonical-path semantics in `InstallLayout`** — rejected.  Nothing
  in Python wants canonical-path semantics; only the PowerShell
  installer does, and it lives outside Python.  Keeping a canonical
  path alive in Python would re-introduce the bug where a relocated
  bundle's autostart registry entry points at the empty canonical
  location.
- **Discriminated-union `InstallLayout = BundleLayout | PipLayout`** —
  rejected as ceremony for a young codebase.  `channel: Literal[...]`
  plus `Path | None` fields are sufficient; consumers that only run
  in bundle mode (the tray) can assert that via a small helper.
- **A dedicated `events.py` IPC module** — rejected; deferred until a
  second event type exists.

## Out of scope

- Fixing the `events.jsonl` read→truncate race in
  `tray/background.py._event_poll_loop` (filed separately).
- Splitting `events.py` out from `appdata.py` (revisit when a second
  event type appears).
- Replacing `FileSessionStore` with something built on top of
  `appdata.session_path()` (the move is mechanical; the architecture
  is unchanged).
- Linux and macOS bundle layouts in `layout.py` (added incrementally
  when those distribution channels are built).
