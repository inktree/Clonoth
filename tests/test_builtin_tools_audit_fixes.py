"""Unit tests for Clonoth built-in tools audit fixes (grep return, oversized read_file startLine, grep $1 replace, apply_diff multi-match shift, get_context_window, execute_command timeout)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from toolbox.builtins.apply_diff import apply_diff
from toolbox.builtins.read_file import _MAX_TEXT_FILE_BYTES, _read_single_file_sync
from toolbox.builtins.search_in_files import _do_replace_sync, _do_search_sync, _search_result_text
from toolbox.context import ToolContext
from toolbox.registry import ToolRegistry
import re


def test_grep_search_result_text_returns_formatted_string(tmp_path: Path) -> None:
    f = tmp_path / "sample.py"
    f.write_text("alpha = 1\nbeta = 2\ngamma = 3\n", encoding="utf-8")
    rx = re.compile("beta", re.MULTILINE | re.IGNORECASE)
    res = _do_search_sync(tmp_path, rx, "**/*.py", 10, 1_000_000, tmp_path, output_mode="content")
    assert res["ok"] is True
    text = res["data"]["result"]
    assert isinstance(text, str) and "1 results found:" in text
    assert "sample.py:2 | beta" in text


def test_grep_replace_supports_dollar_capture_groups_and_literal_mode(tmp_path: Path) -> None:
    f1 = tmp_path / "regex_cap.txt"
    f1.write_text("foo_123 and bar_456", encoding="utf-8")
    rx1 = re.compile(r"([a-z]+)_(\d+)")
    res1 = _do_replace_sync(tmp_path, rx1, "$2:$1", "regex_cap.txt", 10, 1_000_000, tmp_path, is_regex=True)
    assert res1["ok"] is True
    assert f1.read_text(encoding="utf-8") == "123:foo and 456:bar"

    # Literal mode (is_regex=False): backslashes and $1 in replace_str are kept verbatim
    f2 = tmp_path / "literal.txt"
    f2.write_text("hello world", encoding="utf-8")
    rx2 = re.compile(re.escape("world"))
    res2 = _do_replace_sync(tmp_path, rx2, r"\\1 $1", "literal.txt", 10, 1_000_000, tmp_path, is_regex=False)
    assert res2["ok"] is True
    assert f2.read_text(encoding="utf-8") == r"hello \\1 $1"


def test_read_file_oversized_supports_start_line_streaming_and_head_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rf_mod = sys.modules["toolbox.builtins.read_file"]

    # Lower threshold to 2 KB for fast unit testing
    monkeypatch.setattr(rf_mod, "_MAX_TEXT_FILE_BYTES", 2048)
    big_file = tmp_path / "big.txt"
    lines = [f"line_{i:04d}_" + ("x" * 30) for i in range(1, 201)]
    big_file.write_text("\n".join(lines), encoding="utf-8")
    assert big_file.stat().st_size > 2048

    # 1. Without startLine/endLine -> 50-line head preview + hint mentioning startLine
    r_head, mm = _read_single_file_sync(big_file, "big.txt", None, None, tmp_path)
    assert mm is None
    assert r_head["success"] is True
    assert r_head["oversized"] is True
    assert r_head["startLine"] == 1
    assert r_head["endLine"] == 50
    assert "line_0001_" in r_head["content"]
    assert "line_0050_" in r_head["content"]
    assert "line_0051_" not in r_head["content"]
    assert "startLine=51" in r_head["hint"]
    assert "execute_command" in r_head["hint"]

    # 2. With explicit startLine=120, endLine=130 -> streams lines 120..130 and stops early without scanning to line 200
    r_mid, _ = _read_single_file_sync(big_file, "big.txt", 120, 130, tmp_path)
    assert r_mid["success"] is True
    assert r_mid["oversized"] is True
    assert r_mid["startLine"] == 120
    assert r_mid["endLine"] == 130
    assert r_mid["lineCount"] == 11
    assert "line_0120_" in r_mid["content"]
    assert "line_0130_" in r_mid["content"]
    assert "line_0131_" not in r_mid["content"]
    assert "startLine=131" in r_mid["hint"]

    # 3. Scan limit guard when startLine exceeds _MAX_OVERSIZED_SCAN_CHARS
    monkeypatch.setattr(rf_mod, "_MAX_OVERSIZED_SCAN_CHARS", 1000)
    r_deep, _ = _read_single_file_sync(big_file, "big.txt", 150, 160, tmp_path)
    assert r_deep["success"] is False
    assert "streaming scan limit" in r_deep["error"]


def test_apply_diff_multi_match_shift_tracks_actual_replaced_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ad_mod = sys.modules["toolbox.builtins.apply_diff"]

    async def _noop_guard(ctx: Any, op: str, params: dict[str, Any]) -> tuple[dict[str, Any], None]:
        return {}, None

    monkeypatch.setattr(ad_mod, "request_guard", _noop_guard)

    f = tmp_path / "code.py"
    # "target" appears on line 2 and line 5; "marker" appears on line 3 and line 7
    f.write_text(
        "line1\n"
        "target\n"
        "marker\n"
        "line4\n"
        "target\n"
        "line6\n"
        "marker\n",
        encoding="utf-8",
    )
    ctx = ToolContext(
        supervisor_url="http://127.0.0.1:1",
        session_id="s1",
        run_id="r1",
        worker_id="w1",
        workspace_root=tmp_path,
        http=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
    )
    # Diff 0 replaces the SECOND "target" (at line 5) with 3 lines (+2 lines shift ONLY after line 5).
    # Diff 1 targets the FIRST "marker" at original line 3 (which is BEFORE line 5, so its line number is still 3, NOT shifted to 5!).
    res = asyncio.run(apply_diff(
        {
            "path": "code.py",
            "diffs": [
                {"search": "target", "replace": "t_a\nt_b\nt_c", "start_line": 5},
                {"search": "marker", "replace": "FIRST_MARKER", "start_line": 3},
            ],
        },
        ctx,
    ))
    assert res["ok"] is True
    lines = f.read_text(encoding="utf-8").splitlines()
    # Line 3 ("marker") should be replaced with FIRST_MARKER, while the second "marker" at the end remains "marker"
    assert lines[2] == "FIRST_MARKER"
    assert lines[-1] == "marker"


def test_session_context_usage_does_not_crash_without_is_switchable_entry_node(tmp_path: Path) -> None:
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine
    from supervisor.state import SupervisorState

    st = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "events.jsonl", run_id="r1"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    sid = st.get_or_create_session(channel="discord", conversation_key="discord:123")
    st.update_context_usage(
        sid,
        {
            "node_id": "ereuna_main",
            "task_id": "t1",
            "usage": {"prompt_tokens": 50000, "completion_tokens": 500, "total_tokens": 50500, "cached_prompt_tokens": 48000},
        },
    )
    out = st.get_session_context_usage(sid)
    assert out["prompt_tokens"] == 50000
    assert out["usage"]["cached_prompt_tokens"] == 48000


def test_registry_schemas_and_tool_manage_pure_async_mode() -> None:
    root = Path(__file__).resolve().parents[1]
    reg = ToolRegistry(workspace_root=root, tools_dir=root / "tools")
    ct_props = reg.get_spec("create_or_update_tool")["input_schema"]["properties"]
    assert "pure" in ct_props and "async_mode" in ct_props
