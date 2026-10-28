"""Channel context is consumed by delivered replies, not accepted inbounds."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.discord import callbacks, context
from adapters.discord.messaging import _record_bot_reply
from clonoth_sdk import SessionState, TriggerInfo
from clonoth_sdk.event_router import EventRouter
from clonoth_sdk.state import MainTaskState
from clonoth_sdk.types import Event


def runtime():
    return SimpleNamespace(
        session_state=SessionState(), channel_history={}, history_seq_counter=0,
        history_max_len=100, key_roles=set(), superusers=set(), restart_signal="<restart>",
        discord_voice_reply=False, discord_voice_reply_on_voice_input=False,
    )


def test_parallel_question_remains_until_reply_then_preserves_later_messages(monkeypatch):
    rt = runtime()
    monkeypatch.setattr(context, "_format_identity", lambda *a, **kw: "User")
    member = SimpleNamespace(id=1)
    context._push_history(rt, 42, "A asks first")
    snapshot_a = rt.history_seq_counter - 1
    # 尚未投递回复，频道历史水位不得推进。
    assert rt.session_state.last_ctx_seq.get(42, -1) == -1
    context._push_history(rt, 42, "B asks concurrently")
    snapshot_b = rt.history_seq_counter - 1
    before = context._build_context_text(rt, 42, member, "B", exclude_last=True)
    assert "A asks first" in before
    assert "【群聊上下文记录】" in before

    _record_bot_reply(rt, 42, "Answer A", context_watermark=snapshot_a)
    assert rt.session_state.last_ctx_seq[42] == snapshot_a
    after = context._build_context_text(rt, 42, member, "C")
    assert "A asks first" not in after
    assert "B asks concurrently" in after
    assert "Answer A" not in after

    _record_bot_reply(rt, 42, "Answer B", context_watermark=snapshot_b)
    _record_bot_reply(rt, 42, "Late A", context_watermark=snapshot_a)
    assert rt.session_state.get_high_watermark(42) == snapshot_b


def test_dm_context_keeps_private_header(monkeypatch):
    rt = runtime()
    monkeypatch.setattr(context, "_format_identity", lambda *a, **kw: "User")
    context._push_history(rt, 42, "Private question")
    result = context._build_context_text(rt, 42, SimpleNamespace(id=1), "Next", is_dm=True)
    assert "【私聊上下文记录】" in result
    assert "【群聊上下文记录】" not in result


@pytest.mark.parametrize("outcome", ["text", "attachment", "failure", "empty", "voice", "voice_failure"])
def test_only_successful_final_delivery_commits_snapshot(monkeypatch, outcome):
    rt = runtime()
    sender = AsyncMock(return_value=[SimpleNamespace(id=123)])
    if outcome == "failure":
        sender.side_effect = RuntimeError("Discord unavailable")
    monkeypatch.setattr(callbacks, "_send_split_text", sender)
    monkeypatch.setattr(callbacks, "_atts_to_discord_files", lambda *args: [])
    monkeypatch.setattr(callbacks, "_extract_reactions", lambda rt, text: (text, []))
    voice_sender = AsyncMock(return_value=outcome != "voice_failure")
    monkeypatch.setattr(callbacks, "_send_discord_tts_segments", voice_sender)
    monkeypatch.setattr(callbacks, "prepare_discord_tts_reply", lambda *args: ("", [{"text": "Spoken"}]) if outcome in {"voice", "voice_failure"} else (args[1], []))
    trigger = TriggerInfo(
        inbound_seq=100, conversation_key="discord:42", session_id="s", is_dm=False,
        platform_data={"message": SimpleNamespace(channel=object()), "channel_id": 42, "_context_watermark": 7},
    )
    cb = callbacks.DiscordCallbacks(rt)
    attachments = [{"path": "file"}] if outcome == "attachment" else []
    text = "" if attachments or outcome == "empty" else "Answer"
    if outcome in {"failure", "voice_failure"}:
        with pytest.raises(RuntimeError, match="Discord unavailable|discord tts segments delivery failed"):
            asyncio.run(cb.send_reply(trigger, text, attachments))
    else:
        asyncio.run(cb.send_reply(trigger, text, attachments))
    assert rt.session_state.last_ctx_seq.get(42, -1) == (-1 if outcome in {"failure", "empty", "voice_failure"} else 7)
    if outcome in {"voice", "voice_failure"}:
        voice_sender.assert_awaited_once()


@pytest.mark.parametrize("outcome", ["text", "empty", "failure", "voice_failure"])
def test_router_consumes_state_and_settles_status_only_after_reply(monkeypatch, outcome):
    rt = runtime()
    state = rt.session_state
    status_msg = SimpleNamespace(delete=AsyncMock())
    log_msg = SimpleNamespace(edit=AsyncMock())
    trigger = TriggerInfo(
        inbound_seq=100, conversation_key="discord:42", session_id="s", is_dm=False,
        platform_data={
            "message": SimpleNamespace(channel=object()), "channel_id": 42,
            "status_msg": status_msg, "_context_watermark": 7,
        },
    )
    main_state = MainTaskState(platform_data={"log_msg": log_msg})
    state.register_trigger(trigger)
    state.main_task_states[100] = main_state
    cb = callbacks.DiscordCallbacks(rt)
    cb._dot_states[100] = {}
    cb._last_edit_time[100] = 1.0

    async def send(*args, **kwargs):
        assert state.get_trigger(100) is trigger
        assert state.get_main_state(100) is main_state
        status_msg.delete.assert_not_awaited()
        log_msg.edit.assert_not_awaited()
        if outcome == "failure":
            raise RuntimeError("Discord unavailable")
        return [SimpleNamespace(id=123)]

    monkeypatch.setattr(callbacks, "_send_split_text", send)
    monkeypatch.setattr(callbacks, "_extract_reactions", lambda rt, text: (text, []))
    if outcome == "voice_failure":
        monkeypatch.setattr(callbacks, "prepare_discord_tts_reply", lambda *args: ("", [{"text": "Spoken"}]))
        monkeypatch.setattr(callbacks, "_send_discord_tts_segments", AsyncMock(return_value=False))
    notice = AsyncMock()
    monkeypatch.setattr(cb, "send_response_notice", notice)
    router = EventRouter(None, state, cb, SimpleNamespace(entry_node_id="main"))
    event = Event(
        seq=1, event_id="reply", ts="", run_id="run", session_id="s",
        component="supervisor", type="outbound_message",
        payload={
            "source_inbound_seq": 100, "text": "" if outcome == "empty" else "Answer",
            "response_info": {"notices": [{"text": "Delivered"}]},
        },
    )
    asyncio.run(router._dispatch(event))

    if outcome in {"failure", "voice_failure"}:
        assert state.get_trigger(100) is trigger
        assert state.get_main_state(100) is main_state
        status_msg.delete.assert_not_awaited()
        log_msg.edit.assert_not_awaited()
        notice.assert_not_awaited()
        assert 100 in cb._dot_states
        assert 100 in cb._last_edit_time
        assert state.last_ctx_seq.get(42, -1) == -1
    else:
        assert state.get_trigger(100) is None
        assert state.get_main_state(100) is None
        status_msg.delete.assert_awaited_once()
        log_msg.edit.assert_awaited_once()
        assert 100 not in cb._dot_states
        assert 100 not in cb._last_edit_time
        assert state.last_ctx_seq.get(42, -1) == (-1 if outcome == "empty" else 7)
        if outcome == "text":
            notice.assert_awaited_once()
        else:
            notice.assert_not_awaited()
