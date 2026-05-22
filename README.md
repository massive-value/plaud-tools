# plaud-tools

**Talk to your Plaud recordings.** Connect your Plaud account to Claude (or another AI assistant) so you can ask questions about your meetings, generate summaries, and search across every recording — in plain English.

**What you can do:**
- Ask things like *"Summarize my client call from Tuesday afternoon."*
- Ask things like *"What did we decide about the Henderson account in last week's meeting?"*
- Drop an audio file into the chat and say *"Upload and transcribe this."*

Windows users get a one-click installer with auto-updates. macOS and Linux users can install via pip — see [docs/INSTALL-METHODS.md](docs/INSTALL-METHODS.md).

## Quickstart

**1. Open PowerShell.** Press the Windows key, type `PowerShell`, and press Enter. A blue window opens.

**2. Paste this command and press Enter:**

```powershell
irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1 | iex
```

This downloads PlaudTools from GitHub to your user folder. No admin rights are needed and nothing is installed system-wide.

**3. Sign in.** When the install finishes, a Windows notification appears saying *"PlaudTools is now running in your system tray — click the icon to sign in."* Click the PlaudTools icon in your taskbar (bottom-right, next to the clock — you may need to click the up-arrow `^` to find it). A sign-in window opens. Enter your Plaud email and password.

> **Signed up for Plaud with Google?** You don't have a Plaud password yet. Visit [web.plaud.ai](https://web.plaud.ai), click "Forgot password," and Plaud will email you a reset link — even though you've never set a password before. Use that new password here.

**4. Connect the apps you use.** After sign-in, the PlaudTools window opens. Click **Configure AI Agents…**, then click **Connect** next to each AI app you have installed (Claude Desktop, Claude Code, or Codex). Apps you don't have installed are shown as **Not installed** and can't be connected.

**5. Restart the apps you connected.** New connections only load on a fresh start.

- **Claude Desktop** — go to **File → Exit** (closing the window keeps it running in the tray), then reopen it from the Start menu.
- **Claude Code** — in your existing session, type `/exit`, then run `claude` again in a new terminal.
- **Codex** — press `Ctrl+C` to end the session, then run `codex` again in a new terminal.

**6. Try it.** In your AI assistant, paste this prompt:

```text
I just installed an MCP for Plaud. Can you take a look and make sure
everything is wired up. As an example, can you tell me about my most
recent meeting that has a summary. After that, tell me about what sort
of things you can do with the plaud tools and some potential ways that
it can apply to my workflows.
```

The assistant will confirm the tools are wired up, walk through one of your recent recordings, and explain what else it can do for you.

## Keeping PlaudTools up to date

PlaudTools checks GitHub for new releases once a day and notifies you when one is available.

### Installing an update

When an update is ready, you'll see an **Update available: vX.X.X** item at the top of the tray menu. Click it, then click **Install update and restart**. PlaudTools downloads the new version, swaps it in, and relaunches — usually under 30 seconds. Your sign-in and connected apps carry over.

### Checking your version

Click the PlaudTools icon in your taskbar to open the home window. Your current version is shown in the footer.

### If something goes wrong

Re-run the install command in PowerShell with **`-Repair`**:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/massive-value/plaud-tools/main/scripts/install.ps1))) -Repair
```

This shuts down PlaudTools, wipes the install folder, and reinstalls the latest release. Your sign-in is preserved.

## Uninstalling

Open the tray menu, click **Uninstall…**, review the checklist, and click **Uninstall**. Your saved sign-in and log files are kept by default, so a future reinstall picks up where you left off.

## What PlaudTools can do

PlaudTools gives your AI assistant seven tools for working with your Plaud account. You don't call these directly — you ask in plain English, and the assistant picks the right one.

| What it does | What you'd ask |
|---|---|
| Find recordings | *"Show me my recordings from last week."* |
| Read a recording | *"What did I say in the Henderson meeting?"* |
| Rename, move, or trash | *"Rename yesterday's 9am recording to 'Tax planning call'."* |
| Upload audio | *"Upload this audio file and transcribe it."* (with attachment) |
| Transcribe and summarize | *"Transcribe and summarize yesterday's recording."* |
| List folders | *"What folders do I have in Plaud?"* |
| Merge recordings | *"Merge these three call segments into one recording."* |

## Other ways to install

The Windows tray bundle above is the recommended path for most users. Two other install methods exist:

- **PyPI** — if you have Python 3.11+, run `pip install plaud-tools`. You'll get the `plaud-tools` CLI and `plaud-mcp` server but no tray app or auto-updates.
- **Manual zip** — download `PlaudTools.zip` from the [latest GitHub release](https://github.com/massive-value/plaud-tools/releases/latest) and unzip anywhere. Useful for air-gapped machines or restrictive IT environments.

Full instructions for both: [docs/INSTALL-METHODS.md](docs/INSTALL-METHODS.md).

## Signing in again later

Your Plaud sign-in is good for about a year. When it's a month from expiring, the tray menu shows **Session expires in N days — sign in again** — click it and re-enter your password. If you wait until it fully expires, your AI assistant will tell you it can't reach Plaud; open the tray menu, click **Sign in…**, and you're back.

## Troubleshooting

**Claude (or your AI assistant) says it can't see Plaud after you wired it up.**
The connection only loads on a fresh start. Fully quit the app first — see step 5 of the Quickstart for the right way to do it on each platform — then reopen and try again.

**The PlaudTools icon isn't in your taskbar.**
Click the small up-arrow (`^`) next to the clock in the bottom-right of your taskbar — Windows hides newly-installed tray icons there by default. If you still don't see it, re-run the install command in PowerShell with `-Repair` (see "If something goes wrong" above).

Everything else — ffmpeg setup, region mismatches, manual config-file editing, antivirus quarantine, multi-account workflows — is in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## For developers

Source, dev environment setup, contributor workflow: [CONTRIBUTING.md](CONTRIBUTING.md).

## More documentation

- [docs/INSTALL-METHODS.md](docs/INSTALL-METHODS.md) — pip install, manual zip extraction, install from source
- [docs/AI-CLIENTS.md](docs/AI-CLIENTS.md) — manual JSON/TOML wiring for Claude Desktop, Claude Code, and Codex (Windows, macOS, Linux)
- [docs/CLI.md](docs/CLI.md) — full `plaud-tools` CLI reference
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — ffmpeg, region mismatches, antivirus quarantine, multi-account
- [CHANGELOG.md](CHANGELOG.md) — release notes
- [SECURITY.md](SECURITY.md) — security policy
- [LICENSE](LICENSE) — LGPL-3.0-or-later

## Important

**Alpha** — APIs and flags may change between minor versions. Pin versions in production wiring and check the [CHANGELOG](CHANGELOG.md) before upgrading.

**Unofficial** — not affiliated with, endorsed by, or sponsored by Plaud Inc. PlaudTools uses Plaud's web API directly, which their Terms of Service may restrict; your account could be rate-limited or suspended. Use at your own risk. You still need a real Plaud account; PlaudTools does not replace the Plaud mobile or web apps.

---

[![CI](https://github.com/massive-value/plaud-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/massive-value/plaud-tools/actions/workflows/ci.yml)
[![Bundle smoke](https://github.com/massive-value/plaud-tools/actions/workflows/ci.yml/badge.svg?event=push&label=bundle-smoke)](https://github.com/massive-value/plaud-tools/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/plaud-tools)](https://pypi.org/project/plaud-tools/)
![status: alpha](https://img.shields.io/badge/status-alpha-orange)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: LGPL-3.0-or-later](https://img.shields.io/badge/license-LGPL--3.0--or--later-blue)](LICENSE)
