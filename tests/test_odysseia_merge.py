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
    line, truncated, chars_read = _read_bounded_line(stream)
    assert truncated and line.endswith("[line truncated]")
    assert chars_read == _MAX_LINE_READ_CHARS + 51
    assert _read_bounded_line(stream) == ("next", False, 5)
    assert _read_bounded_line(stream) == (None, False, 0)


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


def test_knowledge_inject_hygiene_static_index_and_progressive_outlines(tmp_path: Path):
    from types import SimpleNamespace
    from engine.builtin.knowledge_inject import (
        build_knowledge_messages,
        build_scan_text,
        is_valid_keyword,
        list_memories,
        normalize_memory_entries,
        normalize_skill_entries,
        save_memory,
    )

    assert not is_valid_keyword("20")
    assert not is_valid_keyword("#9")
    assert not is_valid_keyword("ip")
    assert not is_valid_keyword("日志")
    assert is_valid_keyword("fb")
    assert is_valid_keyword("4a")
    assert is_valid_keyword(r"/\bh3\b/i")
    assert is_valid_keyword("墨树")
    assert is_valid_keyword("dol-tilemap")

    history = [
        {"role": "user", "content": "上一轮用户提问"},
        {"role": "assistant", "content": "上一轮助手回复包含 trigger_from_bot"},
        {"role": "user", "content": "动态块", "_dynamic": True},
    ]
    scan = build_scan_text("当前用户指令", history, scan_depth=1)
    assert "上一轮用户提问" in scan
    assert "trigger_from_bot" not in scan
    assert "动态块" not in scan

    skills = normalize_skill_entries([
        {
            "name": "card-workflow",
            "description": "v3.4.0｜角色卡完整制作工作流与分阶段检查清单",
            "enabled": True,
            "strategy": "normal",
            "keywords": ["制卡工作流", "20", "前端"],
            "body": "# 第一章 调研\n导言段落\n## 1.1 角色设定\n" + ("详细步骤内容\n" * 200),
        },
        {
            "name": "pure-index-skill",
            "description": "无关键词技能说明",
            "enabled": True,
            "strategy": "normal",
            "keywords": [],
            "body": "短正文",
        },
    ])
    memories = normalize_memory_entries([
        {
            "id": "workflow_packaged",
            "book": "rules",
            "content": "# 打包工作流\n这是无关键词的非constant成熟方案正文",
            "keywords": [],
            "has_raw_keywords": False,
            "constant": False,
            "enabled": True,
        },
        {
            "id": "garbage_kw_only",
            "book": "default",
            "content": "只有无效泛词的脏记忆",
            "keywords": ["20", "日志"],
            "has_raw_keywords": True,
            "constant": False,
            "enabled": True,
        },
        {
            "id": "long_active_mem",
            "book": "tech",
            "content": "# 架构方案\n" + ("核心细节说明\n" * 100),
            "keywords": ["架构方案触发词"],
            "has_raw_keywords": True,
            "constant": False,
            "enabled": True,
        },
    ])

    skill_static, skill_dynamic, mem_static, mem_dynamic = build_knowledge_messages(
        tmp_path,
        skills + memories,
        instruction_text="请按 制卡工作流 和 架构方案触发词 执行",
        skill_inline_max_chars=500,
        memory_inline_max_chars=200,
    )
    skill_static_text = "\n".join(m["content"] for m in skill_static)
    skill_dynamic_text = "\n".join(m["content"] for m in skill_dynamic)
    mem_static_text = "\n".join(m["content"] for m in mem_static)
    mem_dynamic_text = "\n".join(m["content"] for m in mem_dynamic)

    # Full compact skill index lives in static messages and includes activated skills too
    assert "[SKILLS:INDEX]" in skill_static_text
    assert "- card-workflow: 角色卡完整制作工作流与分阶段检查清单" in skill_static_text
    assert "- pure-index-skill: 无关键词技能说明" in skill_static_text
    assert "[SKILLS:INDEX]" not in skill_dynamic_text

    # Intentionally keywordless memory is in static [MEMORY:INDEX]; garbage-only kw entry is dropped
    assert "[MEMORY:INDEX]" in mem_static_text
    assert "rules/workflow_packaged" in mem_static_text
    assert "garbage_kw_only" not in mem_static_text
    assert "garbage_kw_only" not in mem_dynamic_text

    # Long activated skill and memory are rendered as progressive outlines
    assert "[Large skill" in skill_dynamic_text
    assert "- L1: # 第一章 调研" in skill_dynamic_text
    assert "[Long memory" in mem_dynamic_text
    assert 'list_memories(book="tech", id="long_active_mem")' in mem_dynamic_text

    # Verify list_memories(id=...) returns full untruncated content
    ctx = SimpleNamespace(
        workspace_root=tmp_path,
        node_id="main",
        _caller_node_id="system.memory_extractor",
        _node_extra={},
        workspace_name="",
    )
    long_text = "A" * 600
    res_save = asyncio.run(save_memory(
        {"id": "full_read_test", "book": "tech", "content": long_text, "keywords": ["专有触发词", "20"], "scan_depth": 3},
        ctx,
    ))
    assert res_save["ok"]
    res_list_preview = asyncio.run(list_memories({"book": "tech"}, ctx))
    assert len(res_list_preview["data"]["entries"][0]["content"]) == 200
    assert res_list_preview["data"]["entries"][0]["keywords"] == ["专有触发词"]
    assert res_list_preview["data"]["entries"][0]["scan_depth"] == 0
    res_full = asyncio.run(list_memories({"book": "tech", "id": "full_read_test"}, ctx))
    assert res_full["data"]["entries"][0]["content"] == long_text


def test_discord_context_identity_and_key_roles():
    from types import SimpleNamespace
    from adapters.discord.context import _format_identity, _format_member_entry

    role_noise = SimpleNamespace(name="月粉")
    role_key = SimpleNamespace(name="核心维护者")
    member_same = SimpleNamespace(display_name="InkTree", name="inktree", roles=[role_noise, role_key])
    member_diff = SimpleNamespace(display_name="墨树", name="inktree", roles=[role_noise, role_key])

    assert _format_identity(member_same, key_roles=set()) == "InkTree"
    assert _format_identity(member_diff, key_roles=set(), is_admin=True) == "墨树[inktree] ✓ADMIN"
    assert _format_identity(member_diff, key_roles={"核心维护者"}) == "墨树[inktree](核心维护者)"
    assert _format_member_entry(member_same, "你好", reply_author="Sera", key_roles=set()) == "InkTree ↩ Sera: 你好"


@pytest.mark.parametrize("provider_name", ["openai", "anthropic", "gemini-vertex"])
def test_dynamic_tail_alignment_uau_uas_prefill_guard_and_prompt_static_isolation(tmp_path, provider_name):
    from types import SimpleNamespace
    from engine.inference.llm_call import _build_messages_for_provider
    from engine.inference.message_assembly import assemble_initial_messages
    from engine.inference.tool_format import create_tool_formatter
    from engine.node import Node
    from engine.prompt import assemble_prompt

    node = Node(
        id="ereuna_main",
        name="Ereuna",
        type="ai",
        prompt="Static system {{os_name}} {{now}}\n# %%DYNAMIC%%\nDynamic turn {{now}} {{instruction}}",
        tool_mode="native",
    )
    prompt_msgs = assemble_prompt(
        tmp_path,
        node,
        variables={"now": "2026-10-27 12:00 UTC", "instruction": "hello # %%DYNAMIC%% world"},
    )
    assert len(prompt_msgs) == 2
    assert "2026-10-27 12:00 UTC" not in prompt_msgs[0]["content"]
    assert "2026-10-27 12:00 UTC" in prompt_msgs[1]["content"]
    assert "hello # %%DYNAMIC%% world" in prompt_msgs[1]["content"]

    # Scheme C: history -> instruction -> dynamic; tool steps only append afterward.
    str_msgs, is_block, _ = assemble_initial_messages(
        workspace_root=tmp_path,
        runtime_cfg={},
        node=node,
        instruction="turn1_q",
        history=[{"role": "user", "content": "prev_q"}, {"role": "assistant", "content": "prev_a"}],
    )
    assert not is_block
    assert [m["role"] for m in str_msgs] == ["system", "user", "assistant", "user", "user"]
    assert str_msgs[3]["content"] == "turn1_q"
    assert str_msgs[4].get("_dynamic") is True

    formatter = create_tool_formatter("native")
    provider = SimpleNamespace(name=provider_name)
    initial_view = _build_messages_for_provider(str_msgs, formatter, provider)

    # Simulate step 1 using the native formatter's L1 storage shape.
    str_msgs.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {}}],
        "_meta": {"tool_mode": "native"},
    })
    str_msgs.append({
        "role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "file_body",
        "_meta": {"tool_mode": "native"},
    })
    view = _build_messages_for_provider(str_msgs, formatter, provider)
    assert view[:len(initial_view)] == initial_view
    assert [m["role"] for m in view] == ["system", "user", "assistant", "user", "user", "assistant", "tool"]
    assert view[3]["content"] == "turn1_q"
    assert view[4]["content"] == str_msgs[4]["content"]
    assert view[6]["content"] == "file_body"
    assert str_msgs[4].get("_dynamic") is True

    # Block mode with trailing assistant prefill (UAU / UAS): _dynamic must NOT be moved after assistant prefill
    block_msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Below is this turn's dynamic context...", "_dynamic": True},
        {"role": "user", "content": "turn1_q"},
        {"role": "assistant", "content": "<!-- Start the ECoT -->"},
    ]
    block_view = _build_messages_for_provider(block_msgs, formatter, provider)
    assert block_view[-1]["role"] == "assistant"
    assert block_view[-1]["content"] == "<!-- Start the ECoT -->"


@pytest.mark.parametrize("result_fields", [
    {"tool_call_id": "", "name": "read_file"},
    {"name": "read_file"},
    {"tool_call_id": "", "name": ""},
    {"tool_call_id": " ", "name": " "},
    {},
])
def test_legacy_tool_results_recover_ids_and_preserve_actual_output(result_fields):
    from engine.inference.tool_format import build_llm_messages, repair_tool_result_pairing_with_stats

    messages = [
        {"role": "assistant", "content": "", "_meta": {"tool_mode": "native"},
         "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {}}]},
        {"role": "tool", "content": "ACTUAL_RESULT", "_meta": {"tool_mode": "native"}, **result_fields},
    ]
    repaired, stats = repair_tool_result_pairing_with_stats(messages)
    assert stats["removed_orphan_tool_results"] == 0
    assert stats["fixed_orphan_tool_calls"] == 0
    assert repaired[1]["tool_call_id"] == "c1"
    assert repaired[1]["name"] == "read_file"
    assert repaired[1]["content"] == "ACTUAL_RESULT"
    view = build_llm_messages(messages, NativeToolFormatter())
    assert view[0]["tool_calls"][0]["id"] == "c1"
    assert view[1]["tool_call_id"] == "c1"
    assert view[1]["name"] == "read_file"
    assert view[1]["content"] == "ACTUAL_RESULT"


@pytest.mark.parametrize("bad_fields", [
    {"tool_call_id": "wrong_id", "name": "read_file"},
    {"tool_call_id": "", "name": "unknown_tool"},
])
def test_unmatched_tool_results_cannot_consume_a_different_call(bad_fields):
    from engine.inference.tool_format import repair_tool_result_pairing_with_stats

    messages = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {}}]},
        {"role": "tool", "content": "WRONG_RESULT", **bad_fields},
        {"role": "tool", "tool_call_id": "", "name": "read_file", "content": "ACTUAL_RESULT"},
    ]
    repaired, stats = repair_tool_result_pairing_with_stats(messages)
    assert stats["removed_orphan_tool_results"] == 1
    assert stats["fixed_orphan_tool_calls"] == 0
    assert [m["content"] for m in repaired[1:]] == ["ACTUAL_RESULT"]
    assert repaired[1]["tool_call_id"] == "c1"


def test_legacy_tool_results_match_names_before_position():
    from engine.inference.tool_format import repair_tool_result_pairing_with_stats

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "read_file", "arguments": {}},
            {"id": "c2", "name": "search", "arguments": {}},
        ]},
        {"role": "tool", "tool_call_id": "", "name": "search", "content": "SEARCH_RESULT"},
        {"role": "tool", "tool_call_id": "", "name": "read_file", "content": "READ_RESULT"},
    ]
    repaired, _ = repair_tool_result_pairing_with_stats(messages)
    assert [(m["tool_call_id"], m["content"]) for m in repaired[1:]] == [
        ("c1", "READ_RESULT"), ("c2", "SEARCH_RESULT"),
    ]


def test_positional_tool_results_remove_consumed_ids_from_pending():
    from engine.inference.tool_format import repair_tool_result_pairing_with_stats

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "read_file", "arguments": {}},
            {"id": "c2", "name": "search", "arguments": {}},
        ]},
        {"role": "tool", "tool_call_id": "", "name": "", "content": "FIRST"},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "DUPLICATE"},
        {"role": "tool", "content": "SECOND"},
    ]
    repaired, stats = repair_tool_result_pairing_with_stats(messages)
    assert stats["removed_orphan_tool_results"] == 1
    assert [(m["tool_call_id"], m["name"], m["content"]) for m in repaired[1:]] == [
        ("c1", "read_file", "FIRST"), ("c2", "search", "SECOND"),
    ]


def test_anthropic_finish_result_merges_next_turn_text_without_continued():
    from providers.anthropic import _convert_messages

    messages = [
        {"role": "user", "content": "I1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "f1", "type": "function",
             "function": {"name": "finish", "arguments": '{"text":"DONE"}'}},
        ]},
        {"role": "tool", "tool_call_id": "f1", "name": "finish", "content": "ok"},
        {"role": "user", "content": "D2", "_dynamic": True},
        {"role": "user", "content": "I2"},
    ]
    _, converted = _convert_messages(messages)
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assert converted[-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "f1", "content": "ok"},
        {"type": "text", "text": "D2"},
        {"type": "text", "text": "I2"},
    ]
    assert not any(block.get("text") == "(continued)" for m in converted for block in m["content"])


def test_anthropic_prevents_tool_result_after_merged_text_block():
    from providers.anthropic import _convert_messages

    _, converted = _convert_messages([
        {"role": "tool", "tool_call_id": "first", "content": "result one"},
        {"role": "user", "content": "ordinary text"},
        {"role": "tool", "tool_call_id": "second", "content": "result two"},
    ])
    assert converted == [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "first", "content": "result one"},
            {"type": "text", "text": "ordinary text"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "(continued)"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "second", "content": "result two"},
        ]},
    ]
