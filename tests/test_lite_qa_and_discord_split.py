from __future__ import annotations

import asyncio
import runpy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.discord.callbacks import DiscordCallbacks
from adapters.discord.messaging import _send_split_text
from adapters.message_text import extract_tts_segments, split_long_text, split_message_text
from engine.conversation_store import ConversationStore, Message
from engine.inference.resume_builder import apply_resume_messages
from engine.node import load_node
from engine.protocol import ACTION_ASK, ACTION_FINISH, TaskAction
from supervisor.types import Task, TaskKind, TaskStatus


def test_task_and_protocol_removed_output_chain_and_reject(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    task = Task(
        task_id="t-1",
        session_id="s-1",
        kind=TaskKind.node,
        node_id="main",
        created_at=now,
        updated_at=now,
    )
    assert not hasattr(task, "output_chain_entry_task_id")
    assert not hasattr(task, "output_chain_path")

    act = TaskAction(action=ACTION_FINISH, node_id="main", result={"text": "ok"})
    assert act.to_dict()["result"] == {"text": "ok"}
    ask_act = TaskAction(action=ACTION_ASK, node_id="main", result={"text": "need info"})
    assert ask_act.to_dict()["result"] == {"text": "need info"}

    nodes_dir = tmp_path / "config" / "nodes"
    nodes_dir.mkdir(parents=True)
    (nodes_dir / "script_legacy.yaml").write_text(
        "id: script_legacy\ntype: script\nscript: 'return finish(text)'\n",
        encoding="utf-8",
    )
    assert load_node(tmp_path, "script_legacy") is None

    (nodes_dir / "ai_node.yaml").write_text(
        "id: ai_node\ntype: ai\nname: AI Node\nprompt: hello\n",
        encoding="utf-8",
    )
    loaded = load_node(tmp_path, "ai_node")
    assert loaded is not None
    assert not hasattr(loaded, "output")
    assert not hasattr(loaded, "script")


def test_legacy_output_rejected_messages_in_jsonl_remain_readable(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.append(
        "sid-1",
        Message(
            id="m-1",
            role="user",
            content="reject by ereuna_qa: some old reason",
            message_type="output_rejected",
            created_at=datetime.now(timezone.utc).isoformat(),
        ),
    )
    loaded = store.load("sid-1")
    assert len(loaded) == 1
    assert loaded[0].role == "user"
    assert loaded[0].message_type == "output_rejected"


def test_apply_resume_messages_handles_child_result_and_ask() -> None:
    messages: list[dict] = []
    asyncio.run(
        apply_resume_messages(
            messages=messages,
            resume_data={
                "type": "child_result",
                "child_node_id": "coder",
                "result": {"text": "done"},
            },
            node=SimpleNamespace(id="main"),
            rctx=SimpleNamespace(parent_session_id="", session_id="s-1"),
        )
    )
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "done" in messages[0]["content"]


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
    cb = DiscordCallbacks(rt)
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
