"""Golden-output snapshot tests for MCP tool descriptions.

These tests lock the shape of every tool in ``_TOOLS`` — name, description,
and full inputSchema — so that any future change (description rewrite, new
parameter, removed enum value, etc.) produces a CI failure that demands an
intentional fixture update rather than a silent token-cost regression.

Regenerating the fixture
------------------------
When a change to ``_TOOLS`` is intentional, regenerate the golden file by
running pytest with the ``PLAUD_GOLDEN_REGEN=1`` environment variable::

    PLAUD_GOLDEN_REGEN=1 pytest tests/test_mcp_golden.py

Commit the updated ``tests/data/tool_descriptions.golden.json`` alongside the
``server.py`` change so reviewers can diff both in the same PR.

Token budget
------------
``test_tool_descriptions_within_token_budget`` uses a coarse word-count proxy
(splitting the compact JSON serialization on whitespace) to guard against
description inflation.  The budget (``_TOKEN_BUDGET_WORDS``, see its own
comment for the baseline it was set against) leaves roughly 10% headroom over
the actual current count.  If ``tiktoken`` is ever added as a dev dependency
the proxy can be swapped for an exact cl100k_base count without changing the
budget constant.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plaud_tools.server import _TOOLS

_GOLDEN_PATH = Path(__file__).parent / "data" / "tool_descriptions.golden.json"

# 1.1 * 442 word baseline set at v0.6.0's 12-tool surface. Wave 4A (v0.7.0)
# merged rename_speaker + correct_transcript into edit_transcript(action=...),
# dropping the count to 11 tools and the actual live count to ~439 words --
# comfortably under this budget, so it was left as-is rather than re-tightened.
# Update intentionally (and re-baseline the comment) if descriptions change.
_TOKEN_BUDGET_WORDS = 486


def _serialize_tools() -> str:
    """Serialize _TOOLS to a canonical, deterministic JSON string."""
    snapshot = [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.inputSchema,
        }
        for t in _TOOLS
    ]
    return json.dumps(snapshot, indent=2, sort_keys=True)


def test_tool_descriptions_match_golden(tmp_path: Path) -> None:
    """Serialized _TOOLS must exactly match the checked-in golden fixture.

    If ``PLAUD_GOLDEN_REGEN=1`` is set the fixture is rewritten in-place and
    the test passes unconditionally so a single pytest run updates + validates.
    """
    live = _serialize_tools()

    if os.environ.get("PLAUD_GOLDEN_REGEN") == "1":
        _GOLDEN_PATH.write_text(live + "\n", encoding="utf-8")
        return  # regenerated — nothing left to assert

    if not _GOLDEN_PATH.exists():
        pytest.fail(
            f"Golden fixture not found: {_GOLDEN_PATH}\n"
            "Run `PLAUD_GOLDEN_REGEN=1 pytest tests/test_mcp_golden.py` to generate it."
        )

    golden = _GOLDEN_PATH.read_text(encoding="utf-8").rstrip("\n")
    live_stripped = live.rstrip("\n")

    if live_stripped != golden:
        # Build a human-readable line diff so the failure message shows exactly
        # what changed without requiring the developer to run a separate diff.
        import difflib

        diff_lines = list(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                live_stripped.splitlines(keepends=True),
                fromfile="golden (tests/data/tool_descriptions.golden.json)",
                tofile="live (_TOOLS in server.py)",
            )
        )
        diff_text = "".join(diff_lines)
        pytest.fail(
            "MCP tool descriptions have changed.\n\n"
            "If this change is intentional, regenerate the golden fixture:\n"
            "    PLAUD_GOLDEN_REGEN=1 pytest tests/test_mcp_golden.py\n\n"
            "Diff (golden → live):\n" + diff_text
        )


def test_tool_descriptions_within_token_budget() -> None:
    """Total word count of the compact JSON serialization must stay under budget.

    This is a coarse proxy for token cost.  See ``_TOKEN_BUDGET_WORDS``'s own
    comment for what baseline the current 486-word budget was set against.
    Exceeding it means descriptions have been inflated and should be trimmed
    before merging.
    """
    compact = json.dumps(
        [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.inputSchema,
            }
            for t in _TOOLS
        ],
        separators=(",", ":"),
    )
    word_count = len(compact.split())
    assert word_count <= _TOKEN_BUDGET_WORDS, (
        f"Tool descriptions exceed the token budget proxy: "
        f"{word_count} words > {_TOKEN_BUDGET_WORDS} allowed.\n"
        "Trim descriptions in server.py or raise the budget intentionally."
    )
