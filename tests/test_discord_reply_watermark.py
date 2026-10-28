"""Channel context is consumed by delivered replies, not accepted inbounds."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.discord import callbacks, context
from adapters.discord.messaging import _record_bot_reply
from clonoth_sdk import SessionState, TriggerInfo


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


@pytest.mark.parametrize("outcome", ["text", "attachment", "failure", "empty", "voice"])
def test_only_successful_final_delivery_commits_snapshot(monkeypatch, outcome):
    rt = runtime()
    sender = AsyncMock(return_value=[SimpleNamespace(id=123)])
    if outcome == "failure":
        sender.side_effect = RuntimeError("Discord unavailable")
    elif outcome == "empty":
        sender.return_value = []
    monkeypatch.setattr(callbacks, "_send_split_text", sender)
    monkeypatch.setattr(callbacks, "_atts_to_discord_files", lambda *args: [])
    monkeypatch.setattr(callbacks, "_extract_reactions", lambda rt, text: (text, []))
    voice_sender = AsyncMock(return_value=True)
    monkeypatch.setattr(callbacks, "_send_discord_tts_segments", voice_sender)
    monkeypatch.setattr(callbacks, "prepare_discord_tts_reply", lambda *args: ("", [{"text": "Spoken"}]) if outcome == "voice" else (args[1], []))
    trigger = TriggerInfo(
        inbound_seq=100, conversation_key="discord:42", session_id="s", is_dm=False,
        platform_data={"message": SimpleNamespace(channel=object()), "channel_id": 42, "_context_watermark": 7},
    )
    cb = callbacks.DiscordCallbacks(rt)
    attachments = [{"path": "file"}] if outcome == "attachment" else []
    asyncio.run(cb.send_reply(trigger, "" if attachments else "Answer", attachments))
    assert rt.session_state.last_ctx_seq.get(42, -1) == (-1 if outcome in {"failure", "empty"} else 7)
    if outcome == "voice":
        voice_sender.assert_awaited_once()
