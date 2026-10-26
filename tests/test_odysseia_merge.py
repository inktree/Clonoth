"""Regression tests for merged Odysseia capabilities, provider options, compaction, and preempt."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from io import StringIO
from threading import RLock
from types import SimpleNamespace

import httpx
import pytest
import yaml

from clonoth_sdk.approval import classify_path
from clonoth_sdk.types import HealthInfo
from engine import compact, data_cleanup
from engine.builtin.usage_tracker import PLUGIN_META, UsageTracker
from engine.hooks import HookContext
from engine.hooks.registry import HookRegistry
from engine.inference.tool_format import JsonToolFormatter, NativeToolFormatter
from providers.base import ProviderResponse
from providers.deepseek import DeepSeekProvider
from providers.gemini import _parse_usage
from supervisor.config_store import ConfigStore
from supervisor.state import SupervisorState
from supervisor.task_store import TaskStoreMixin
from supervisor.types import MimoAudioConfigUpdateIn
from toolbox.builtins.read_file import _MAX_LINE_READ_CHARS, _read_bounded_line


def test_deepseek_official_defaults_and_thinking_options():
    async def run():
        async with httpx.AsyncClient() as http:
            provider = DeepSeekProvider(http=http, api_key="test", reasoning_effort="none", max_completion_tokens=123)
            payload = provider._build_payload([{"role": "user", "content": "hello"}], None)
            assert provider._base_url == "https://api.deepseek.com"
            assert payload["model"] == "deepseek-chat"
            assert payload["max_tokens"] == 123
            assert payload["thinking"] == {"type": "disabled"}
            assert "reasoning_effort" not in payload
            assert "max_completion_tokens" not in payload
            enabled = DeepSeekProvider(http=http, api_key="test", base_url="https://api.deepseek.com/v1/")
            assert enabled._base_url == "https://api.deepseek.com/v1"
            assert enabled._build_payload([], None)["thinking"] == {"type": "enabled"}
    asyncio.run(run())


def test_gemini_prompt_not_double_counted_and_reasoning_preserved():
    usage = _parse_usage({
        "promptTokenCount": 120,
        "toolUsePromptTokenCount": 120,
        "candidatesTokenCount": 9,
        "thoughtsTokenCount": 7,
        "cachedContentTokenCount": 40,
    })
    assert usage == {
        "prompt_tokens": 120,
        "completion_tokens": 16,
        "total_tokens": 136,
        "reasoning_tokens": 7,
        "cached_prompt_tokens": 40,
    }
    assert "reasoning_tokens" not in _parse_usage({"thoughtsTokenCount": "7"})


def test_microcompact_runtime_and_store_have_identical_content():
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    content = "long tool output\n" * 100
    messages = [{"role": "assistant", "content": "ok", "_meta": {"timestamp": old}}]
    stored = []
    for _ in range(3):
        messages.append({
            "role": "tool",
            "name": "search",
            "content": content,
            "_meta": {"usage": {"prompt_tokens": 1000}},
        })
        stored.append(SimpleNamespace(
            role="tool",
            name="search",
            content=content,
            meta={"usage": {"prompt_tokens": 1000}},
        ))
    _, count = compact.microcompact_messages(messages, keep_recent=1)
    _, stored_count = compact.microcompact_store(stored, keep_recent=1)
    assert count == stored_count == 2
    assert messages[1]["content"] == stored[0].content
    assert stored[0].meta["usage"]["_compacted_stale"] is True
    assert stored[-1].content == content
    assert compact.estimate_message_tokens({"tool_calls": [{"value": object()}]}) > 0


def test_reasoning_survives_formatter_boundary():
    message = {
        "role": "assistant",
        "content": "answer",
        "_meta": {"reasoning": "private chain", "thought_summary": "display only"},
    }
    for formatter in (NativeToolFormatter(), JsonToolFormatter()):
        result = formatter.message_to_llm(message)
        assert result["_meta"]["reasoning"] == "private chain"
        assert "thought_summary" not in result["_meta"]


def test_usage_tracker_runs_last_and_accumulates_all_six_fields():
    registry = HookRegistry()
    usage = dict(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        cached_prompt_tokens=3,
        cache_write_prompt_tokens=2,
        reasoning_tokens=4,
    )

    async def fallback(ctx):
        ctx.response = ProviderResponse(ok=True, usage=usage)

    tracker = UsageTracker()
    registry.register("after_llm_call", fallback, priority=-20)
    registry.register("after_llm_call", tracker.handle)
    rctx = SimpleNamespace(total_usage={})
    ctx = HookContext(
        messages=[], tools=[], node=None, provider=None,
        response=ProviderResponse(ok=False), rctx=rctx,
    )
    asyncio.run(registry.afire("after_llm_call", ctx))
    assert PLUGIN_META["priority"] == tracker.priority == -100
    assert rctx.total_usage == usage


def test_stats_cleanup_is_local_and_vacuums(tmp_path, monkeypatch):
    monkeypatch.setattr(data_cleanup, "DATA_DIR", tmp_path)
    db = tmp_path / "stats.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE requests (timestamp REAL, payload TEXT)")
        conn.execute("CREATE TABLE metadata (value TEXT)")
        conn.execute("INSERT INTO metadata VALUES ('keep')")
        conn.execute("INSERT INTO requests VALUES (0, 'old')")
        conn.execute("INSERT INTO requests VALUES (?, 'new')", (datetime.now(timezone.utc).timestamp(),))
    data_cleanup.prune_stats_db()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT payload FROM requests").fetchall() == [("new",)]
        assert conn.execute("SELECT value FROM metadata").fetchall() == [("keep",)]
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0


def _make_preempt(message, attachments):
    task = SimpleNamespace(preempt_message=message, preempt_attachments=attachments, preempt_requested=True)
    state = SimpleNamespace(_lock=RLock(), tasks={"task": task})
    return state, task


@pytest.mark.parametrize(
    "message,consumed,count,expected_text,expected_attachments",
    [
        ("first\n\nsecond", "first", 1, "second", ["new"]),
        ("first", "first", 1, "", ["new"]),
        ("", "", 1, "", ["new"]),
        ("new", "", 1, "new", ["new"]),
        ("new", "stale", 1, "new", ["old", "new"]),
    ],
)
def test_incremental_preempt_preserves_concurrent_text_and_attachments(
    message, consumed, count, expected_text, expected_attachments,
):
    state, task = _make_preempt(message, ["old", "new"])
    TaskStoreMixin.consume_preempt_message(state, "task", consumed, count)
    assert task.preempt_message == expected_text
    assert task.preempt_attachments == expected_attachments
    assert task.preempt_requested == bool(expected_text or expected_attachments)


def test_legacy_preempt_consumption_still_clears_everything():
    state, task = _make_preempt("message", ["attachment"])
    assert TaskStoreMixin.consume_preempt_message(state, "task") == {
        "message": "message",
        "attachments": ["attachment"],
    }
    assert not task.preempt_requested
    assert task.preempt_attachments == []


def test_openai_extension_keys_survive_mimo_configuration_updates(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "openai": {"model": "test", "reasoning_field": "reasoning_content", "custom": {"enabled": True}},
        "mimo_audio": {"tts_voice": "test-voice"},
    }), encoding="utf-8")
    store = ConfigStore(path=path)
    store.reload()
    store.update_mimo_audio(MimoAudioConfigUpdateIn(tts_model="updated-model"))
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["openai"]["reasoning_field"] == "reasoning_content"
    assert saved["openai"]["custom"] == {"enabled": True}
    assert store.get_mimo_audio_secret().tts_voice == "test-voice"


def test_switchability_uses_metadata_not_private_node_names(tmp_path):
    nodes = tmp_path / "config" / "nodes"
    nodes.mkdir(parents=True)
    (nodes / "blocked.yaml").write_text("id: blocked\nswitchable: false\n", encoding="utf-8")
    (nodes / "mini.yaml").write_text("id: mini\nswitchable: true\n", encoding="utf-8")
    state = SimpleNamespace(workspace_root=tmp_path)
    assert not SupervisorState._is_switchable_entry_node(state, "system.internal")
    assert not SupervisorState._is_switchable_entry_node(state, "blocked")
    assert SupervisorState._is_switchable_entry_node(state, "mini")


def test_bounded_line_reader_skips_long_tail_and_keeps_next_line():
    stream = StringIO("x" * (_MAX_LINE_READ_CHARS + 50) + "\nnext\n")
    line, truncated = _read_bounded_line(stream)
    assert truncated and line.endswith("[line truncated]")
    assert _read_bounded_line(stream) == ("next", False)


def test_sdk_health_remains_constructible_without_new_workspace_field():
    assert HealthInfo("ok", "run", "now", 1.0).workspace_root == ""


def test_sdk_external_path_classification(tmp_path):
    assert classify_path(tmp_path, [], "file.txt")[2] is False
    assert classify_path(tmp_path, [], "../outside.txt")[2] is True
    assert classify_path(tmp_path, [tmp_path.parent], "../trusted.txt")[2] is False


def test_health_workspace_and_reset_clear_context_usage(tmp_path):
    from fastapi.testclient import TestClient
    from supervisor.api import create_app
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine

    data = tmp_path / "data"
    data.mkdir()
    state = SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(data / "events.jsonl", "module-two-test"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    session_id = state.get_or_create_session(channel="test", conversation_key="test:reset")
    state._session_context_usage[(session_id, "entry")] = {"prompt_tokens": 500}
    assert state.reset_conversation(conversation_key="test:reset")["ok"]
    assert (session_id, "entry") not in state._session_context_usage
    app = create_app(
        state=state, process_manager=None, config_store=ConfigStore(path=data / "config.yaml"),
    )
    with TestClient(app) as client:
        assert client.get("/v1/health").json()["workspace_root"] == str(tmp_path)
    assert app.state.panel_auth is not None
