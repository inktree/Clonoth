"""Channel context is consumed by delivered replies, not accepted inbounds."""
import asyncio
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.discord import callbacks, context
from adapters.discord.messaging import _record_bot_reply, _send_split_text
from adapters.message_text import extract_tts_segments, split_long_text, split_message_text
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


@pytest.mark.parametrize("outcome", ["text", "attachment", "empty", "failure", "voice", "voice_failure"])
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
    monkeypatch.setattr(callbacks, "_atts_to_discord_files", lambda *args: [])
    if outcome in {"voice", "voice_failure"}:
        monkeypatch.setattr(callbacks, "prepare_discord_tts_reply", lambda *args: ("", [{"text": "Spoken"}]))
        monkeypatch.setattr(callbacks, "_send_discord_tts_segments", AsyncMock(return_value=outcome != "voice_failure"))
    notice = AsyncMock()
    monkeypatch.setattr(cb, "send_response_notice", notice)
    router = EventRouter(None, state, cb, SimpleNamespace(entry_node_id="main"))
    event = Event(
        seq=1, event_id="reply", ts="", run_id="run", session_id="s",
        component="supervisor", type="outbound_message",
        payload={
            "source_inbound_seq": 100, "text": "" if outcome in {"empty", "attachment"} else "Answer",
            "attachments": [{"path": "file"}] if outcome == "attachment" else [],
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
        if outcome != "empty":
            notice.assert_awaited_once()
        else:
            notice.assert_not_awaited()


def test_extract_tts_segments_preserves_code_indentation_and_strips_split_markers() -> None:
    raw = (
        "下面是代码：\n"
        "```python\n"
        "def foo():\n"
        "    if True:\n"
        "        return 1  # two spaces before comment\n"
        "```\n"
        "<tts backend=\"ryza_onnx\" preset=\"normal_cn\">第一句[SPLIT]第二句[LEFT]第三句</tts>"
    )
    display_text, segments = extract_tts_segments(raw)
    assert "    if True:\n        return 1  # two spaces before comment" in display_text
    assert len(segments) == 1
    assert segments[0]["text"] == "第一句 第二句 第三句"
    assert segments[0]["backend"] == "ryza_onnx"
    assert segments[0]["preset"] == "normal_cn"


@pytest.mark.parametrize("limit", [1900, 4300])
def test_split_long_text_closes_and_reopens_fenced_code_and_preserves_indent(limit) -> None:
    code_lines = ["```python", "def compute():"]
    for i in range(240):
        code_lines.append(f"    value_{i} = {i} + 1000000000")
    code_lines.append("    return value_0")
    code_lines.append("```")
    long_code = "\n".join(code_lines)
    assert len(long_code) > 2000

    chunks = split_long_text(long_code, limit=limit)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= limit
        assert chunk.startswith("```python\n")
        assert chunk.endswith("```")
    # Check that indentation on the first line after reopening fence is preserved
    second_body_first_line = chunks[1].splitlines()[1]
    assert second_body_first_line.startswith("    ")
    body_lines = [
        line for chunk in chunks for line in chunk.splitlines()
        if line and not line.startswith("```")
    ]
    assert body_lines == code_lines[1:-1]


def test_send_split_text_combines_explicit_split_left_and_long_chunks_without_truncating() -> None:
    rt = SimpleNamespace(split_signal="[SPLIT]")
    sent_contents: list[str] = []

    class DummyChannel:
        def typing(self):
            class _Ctx:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            return _Ctx()

        async def send(self, content: str = "", files=None, suppress_embeds: bool = True):
            sent_contents.append(content)
            return SimpleNamespace(id=len(sent_contents))

    # Segment 1 has code containing [SPLIT] and [LEFT] which must NOT be split
    seg1 = "示例：\n```text\nkeep [SPLIT] and [LEFT] inside fence\n```"
    # Segment 2 is > 2200 chars and separated by [LEFT]
    seg2 = ("这是一段很长的中文测试句子。" * 170).strip()
    assert len(seg2) > 2200
    full_text = f"{seg1}[LEFT]{seg2}"

    channel = DummyChannel()
    asyncio.run(_send_split_text(rt, channel, full_text))

    assert len(sent_contents) == 3
    assert "keep [SPLIT] and [LEFT] inside fence" in sent_contents[0]
    assert "..." not in sent_contents[1]
    assert "..." not in sent_contents[2]
    assert "".join(sent_contents[1:]) == seg2


def test_send_intermediate_reply_delivers_tts_only_message(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = SimpleNamespace(
        workspace=Path("."),
        react_pattern=MagicMock(findall=lambda s: [], sub=lambda repl, s: s),
        split_signal="[SPLIT]",
        discord_voice_reply=True,
        discord_voice_reply_on_voice_input=True,
    )
    cb = callbacks.DiscordCallbacks(rt)
    tts_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("adapters.discord.callbacks._send_discord_tts_segments", tts_mock)

    dummy_channel = SimpleNamespace(send=AsyncMock())
    dummy_msg = SimpleNamespace(channel=dummy_channel, add_reaction=AsyncMock())
    trigger = SimpleNamespace(
        conversation_key="discord:123",
        is_dm=True,
        platform_data={"message": dummy_msg, "channel_id": 123, "_discord_input_modality": "text"},
    )

    asyncio.run(
        cb.send_intermediate_reply(
            trigger,
            '<tts backend="ryza_onnx" preset="normal_cn">纯语音中间回复</tts>',
            attachments=[],
        )
    )
    tts_mock.assert_awaited_once()
    args = tts_mock.await_args.args
    assert args[3] == [{"text": "纯语音中间回复", "backend": "ryza_onnx", "preset": "normal_cn"}]


@pytest.mark.parametrize(
    "markup, expected",
    [
        ('<TTS backend=\'MIMO\' voice=Alice instruction="Speak gently">one[SPLIT]two[LEFT]three</TTS>',
         {"text": "one two three", "backend": "mimo", "voice": "Alice", "instruction": "Speak gently"}),
        ('[[tts]]one[LEFT]two[[/tts]]', {"text": "one two"}),
    ],
)
def test_shared_tts_markup_attributes_and_legacy_segments(markup, expected):
    assert extract_tts_segments("before\n" + markup + "\nafter") == (
        "before\n\nafter", [expected],
    )


def test_onebot_text_pipeline_keeps_long_tail_and_literal_fenced_code():
    # Load the dependency-free utility without bootstrapping the NoneBot plugin.
    process_emojis = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "emoji_handler.py"),
    )["process_emojis"]
    prefix = "正文。" * 3000
    code = "\n```python\n    value = '[QQ_EMOJI:test] [at:123] [SPLIT] [LEFT]'\n\n    return value\n```"
    chunks = split_message_text(prefix + code + "[LEFT]尾部END", limit=4300, split_signal="[SPLIT]")
    assert all(len(chunk) <= 4300 for chunk in chunks)
    assert chunks[-1] == "尾部END"

    async def render():
        return [segment for chunk in chunks for segment in await process_emojis(chunk, None, [])]

    rendered = asyncio.run(render())
    assert all(segment["type"] == "text" for segment in rendered)
    assert "".join(segment["content"] for segment in rendered) == prefix + code + "尾部END"
