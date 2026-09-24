"""Shell completions must not drift from build_parser()'s actual subcommand set.

The bash/PowerShell/zsh completion scripts under
``src/plaud_tools/tray/completions/`` are hand-maintained string lists, not
generated from ``build_parser()`` -- nothing stops a new subcommand or flag
from shipping without a matching completion entry. These tests parse each
file's declared subcommand (and, for the flat/non-nested subcommands, flag)
set with a small regex and diff it against the parser's real one, so a
future drift fails CI instead of just being a stale tab-completion.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from plaud_tools.cli.cli import build_parser

COMPLETIONS_DIR = Path(__file__).resolve().parents[1] / "src" / "plaud_tools" / "tray" / "completions"

# `folder` and `session` have their own nested subcommands with their own
# flag sets; each completion file structures that nesting differently, so
# flag coverage below is only checked for the flat (non-nested) commands.
_NESTED_COMMANDS = {"folder", "session"}


def _canonical_subparsers() -> dict[str, argparse.ArgumentParser]:
    action = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return dict(action.choices)


def _canonical_flags(subparser: argparse.ArgumentParser) -> set[str]:
    # -h/--help is argparse's automatic help action; every completion file
    # lists a literal "--help" too, but that's just a completion convenience,
    # not something to diff against the parser's real flag set.
    return {
        opt for action in subparser._actions for opt in action.option_strings if opt not in ("-h", "--help")
    }


def _bash_flags() -> dict[str, set[str]]:
    text = (COMPLETIONS_DIR / "plaud-tools.bash").read_text(encoding="utf-8")
    flags: dict[str, set[str]] = {}
    pattern = r'^\s*([\w-]+)\)\s*\n\s*COMPREPLY=\(\$\(compgen -W "([^"]+)"'
    for match in re.finditer(pattern, text, re.MULTILINE):
        name, words = match.group(1), match.group(2)
        flags[name] = {w for w in words.split() if w.startswith("-") and w != "--help"}
    return flags


def _ps1_flags() -> dict[str, set[str]]:
    text = (COMPLETIONS_DIR / "plaud-tools.ps1").read_text(encoding="utf-8")
    match = re.search(r"\$_plaud_tools_flags = @\{(.+?)\n\}", text, re.DOTALL)
    assert match, "could not find $_plaud_tools_flags in plaud-tools.ps1"
    flags: dict[str, set[str]] = {}
    for line in match.group(1).splitlines():
        entry = re.match(r"\s*'([\w-]+)'\s*=\s*@\(([^)]*)\)", line)
        if not entry:
            continue
        name, words = entry.group(1), entry.group(2)
        flags[name] = {w for w in re.findall(r"'([^']+)'", words) if w.startswith("-") and w != "--help"}
    return flags


def _zsh_flags() -> dict[str, set[str]]:
    text = (COMPLETIONS_DIR / "_plaud_tools").read_text(encoding="utf-8")
    flags: dict[str, set[str]] = {}
    for match in re.finditer(r"^\s{16}([\w-]+)\)\n(.*?);;", text, re.MULTILINE | re.DOTALL):
        name, body = match.group(1), match.group(2)
        if name in _NESTED_COMMANDS:
            continue
        flags[name] = set(re.findall(r"'(-{1,2}[\w-]+)\+?\[", body))
    return flags


def test_completions_cover_every_subcommand_and_flag():
    canonical = _canonical_subparsers()
    canonical_names = set(canonical)

    bash_text = (COMPLETIONS_DIR / "plaud-tools.bash").read_text(encoding="utf-8")
    bash_match = re.search(r'local subcommands="([^"]+)"', bash_text)
    assert bash_match, "could not find the subcommands list in plaud-tools.bash"
    assert set(bash_match.group(1).split()) == canonical_names

    ps1_text = (COMPLETIONS_DIR / "plaud-tools.ps1").read_text(encoding="utf-8")
    ps1_match = re.search(r"\$_plaud_tools_subcommands = @\(([^)]+)\)", ps1_text, re.DOTALL)
    assert ps1_match, "could not find $_plaud_tools_subcommands in plaud-tools.ps1"
    assert set(re.findall(r"'([\w-]+)'", ps1_match.group(1))) == canonical_names

    zsh_text = (COMPLETIONS_DIR / "_plaud_tools").read_text(encoding="utf-8")
    zsh_match = re.search(r"subcommands=\(([^)]+)\)", zsh_text, re.DOTALL)
    assert zsh_match, "could not find the subcommands array in _plaud_tools"
    assert set(re.findall(r"'([\w-]+):", zsh_match.group(1))) == canonical_names

    bash_flags = _bash_flags()
    ps1_flags = _ps1_flags()
    zsh_flags = _zsh_flags()
    for name, subparser in canonical.items():
        if name in _NESTED_COMMANDS:
            continue
        expected = _canonical_flags(subparser)
        assert bash_flags.get(name, set()) == expected, f"plaud-tools.bash flags drifted for {name!r}"
        assert ps1_flags.get(name, set()) == expected, f"plaud-tools.ps1 flags drifted for {name!r}"
        assert zsh_flags.get(name, set()) == expected, f"_plaud_tools flags drifted for {name!r}"


def test_ps1_completions_are_pure_ascii():
    """PS 5.1 parses a BOM-less .ps1 as ANSI, so any non-ASCII byte corrupts it on read."""
    raw = (COMPLETIONS_DIR / "plaud-tools.ps1").read_bytes()
    raw.decode("ascii")
