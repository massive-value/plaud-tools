# Install methods

plaud-tools ships through three channels. The Windows tray bundle is the recommended path for most users and is documented in the main [README](../README.md). This file covers the alternative methods.

| Method | Audience | Auto-updates? |
|---|---|---|
| Windows tray bundle (in README) | Most users | Yes, via the tray |
| PyPI (`pip install plaud-tools`) | Python users, macOS/Linux | Via `plaud-tools update` |
| Manual zip extraction | Air-gapped Windows machines, restrictive IT | No (manual re-extract) |
| Editable install from source | Contributors | No (git pull) |

---

## Prerequisites

- **Python 3.11+** for any method that isn't the Windows tray bundle. The bundle ships its own Python runtime.
- **ffmpeg** (optional) — required only when uploading audio files in formats other than MP3, OPUS, or OGG. Install via your OS package manager and ensure `ffmpeg` is on `PATH`, or set `FFMPEG_BIN` to the binary path. The Windows bundle ships its own ffmpeg.

---

## PyPI install (any OS)

```
pip install plaud-tools
```

This installs the `plaud-tools` CLI, the `pt` short alias, and the `plaud-mcp` server entry point. Optional tray extra (Windows only):

```
pip install "plaud-tools[tray]"
```

### pipx

```
pipx install plaud-tools
```

### uv

```
uv tool install plaud-tools
```

### Updating a PyPI install

For plain pip installs, use the built-in subcommand:

```
plaud-tools update
```

This runs `pip install --upgrade plaud-tools` in the current Python environment and streams pip's output. pipx, uv, and conda users should run their own package manager's upgrade command instead.

### Uninstalling a PyPI install

```
pip uninstall plaud-tools
```

This removes the package and entry points. To also remove the saved Plaud session:

```
plaud-tools session clear
```

(Run this *before* `pip uninstall`, since the command lives in the package.) Or delete `~/.config/plaud-tools/session.json` and any matching entry from your OS keyring manually.

---

## Manual zip install (Windows)

Useful for air-gapped machines, restrictive IT environments where `irm | iex` is blocked, or any case where you want to inspect the contents before launching.

1. Open the [latest GitHub release](https://github.com/massive-value/plaud-tools/releases/latest) and download `PlaudTools.zip`.
2. Right-click the downloaded zip and choose **Properties → Unblock** (Windows marks files from the internet as locked by default).
3. Extract anywhere — `%LOCALAPPDATA%\Programs\PlaudTools\` is the conventional location that matches the install script.
4. Run `PlaudTools.exe` from the extracted folder.

On first launch, the tray app:

- Adds `PlaudTools\cli\` to your user `PATH` via `HKCU\Environment`, so `plaud-tools` and `pt` work from any new shell without manual PATH editing. No admin elevation required.
- Sources `PlaudTools\completions\plaud-tools.ps1` from your PowerShell profile, enabling tab-completion for both `plaud-tools` and `pt`.
- Registers a Run-on-login entry under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (visible and removable from the tray's Uninstall dialog).

Open a **new** PowerShell or cmd window after the first launch — the PATH change takes effect in new shells only.

### Updating a manual zip install

Re-download the zip from the latest release and overwrite the contents of your install folder. Quit PlaudTools from the tray first to avoid file-lock errors during extraction. Alternatively, use the in-tray updater (the **Update available: vX.X.X** menu item) — it works regardless of whether you installed via the script or by manual extraction.

### Uninstalling a manual zip install

Use the tray menu's **Uninstall…** item — it handles PATH cleanup, autostart cleanup, profile-line cleanup, and install-directory removal in one place. See the [main README](../README.md#uninstalling).

---

## Editable install from source

For contributors and anyone wanting to run a development build.

```
git clone https://github.com/massive-value/plaud-tools.git
cd plaud-tools
pip install -e ".[dev]"
pytest -q
```

The `[dev]` extra pulls in test and lint tooling. See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full contributor workflow, including the `PLAUD_LIVE_READS=1` live-test gate.

---

## Shell completions

### PowerShell (Windows tray bundle)

Completions are wired up automatically on first launch. No manual step needed. The tray sources `completions\plaud-tools.ps1` from your `$PROFILE`.

### PowerShell (pip install)

Locate `plaud-tools.ps1` inside the installed package and add a sourcing line to your profile:

```powershell
$ps1 = python -c "import plaud_tools, pathlib; print(pathlib.Path(plaud_tools.__file__).parent / 'completions' / 'plaud-tools.ps1')"
Add-Content $PROFILE ". `"$ps1`""
```

Reload your profile or open a new shell: `plaud-tools <Tab>` will cycle through subcommands.

### bash

```bash
source "$(python -c "import plaud_tools, pathlib; print(pathlib.Path(plaud_tools.__file__).parent / 'completions' / 'plaud-tools.bash')")"
```

Add the line above to your `~/.bashrc` to make it permanent.

### zsh

Copy `_plaud_tools` from the package to a directory on your `$fpath`:

```zsh
cp "$(python -c "import plaud_tools, pathlib; print(pathlib.Path(plaud_tools.__file__).parent / 'completions' / '_plaud_tools')")" ~/.zsh/completions/
# ensure fpath includes ~/.zsh/completions before calling compinit
```
