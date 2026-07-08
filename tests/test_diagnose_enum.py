"""Unit tests for the --diagnose-enum diagnostic path in plaud_tray_entry.py.

These tests verify the flag's contract WITHOUT running a frozen build:
  - reports the active enumerator via importlib.util.find_spec / import
  - exits 0
  - does NOT import pystray, PIL, or tkinter (GUI modules must stay out of
    the --diagnose-enum code path so CI runners without a display are safe)
  - imports plaud_tools.cli.process_probe successfully (exercises the module's
    import chain in a non-frozen context as a unit-level smoke test)

The actual frozen-build assertion ("enumerator=psutil") is performed by the
bundle-smoke CI job in .github/workflows/ci.yml, which runs the real
PlaudTools.exe --diagnose-enum after building the tray spec.

Wave 2 / C4.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

# Path to the entry script under test.
_ENTRY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plaud_tray_entry.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_entry_with_flag(*flags: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run the tray entry script in a subprocess with the given flag(s).

    Uses the *current* Python interpreter so any installed packages (including
    psutil if present) are on the path.  The subprocess inherits the current
    sys.path so plaud_tools is importable.

    Capturing stdout/stderr lets us assert on printed output and exit codes
    without any display or GUI initialization.
    """
    import os

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, str(_ENTRY_SCRIPT), *flags],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


# ---------------------------------------------------------------------------
# --diagnose-enum contract tests
# ---------------------------------------------------------------------------


def test_diagnose_enum_exits_zero():
    """--diagnose-enum exits with code 0 on success."""
    result = _run_entry_with_flag("--diagnose-enum")
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}.\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def test_diagnose_enum_prints_enumerator_line():
    """--diagnose-enum prints exactly one 'enumerator=<value>' line."""
    result = _run_entry_with_flag("--diagnose-enum")
    assert result.returncode == 0
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    enum_lines = [line for line in lines if line.startswith("enumerator=")]
    assert len(enum_lines) == 1, (
        f"Expected exactly one 'enumerator=...' line, got: {lines!r}\nstderr: {result.stderr!r}"
    )


def test_diagnose_enum_reports_psutil_when_psutil_available():
    """When psutil is importable, --diagnose-enum reports enumerator=psutil."""
    # Skip if psutil is not installed in this environment.
    if importlib.util.find_spec("psutil") is None:
        pytest.skip("psutil not installed in this test environment")

    result = _run_entry_with_flag("--diagnose-enum")
    assert result.returncode == 0
    assert "enumerator=psutil" in result.stdout, (
        f"Expected 'enumerator=psutil' in stdout but got: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def test_diagnose_enum_reports_fallback_when_psutil_absent(tmp_path):
    """When psutil is NOT importable, --diagnose-enum reports enumerator=powershell_fallback.

    We inject a stub psutil.py that raises ImportError on import at the front of
    PYTHONPATH so the ``import psutil`` in the entry script fails, exercising
    the fallback branch.  The stub file takes priority over any real psutil in
    site-packages because it appears first on sys.path.
    """
    import os

    # Create a stub psutil.py that raises ImportError when imported.
    # A .py file at the top level of tmp_path is found before site-packages.
    stub = tmp_path / "psutil.py"
    stub.write_text('raise ImportError("stub psutil — simulating missing C extension")\n')

    src_dir = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    # Prepend tmp_path (containing the stub) and src/ to PYTHONPATH.
    env["PYTHONPATH"] = str(tmp_path) + os.pathsep + str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, str(_ENTRY_SCRIPT), "--diagnose-enum"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"Expected exit 0 even when psutil absent, got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert "enumerator=powershell_fallback" in result.stdout, (
        f"Expected 'enumerator=powershell_fallback' when psutil broken, "
        f"got: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )


def test_diagnose_enum_does_not_import_gui_modules():
    """--diagnose-enum must NOT import pystray, PIL, or tkinter.

    The flag is designed for headless CI runners.  Importing any GUI module
    under --diagnose-enum would hang or crash on runners without a display.
    We verify by inspecting sys.modules via a helper script that wraps the
    entry script execution in a try/except SystemExit so it can check
    sys.modules AFTER the entry script's sys.exit(0) call.
    """
    import os
    import textwrap

    # Wrap the entry script in a try/except SystemExit so we can inspect
    # sys.modules after the --diagnose-enum path calls sys.exit(0).
    full_script = textwrap.dedent(f"""
        import sys
        sys.argv = ["PlaudTools", "--diagnose-enum"]
        try:
            _entry = open({str(_ENTRY_SCRIPT)!r}).read()
            exec(compile(_entry, {str(_ENTRY_SCRIPT)!r}, "exec"), {{"__name__": "__main__"}})
        except SystemExit:
            pass  # expected: --diagnose-enum calls sys.exit(0)
        # Now check that no GUI modules were imported during the flag handling.
        forbidden = ["pystray", "PIL", "tkinter", "_tkinter"]
        found = [m for m in forbidden if m in sys.modules]
        if found:
            print(f"FAIL: GUI modules imported: {{found}}", flush=True)
            sys.exit(2)
        print("OK: no GUI modules imported", flush=True)
        sys.exit(0)
    """)

    env = os.environ.copy()

    result = subprocess.run(
        [sys.executable, "-c", full_script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"GUI module import check failed (exit {result.returncode}).\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert "OK: no GUI modules imported" in result.stdout, (
        f"Unexpected output: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


def test_diagnose_enum_imports_process_probe():
    """--diagnose-enum must import plaud_tools.cli.process_probe without error.

    This is the chain exercised by the frozen hiddenimports: process_probe
    imports psutil (or falls back), and the flag verifies the import resolves.
    If process_probe import fails the flag exits 1 and prints an error line —
    we assert it does NOT print a failure line and exits 0.
    """
    result = _run_entry_with_flag("--diagnose-enum")
    assert result.returncode == 0, (
        f"Expected exit 0 (process_probe imported OK), "
        f"got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    assert "process_probe import failed" not in result.stdout, (
        f"process_probe import failure detected in output: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Normal (non-flag) path: entry script does NOT call main() under --diagnose-enum
# ---------------------------------------------------------------------------


def test_normal_path_not_triggered_under_diagnose_enum(monkeypatch):
    """The normal tray startup (DPAPI shadow + main()) is bypassed under --diagnose-enum.

    We verify by running the entry script in a subprocess and confirming that
    it exits quickly (under --diagnose-enum) without attempting GUI initialization.
    If the normal path were taken the subprocess would either hang waiting for
    a display or crash importing pystray in an environment where it's stubbed.

    (The fact that the subprocess finishes and exits 0 in the other tests is
    already strong evidence; this test makes the intent explicit.)
    """
    result = _run_entry_with_flag("--diagnose-enum")
    # A quick clean exit (0) means the GUI path was NOT taken.
    assert result.returncode == 0
    # The entry script should not produce any tkinter/pystray error text.
    for gui_hint in ("tkinter", "pystray", "PIL", "Tk", "display"):
        assert gui_hint not in result.stderr.lower(), (
            f"Unexpected GUI hint {gui_hint!r} in stderr: {result.stderr!r}"
        )
