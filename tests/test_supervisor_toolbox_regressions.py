"""Regression coverage for scheduler isolation and tool metadata/upserts."""
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from supervisor import scheduler
from supervisor.task_router import TaskRouterMixin
from toolbox.builtins import schedules
from toolbox.builtins.apply_diff import _find_and_replace, apply_diff
from toolbox.builtins.read_file import _read_single_file_sync
from toolbox.builtins.search_in_files import _do_replace_sync
from toolbox.context import ToolContext
from engine.tool_step import result_to_raw
from toolbox.registry import ToolRegistry


def test_unique_diff_uses_actual_line_not_stale_hint():
    result, error, candidates, line = _find_and_replace("a\nb\nunique\n", "unique", "changed", 99)
    assert result == "a\nb\nchanged\n"
    assert error is None and candidates is None and line == 3


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_script_timeout_kills_process_tree_and_releases_running_slot(tmp_path, monkeypatch, platform):
    proc = Mock(pid=123)
    proc.communicate.side_effect = [subprocess.TimeoutExpired("command", 1), ("", "")]
    popen = Mock(return_value=proc)
    taskkill = Mock()
    killpg = Mock()
    monkeypatch.setattr(scheduler.subprocess, "Popen", popen)
    monkeypatch.setattr(scheduler.subprocess, "run", taskkill)
    monkeypatch.setattr(scheduler, "os", SimpleNamespace(name=platform, killpg=killpg))
    monkeypatch.setattr(scheduler, "signal", SimpleNamespace(SIGKILL=9))
    runner = scheduler.SchedulerThread(state=None, workspace_root=tmp_path)
    runner._inject_inbound = Mock()
    runner._fire_script({"command": "command", "timeout": 1}, "script")
    assert popen.call_args.kwargs["start_new_session"] is True
    if platform == "nt":
        assert taskkill.call_args.args[0] == ["taskkill", "/PID", "123", "/T", "/F"]
        killpg.assert_not_called()
    else:
        killpg.assert_called_once_with(123, 9)
        taskkill.assert_not_called()
    assert not runner._running_scripts
    runner._inject_inbound.assert_not_called()


def test_script_does_not_block_message_schedule(tmp_path, monkeypatch):
    state = SimpleNamespace(
        _lock=threading.Lock(), hook_registry=SimpleNamespace(fire=Mock()),
        _build_supervisor_hook_ctx=lambda **kw: kw,
    )
    runner = scheduler.SchedulerThread(state=state, workspace_root=tmp_path)
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    worker_threads = []

    def blocking_script(*args):
        worker_threads.append(threading.current_thread())
        started.set()
        release.wait(3)
        done.set()

    monkeypatch.setattr(scheduler, "load_schedules", lambda root: [
        {"id": "slow", "type": "script", "cron": "* * * * *"},
        {"id": "message", "cron": "* * * * *", "text": "hello"},
    ])
    runner._fire_script = blocking_script
    runner._fire_message = Mock()
    try:
        runner._tick()
        assert started.wait(1)
        runner._fire_message.assert_called_once()
        assert worker_threads[0].daemon
        assert worker_threads[0] is not threading.current_thread()
    finally:
        release.set()
        assert done.wait(1)


def test_schedule_upsert_inherits_fields_and_preserves_other_approval_updates(tmp_path, monkeypatch):
    scheduler.save_schedules(tmp_path, [{
        "id": "job", "type": "script", "cron": "0 * * * *", "command": "old",
        "conversation_key": "discord:42", "timeout": 90, "silent": False,
        "enabled": False, "once": True,
    }])

    async def approve(ctx, op, args):
        current = scheduler.load_schedules(tmp_path)
        current.append({"id": "parallel", "text": "keep"})
        scheduler.save_schedules(tmp_path, current)
        return {}, None

    monkeypatch.setattr(schedules, "request_guard", approve)
    ctx = SimpleNamespace(workspace_root=tmp_path, conversation_key="wrong")
    res = asyncio.run(schedules.create_schedule({"id": "job", "command": "new"}, ctx))
    assert res["ok"]
    saved = scheduler.load_schedules(tmp_path)
    assert saved[0] == {
        "id": "job", "type": "script", "cron": "0 * * * *", "command": "new",
        "conversation_key": "discord:42", "timeout": 90, "silent": False,
        "enabled": False, "once": True,
    }
    assert saved[1]["id"] == "parallel"


@pytest.mark.parametrize("has_carrier", [True, False])
def test_async_result_reuses_carrier_or_keeps_root_owner(has_carrier):
    carrier = SimpleNamespace(task_id="resumed-branch") if has_carrier else None
    router = SimpleNamespace(
        _lock=threading.Lock(),
        sessions={"s": SimpleNamespace(conversation_key="discord:42", channel="discord")},
        _find_dispatch_callback_carrier_locked=Mock(return_value=carrier),
        _is_branchless_running_task_locked=Mock(return_value=None),
        _append_preempt_to_task_locked=Mock(),
        eventlog=SimpleNamespace(append=Mock(return_value={"seq": 9})),
        _apply_inbound_message=Mock(), _advance_inbound_cursor=Mock(),
        _create_entry_task_for_inbound_locked=Mock(),
    )
    result = TaskRouterMixin.inject_async_result(router, "s", "done", ["file.txt"], "original")
    router._find_dispatch_callback_carrier_locked.assert_called_once_with("original", "s")
    if has_carrier:
        assert result == {"ok": True, "strategy": "preempt", "task_id": "resumed-branch"}
        args = router._append_preempt_to_task_locked.call_args.args
        assert args[0] is carrier
        assert '<dispatch_result kind="async_tool">' in args[1]
        assert args[2] == [{"path": "file.txt"}]
        router.eventlog.append.assert_not_called()
    else:
        assert result["strategy"] == "inbound"
        payload = router._create_entry_task_for_inbound_locked.call_args.kwargs["payload"]
        assert payload["_dispatch_owner_root_task_id"] == "original"
        assert payload["message_type"] == "async_tool_result"
        assert payload["attachments"] == [{"path": "file.txt"}]


def test_registry_pure_flags_disabled_todos_and_retired_cancel(tmp_path):
    reg = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    definitions = {spec["name"]: spec for spec in reg.list_specs()}
    for name in {"list_dir", "read_file", "get_context_window"}:
        assert definitions[name]["pure"] is True
    assert reg.get_spec("list_schedules")["name"] == "manage_schedule"
    assert "list_schedules" not in definitions
    assert definitions["write_file"]["pure"] is False
    assert not ({"cancel_active_tasks", "update_todo_list", "get_todo_list"} & definitions.keys())


@pytest.mark.parametrize("old_name,target,action", [
    ("read_image", "read_media", None), ("read_video", "read_media", None),
    ("list_schedules", "manage_schedule", "list"), ("create_schedule", "manage_schedule", "upsert"),
    ("delete_schedule", "manage_schedule", "delete"), ("list_mcp_clients", "manage_mcp_client", "list"),
    ("create_or_update_mcp_client", "manage_mcp_client", "upsert"), ("delete_mcp_client", "manage_mcp_client", "delete"),
    ("list_skills", "manage_skill", "list"), ("create_or_update_skill", "manage_skill", "upsert"),
    ("delete_skill", "manage_skill", "delete"), ("create_or_update_tool", "manage_tool", "upsert"),
    ("reload_tools", "manage_tool", "reload"),
])
def test_legacy_alias_resolves_spec_authorization_and_execution(tmp_path, monkeypatch, old_name, target, action):
    from toolbox import _common

    reg = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    calls = []
    guards = []

    async def tool(args, ctx):
        calls.append(args)
        return {"ok": True, "data": {"result": "done"}}

    async def guard(ctx, op, params):
        guards.append(params)
        return {}, None

    reg.register_builtin_tool(target, "test target", {"type": "object"}, tool)
    monkeypatch.setattr(_common, "request_guard", guard)
    original_args = {"path": "sample.png"}
    expected = {**original_args, **({"action": action} if action else {})}
    assert reg._resolve_alias(old_name, original_args) == (target, expected)
    assert original_args == {"path": "sample.png"}
    assert reg.get_spec(old_name)["name"] == target
    assert asyncio.run(reg.authorize(name=old_name, arguments=original_args, ctx=None)) is None
    assert guards[-1] == {"tool_name": target, "arguments": expected}
    result = asyncio.run(reg.execute(name=old_name, arguments=original_args, ctx=None, _authorized=True))
    assert result["ok"] and calls == [expected]
    assert reg._resolve_alias(old_name, {"action": "custom"})[1]["action"] == "custom"


@pytest.mark.parametrize("format_", ["dict", "scalar", "text"])
def test_script_output_parses_large_json_before_text_truncation(tmp_path, monkeypatch, format_):
    import json
    from unittest.mock import AsyncMock
    from toolbox.registry import _make_script_tool

    large = "value" * 5000
    structured = {"ok": True, "data": {"result": large, "nested": [1, {"text": large}]}}
    output = json.dumps(structured) if format_ == "dict" else json.dumps(large) if format_ == "scalar" else large
    proc = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(output.encode(), b"")))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    tool = _make_script_tool(script_path=tmp_path / "output.py", timeout_sec=5)
    result = asyncio.run(tool({}, SimpleNamespace(workspace_root=tmp_path)))
    if format_ == "dict":
        assert result == structured
    else:
        assert result["data"]["result"] == large[:16000]


def test_reset_cascades_tasks_approvals_async_tools_and_keeps_call_chain(tmp_path):
    from datetime import datetime, timedelta, timezone
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine
    from supervisor.state import SupervisorState
    from supervisor.types import Approval, ApprovalStatus, Task, TaskKind, TaskStatus

    state = SupervisorState(
        workspace_root=tmp_path, eventlog=EventLog(tmp_path / "events.jsonl", run_id="r"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )
    root = state.get_or_create_session(channel="test", conversation_key="test:reset")
    caller_branch, _ = state._create_entry_branch_locked(root, 1)
    sibling, _ = state._create_entry_branch_locked(root, 2)
    dispatch = state.get_or_create_session(channel="test", conversation_key="agent:worker:test:reset")
    nested, _ = state._create_entry_branch_locked(dispatch, 3)
    # Persisted-only nested sessions must survive missing in-memory parent indexes.
    state.parent_children.pop(dispatch, None)
    state.parent_entry_branches.pop(dispatch, None)
    state.entry_branch_parents.pop(nested, None)
    now = datetime.now(timezone.utc)

    def task(tid, sid, status=TaskStatus.pending, caller=None, lease=None):
        t = Task(task_id=tid, session_id=sid, kind=TaskKind.node, node_id="worker", status=status,
                 caller_task_id=caller, created_at=now, updated_at=now, lease_expires_at=lease)
        state.tasks[tid] = t
        return t

    parent = task("parent", root, TaskStatus.suspended)
    current = task("current", caller_branch, TaskStatus.running, "parent", now + timedelta(minutes=1))
    pending = task("pending", root)
    suspended = task("suspended", sibling, TaskStatus.suspended)
    leased = task("leased", dispatch, TaskStatus.running, lease=now + timedelta(minutes=1))
    expired = task("expired", dispatch, TaskStatus.running, lease=now - timedelta(minutes=1))
    nested_task = task("nested", nested)
    descendant = task("descendant", "detached", caller="nested")
    grandchild = task("grandchild", "another-detached", caller="descendant")
    unrelated = task("unrelated", "unrelated-session")

    for tid, sid in [("pending", root), ("grandchild", "another-detached"), ("current", caller_branch), ("parent", root), ("unrelated", "unrelated-session")]:
        state.approvals[tid] = Approval(
            approval_id=tid, session_id=sid, operation="write_file", task_id=tid,
            status=ApprovalStatus.pending, fingerprint=tid, requested_at=now,
        )
    for tid in ["pending", "grandchild", "current", "unrelated"]:
        state._async_tools[tid] = {"task_id": tid, "session_id": state.tasks[tid].session_id,
                                   "status": "running", "worker_id": "worker"}
    # Create real history files to validate physical cleanup and caller retention.
    conversation_dir = tmp_path / "data" / "conversations"
    for sid in [root, caller_branch, sibling, dispatch, nested]:
        (conversation_dir / f"{sid}.jsonl").write_text("", encoding="utf-8")

    result = state.reset_conversation(conversation_key="test:reset", exclude_session_id=caller_branch, exclude_task_id="current")
    assert result["ok"] and result["cancelled_tasks"] == 7
    assert result["cleared_children"] == 3
    assert not parent.cancel_requested and not current.cancel_requested and not unrelated.cancel_requested
    for t in [pending, suspended, expired, nested_task, descendant, grandchild]:
        assert t.status == TaskStatus.cancelled
        assert t.input["_branch_finalized"] and t.input["_branch_skipped_merge"]
    assert leased.cancel_requested and leased.status == TaskStatus.running
    assert leased.input["_branch_skipped_merge"]
    for tid in ["pending", "grandchild"]:
        assert state.approvals[tid].status == ApprovalStatus.denied
        assert state.approvals[tid].decision == "deny"
        assert state.approvals[tid].comment == "conversation reset"
    for tid in ["parent", "current", "unrelated"]:
        assert state.approvals[tid].status == ApprovalStatus.pending
    assert set(state.pull_pending_cancels("worker")) == {"pending", "grandchild"}
    assert caller_branch in state._skip_merge_sessions
    assert (conversation_dir / f"{caller_branch}.jsonl").exists()
    for sid in [root, sibling, dispatch, nested]:
        assert not (conversation_dir / f"{sid}.jsonl").exists()
    assert "agent:worker:test:reset" not in state.conversation_map


def test_manage_skill_list_is_pure_and_legacy_delete_deny_keeps_manager_visible():
    from engine.inference.ai_step import _is_pure_tool_call
    from engine.inference.pseudo_tools import _filter_tool_specs
    from engine.node import Node, ToolAccess

    spec = {"name": "manage_skill", "input_schema": {"type": "object"}}
    assert _is_pure_tool_call("manage_skill", {"action": "list"}, spec)
    assert not _is_pure_tool_call("manage_skill", {"action": "delete", "name": "sample"}, spec)
    node = Node(id="agent", type="ai", tool_access=ToolAccess(mode="all", deny=["delete_skill"]))
    assert _filter_tool_specs(node, [spec]) == [spec]


@pytest.mark.parametrize("key", ["path", "image_path", "video_path", "paths"])
def test_read_media_legacy_paths_and_concurrent_mixed_inputs(tmp_path, monkeypatch, key):
    import base64
    from io import BytesIO
    import json
    from threading import Barrier
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    spec = spec_from_file_location("read_media_under_test", Path(__file__).resolve().parents[1] / "tools" / "read_media.py")
    media = module_from_spec(spec)
    spec.loader.exec_module(media)

    image, video = tmp_path / "sample.png", tmp_path / "sample.mp4"
    image.write_bytes(b"image bytes")
    video.write_bytes(b"video bytes")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://api.example.test/v1")
    captured = []

    def respond(request, timeout):
        captured.append(json.loads(request.data))
        return BytesIO(json.dumps({"candidates": [{"content": {"parts": [
            {"text": "private reasoning", "thought": True}, {"text": "media description"},
        ]}}]}).encode())

    monkeypatch.setattr(media.urllib_request, "urlopen", respond)
    if key == "paths":
        barrier = Barrier(2)
        prepare = media._prepare_media

        def concurrent_prepare(path, root):
            barrier.wait(timeout=2)
            return prepare(path, root)

        monkeypatch.setattr(media, "_prepare_media", concurrent_prepare)
        args = {"paths": [str(image), str(video)], "prompt": "OCR"}
    else:
        args = {key: str(video if key == "video_path" else image), "focus": "OCR"}
    result = media.read_media(args, tmp_path)
    assert result["data"]["result"] == "media description"
    parts = captured[0]["contents"][0]["parts"]
    assert "OCR" in parts[-1]["text"]
    assert len(parts) == (3 if key == "paths" else 2)
    assert base64.b64decode(parts[0]["inlineData"]["data"]) == (b"video bytes" if key == "video_path" else b"image bytes")
    if key == "paths":
        assert parts[1]["inlineData"]["mimeType"] == "video/mp4"


def test_manage_schedule_and_tool_wrappers_preserve_crud_and_metadata(tmp_path, monkeypatch):
    from toolbox import _common
    from toolbox.builtins import manage_schedule, manage_tool
    from toolbox.context import ToolContext

    async def guard(*args):
        return {}, None

    monkeypatch.setattr(_common, "request_guard", guard)
    monkeypatch.setattr(schedules, "request_guard", guard)
    # write_file imports the guard at module scope.
    import importlib
    monkeypatch.setattr(importlib.import_module("toolbox.builtins.write_file"), "request_guard", guard)
    reg = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    ctx = ToolContext(workspace_root=tmp_path, registry=reg, supervisor_url="test", session_id="s", run_id="r", worker_id="w", http=None)
    upsert = asyncio.run(manage_schedule({"action": "upsert", "id": "job", "cron": "* * * * *", "text": "hello"}, ctx))
    assert upsert["ok"]
    assert asyncio.run(manage_schedule({"action": "list"}, ctx))["ok"]
    assert asyncio.run(manage_schedule({"action": "delete", "id": "job"}, ctx))["ok"]
    assert scheduler.load_schedules(tmp_path) == []
    result = asyncio.run(manage_tool({"action": "upsert", "name": "example", "script": "output({'ok': True})", "pure": True, "async_mode": True}, ctx))
    assert result["ok"]
    assert reg.get_spec("example")["pure"] is True
    assert reg.get_spec("example")["async_mode"] is True
    assert asyncio.run(manage_tool({"action": "reload"}, ctx))["ok"]


@pytest.fixture
def supervisor_state(tmp_path):
    from supervisor.eventlog import EventLog
    from supervisor.policy import PolicyEngine
    from supervisor.state import SupervisorState

    return SupervisorState(
        workspace_root=tmp_path,
        eventlog=EventLog(tmp_path / "events.jsonl", run_id="batch4"),
        policy=PolicyEngine(workspace_root=tmp_path),
    )


def _batch4_task(state, task_id, session_id, *, status=None, **kwargs):
    from datetime import datetime, timezone
    from supervisor.types import Task, TaskKind, TaskStatus

    now = datetime.now(timezone.utc)
    task = Task(
        task_id=task_id, session_id=session_id, kind=TaskKind.tool,
        status=TaskStatus.pending if status is None else status,
        created_at=now, updated_at=now, **kwargs,
    )
    state.tasks[task_id] = task
    state._task_order.append(task_id)
    return task


@pytest.mark.parametrize("content", ["", "  answer\n", [], [{"type": "text", "text": "image caption"}]])
def test_message_projections_preserve_typed_content_and_metadata(content):
    from engine.conversation_store import Message
    from supervisor.api import _conversation_message_to_compact_dict
    from supervisor.task_router import _message_to_turn_summary_dict

    calls = [{"id": "call-1", "function": {"name": "search", "arguments": "{}"}}]
    message = Message(
        id="m-1", role="assistant", content=content, message_type="assistant",
        source_task_id="task-1", ephemeral=True, meta={"usage": {"prompt_tokens": 0}},
        tool_calls=calls, tool_call_id="result-1", name="search",
    )
    summary = _message_to_turn_summary_dict(message)
    compact = _conversation_message_to_compact_dict(message)
    for row in [summary, compact]:
        assert row["role"] == "assistant"
        assert row["content"] == content
        assert row["tool_calls"] is calls
        assert row["tool_call_id"] == "result-1" and row["name"] == "search"
        assert row["_meta"]["source_task_id"] == "task-1"
        assert row["_meta"] is not message.meta
        assert row["_meta"]["usage"]["prompt_tokens"] == 0
    assert compact["_dynamic"] is True
    assert compact["_meta"]["message_type"] == "assistant"
    assert "message_type" not in summary["_meta"]
    assert message.meta == {"usage": {"prompt_tokens": 0}}

    message.meta.update(source_task_id="original", message_type="original-type")
    projected = _conversation_message_to_compact_dict(message)
    assert projected["_meta"]["source_task_id"] == "original"
    assert projected["_meta"]["message_type"] == "original-type"


def test_turn_summary_repairs_pairs_without_mutating_stored_messages():
    from engine.conversation_store import Message
    from supervisor.task_router import _format_task_messages_for_turn_summary

    calls = [
        {"id": "finish-1", "type": "function", "function": {"name": "finish", "arguments": '{"text": "delivered"}'}},
        {"id": "orphan", "type": "function", "function": {"name": "search", "arguments": "{}"}},
    ]
    assistant = Message(id="a", role="assistant", content="answer", tool_calls=calls, source_task_id="task")
    result = Message(id="r", role="tool", content="delivered", tool_call_id="finish-1", name="finish")
    text = _format_task_messages_for_turn_summary([assistant, result])
    assert "delivered" in text and "COMPLETED TASK LOG" in text
    assert assistant.tool_calls == calls and len(assistant.tool_calls) == 2
    assert assistant.meta == {} and result.role == "tool"


def test_running_tasks_reaps_zombie_once_and_updates_terminal_registry(supervisor_state, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from supervisor.types import TaskStatus

    state = supervisor_state
    root = state.get_or_create_session(channel="test", conversation_key="test:zombie")
    child, _ = state.get_or_create_child_session(root, "worker", "", "accumulate")
    zombie = _batch4_task(
        state, "zombie", root, status=TaskStatus.running, worker_id="worker",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=4),
        input={"_system_task": True, "child_session_id": child},
    )
    pending = _batch4_task(state, "pending", root)
    cleanup = Mock(wraps=state._cleanup_tool_result_artifacts_for_task)
    monkeypatch.setattr(state, "_cleanup_tool_result_artifacts_for_task", cleanup)
    rows = state.list_session_running_tasks(root)
    assert [row["task_id"] for row in rows] == [pending.task_id]
    assert zombie.status == TaskStatus.failed and zombie.lease_expires_at is None
    assert zombie.result == {"action": "fail", "error": "lease expired (zombie reaped)"}
    entry = state._session_store._registry[child]
    assert entry["terminal_status"] == "failed" and entry["completed_at"]
    state.list_session_running_tasks(root)
    cleanup.assert_called_once_with("zombie")
    events = state.eventlog.list_events(session_id=root, after_seq=0)
    assert sum(e["type"] == "task_completed" and e["payload"]["task_id"] == "zombie" for e in events) == 1
    assert state.list_session_running_tasks("missing") is None


@pytest.mark.parametrize("lease_kind,awaiting_approval,expected", [
    ("missing", False, "failed"), ("expired", False, "failed"),
    ("recent", False, "running"), ("future", False, "running"),
    ("missing", True, "running"), ("expired", True, "running"),
])
def test_running_tasks_respects_lease_grace_and_approval(supervisor_state, lease_kind, awaiting_approval, expected):
    from datetime import datetime, timedelta, timezone
    from supervisor.types import Approval, ApprovalStatus, TaskStatus

    state = supervisor_state
    sid = state.get_or_create_session(channel="test", conversation_key="test:leases")
    now = datetime.now(timezone.utc)
    lease = {"missing": None, "expired": now - timedelta(minutes=4),
             "recent": now - timedelta(minutes=1), "future": now + timedelta(minutes=1)}[lease_kind]
    task = _batch4_task(state, "lease", sid, status=TaskStatus.running,
                        lease_expires_at=lease, input={"_system_task": True})
    if awaiting_approval:
        state.approvals["approval"] = Approval(
            approval_id="approval", session_id=sid, operation="write_file",
            status=ApprovalStatus.pending, fingerprint="fp", requested_at=now,
        )
    rows = state.list_session_running_tasks(sid)
    assert task.status.value == expected
    assert [row["task_id"] for row in rows] == ([] if expected == "failed" else ["lease"])


def test_running_tasks_rechecks_candidates_after_routing_changes_state(supervisor_state, monkeypatch):
    from supervisor.types import TaskStatus

    state = supervisor_state
    sid = state.get_or_create_session(channel="test", conversation_key="test:route")
    first = _batch4_task(state, "first", sid, status=TaskStatus.running)
    second = _batch4_task(state, "second", sid, status=TaskStatus.running)

    def route(task):
        assert task is first
        second.status = TaskStatus.cancelled
        _batch4_task(state, "created-by-route", sid)

    monkeypatch.setattr(state, "_route_completed_task_locked", route)
    rows = state.list_session_running_tasks(sid)
    assert [row["task_id"] for row in rows] == ["created-by-route"]
    assert second.status == TaskStatus.cancelled


def test_delete_session_cascades_persisted_tree_and_retains_unrelated_work(supervisor_state):
    from datetime import datetime, timedelta, timezone
    from engine.conversation_store import ConversationStore, Message
    from supervisor.types import Approval, ApprovalStatus, TaskStatus

    state = supervisor_state
    root = state.get_or_create_session(channel="test", conversation_key="test:delete")
    child, _ = state.get_or_create_child_session(root, "worker", "", "fresh")
    branch, _ = state._create_entry_branch_locked(child, 1)
    other = state.get_or_create_session(channel="test", conversation_key="test:other")
    state.conversation_map["alias-to-delete"] = root
    # Persistence must still supply the descendant after its fast index entries disappear.
    state.parent_children.pop(child, None)
    state.parent_entry_branches.pop(child, None)
    state.entry_branch_parents.pop(branch, None)
    now = datetime.now(timezone.utc)
    pending = _batch4_task(state, "pending-delete", child)
    leased = _batch4_task(state, "leased-delete", branch, status=TaskStatus.running,
                          worker_id="worker", lease_expires_at=now + timedelta(minutes=1))
    detached = _batch4_task(state, "detached-delete", "detached", caller_task_id=pending.task_id)
    untouched = _batch4_task(state, "untouched", other)
    for task in [pending, detached, untouched]:
        state.approvals[task.task_id] = Approval(
            approval_id=task.task_id, session_id=task.session_id, task_id=task.task_id,
            operation="write_file", status=ApprovalStatus.pending,
            fingerprint=task.task_id, requested_at=now,
        )
        state._async_tools[task.task_id] = {
            "session_id": task.session_id, "task_id": task.task_id,
            "worker_id": "worker", "status": "running",
        }
    store = ConversationStore(state.workspace_root / "data" / "conversations")
    for sid in [root, child, branch, other]:
        store.append(sid, Message(id=sid, role="user", content="history"))
        context_dir = state.workspace_root / "data" / "node_contexts" / sid
        context_dir.mkdir(parents=True)
        (context_dir / "context.json").write_text("{}", encoding="utf-8")
        state._session_context_usage[(sid, "worker")] = {"prompt_tokens": 1}

    assert state.delete_session(root)
    deleted = {root, child, branch}
    assert not deleted.intersection(state.sessions)
    assert not deleted.intersection(state._session_store._registry)
    assert not deleted.intersection(state.conversation_map.values())
    assert not deleted.intersection(state.entry_branch_parents)
    assert not deleted.intersection(state.child_session_map.values())
    assert not deleted.intersection(state.parent_children)
    assert not deleted.intersection(state.parent_entry_branches)
    assert all(sid not in deleted for sid, _ in state._session_context_usage)
    for sid in deleted:
        assert not (store._data_dir / f"{sid}.jsonl").exists()
        assert not (state.workspace_root / "data" / "node_contexts" / sid).exists()
    assert pending.status == detached.status == TaskStatus.cancelled
    assert leased.cancel_requested and leased.status == TaskStatus.running
    for task in [pending, detached]:
        assert state.approvals[task.task_id].status == ApprovalStatus.denied
    assert set(state.pull_pending_cancels("worker")) == {pending.task_id, detached.task_id}
    assert untouched.status == TaskStatus.pending
    assert state.approvals[untouched.task_id].status == ApprovalStatus.pending
    assert other in state.sessions and (store._data_dir / f"{other}.jsonl").exists()
    assert not state.delete_session(root)
    # A late engine completion is cancelled rather than routed back into deleted history.
    state.complete_task(task_id=leased.task_id, worker_id="worker", result={"action": "finish", "result": {"text": "late"}})
    assert leased.status == TaskStatus.cancelled
    assert not (store._data_dir / f"{branch}.jsonl").exists()


def test_session_store_remove_rolls_back_on_atomic_write_failure(supervisor_state, monkeypatch):
    from supervisor import session_store

    state = supervisor_state
    sid = state.get_or_create_session(channel="test", conversation_key="test:flush-failure")
    store = state._session_store
    original = store._registry[sid]
    disk = store._path.read_bytes()

    def fail_replace(*args):
        raise PermissionError("registry denied")

    monkeypatch.setattr(session_store.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="registry denied"):
        store.remove_session(sid)
    assert store._registry[sid] is original
    assert store._path.read_bytes() == disk


def test_delete_session_does_not_report_success_on_history_delete_failure(supervisor_state, monkeypatch):
    from engine.conversation_store import ConversationStore

    state = supervisor_state
    sid = state.get_or_create_session(channel="test", conversation_key="test:delete-failure")

    def fail_delete(self, session_id):
        raise PermissionError("history denied")

    monkeypatch.setattr(ConversationStore, "delete", fail_delete)
    with pytest.raises(PermissionError, match="history denied"):
        state.delete_session(sid)
    assert sid in state.sessions and sid in state._session_store._registry
    assert state.conversation_map["test:delete-failure"] == sid


def test_parallel_running_task_queries_commit_one_failure(supervisor_state):
    from concurrent.futures import ThreadPoolExecutor
    from supervisor.types import TaskStatus

    state = supervisor_state
    sid = state.get_or_create_session(channel="test", conversation_key="test:parallel-reaper")
    task = _batch4_task(state, "parallel", sid, status=TaskStatus.running,
                        input={"_system_task": True})
    ready = threading.Barrier(2)

    def query():
        ready.wait(timeout=5)
        return state.list_session_running_tasks(sid)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(query) for _ in range(2)]
        assert [future.result(timeout=10) for future in results] == [[], []]
    assert task.status == TaskStatus.failed
    events = state.eventlog.list_events(session_id=sid, after_seq=0)
    assert sum(e["type"] == "task_completed" for e in events) == 1


def test_zombie_branch_is_finalized_through_normal_failure_route(supervisor_state):
    from supervisor.types import TaskStatus

    state = supervisor_state
    root = state.get_or_create_session(channel="test", conversation_key="test:branch-reaper")
    branch, fork_meta = state._create_entry_branch_locked(root, 1)
    task = _batch4_task(
        state, "branch-zombie", branch, status=TaskStatus.running,
        input={"parent_session_id": root, "branch_session_id": branch,
               "base_count": fork_meta["base_count"], "_system_task": True},
    )
    assert state.list_session_running_tasks(root) == []
    assert task.status == TaskStatus.failed and task.input["_branch_finalized"]
    assert branch not in state.sessions and branch not in state._session_store._registry
    assert branch not in state.entry_branch_parents


def test_delete_session_removes_descendants_before_parent_on_failure(supervisor_state, monkeypatch):
    from engine.conversation_store import ConversationStore

    state = supervisor_state
    root = state.get_or_create_session(channel="test", conversation_key="test:delete-order")
    child, _ = state.get_or_create_child_session(root, "worker", "", "fresh")
    nested, _ = state.get_or_create_child_session(child, "nested-worker", "", "fresh")
    deleted = []
    delete = ConversationStore.delete

    def fail_nested(self, sid):
        deleted.append(sid)
        if sid == nested:
            raise PermissionError("nested denied")
        delete(self, sid)

    monkeypatch.setattr(ConversationStore, "delete", fail_nested)
    with pytest.raises(PermissionError, match="nested denied"):
        state.delete_session(root)
    assert deleted == [nested]
    assert root in state.sessions
    assert child in state.parent_children[root] and nested in state.parent_children[child]
    monkeypatch.setattr(ConversationStore, "delete", delete)
    assert state.delete_session(root)
    assert not {root, child, nested}.intersection(state._session_store._registry)


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


@pytest.mark.parametrize("result,spec,expected", [
    pytest.param(
        {"_display": {"format": "markdown", "raw": "**ready**"}}, None,
        ("markdown", "**ready**"), id="display_directive",
    ),
    pytest.param(
        {"answer": 42}, {"result_format": "text"},
        ("text", "{'answer': 42}"), id="spec_result_format",
    ),
    pytest.param(
        {"data": {"results": [
            {"success": True, "type": "text", "path": "a.py", "content": "print(1)"},
            {"success": True, "type": "multimodal", "path": "b.png", "mimeType": "image/png", "size": 12},
            {"success": True, "type": "binary", "path": "c.bin", "size": 3},
            {"success": False, "path": "missing", "error": "not found"},
        ]}}, None,
        ("text", "── a.py ──\nprint(1)\n── b.png ── [image: image/png, 12 bytes]\n"
         "── c.bin ── [binary: 3 bytes]\n── missing ── ERROR: not found"),
        id="read_file_batch",
    ),
    pytest.param(
        {"data": {"results": [{"file": "a.py", "line": 7, "match": "one\ntwo", "context": "before\nafter"}],
                  "count": 1, "truncated": True}}, None,
        ("text", "1 results found (truncated):\n\na.py:7 | one\\ntwo\n  before\n  after"),
        id="search_matches",
    ),
    pytest.param(
        {"data": {"filesModified": 1, "totalReplacements": 2,
                  "results": [{"file": "a.py", "replacements": 2}]}}, None,
        ("text", "1 files modified, 2 replacements total\na.py: 2 replacements"),
        id="search_replace",
    ),
    pytest.param(
        {"data": {"totalFiles": 1, "totalDirs": 1, "results": [
            {"path": ".", "success": True, "fileCount": 1, "dirCount": 1,
             "entries": [{"name": "src", "type": "directory"}, {"name": "a.py", "type": "file"}]},
            {"path": "private", "success": False, "error": "denied", "entries": []},
        ]}}, None,
        ("text", "── . ── (1 files, 1 dirs)\n  📁 src/\n  📄 a.py\n\n"
         "── private ── ERROR: denied\n\nTotal: 1 files, 1 dirs"),
        id="list_dir_tree",
    ),
    pytest.param(
        {"returncode": 0, "output": "done"}, None,
        ("text", "returncode=0\ndone"), id="command_output",
    ),
    pytest.param(
        {"data": {"file": "a.py", "diffCount": 1, "appliedCount": 0, "failedCount": 1}}, None,
        ("text", "apply_diff on a.py: 0 applied, 1 rejected"), id="apply_diff_summary",
    ),
    pytest.param(
        {"ok": True, "path": "a.py", "bytes": 0}, None,
        ("text", "File written: a.py"), id="write_file_summary",
    ),
    pytest.param(
        {"result": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "image", "path": "data/pic.png"},
            {"type": "resource", "resource": {"uri": "file:///a.py"}},
        ]}}, None,
        ("text", "hello\n[image: data/pic.png]\n[resource: file:///a.py]"), id="mcp_content_parts",
    ),
    pytest.param(
        {"data": {"attachments": [{"path": "data/pic.png"}], "description": "  produced  "},
         "attachments": ["data/pic.png", {"path": "data/sound.wav"}], "message": "secondary"}, None,
        ("text", "produced\nAttachments:\n- data/pic.png\n- data/sound.wav"), id="attachment_result",
    ),
    pytest.param(
        {"text": "first", "description": "second"}, None,
        ("text", "first"), id="primary_text_result",
    ),
    ({'text': '', 'message': 'not selected'}, None, ('text', '')),
    ({'_display': '', 'text': 'not selected'}, None, ('text', '')),
    ({'data': {'results': [], 'count': 0, 'truncated': False}}, None, ('text', '0 results found:')),
    ({'data': {'results': [], 'totalFiles': 0, 'totalDirs': 0}}, None, ('text', 'Total: 0 files, 0 dirs')),
    ({'returncode': 0, 'output': ''}, None, ('text', 'returncode=0\n')),
    ({'data': {'filesModified': 0, 'totalReplacements': 0}}, None, ('text', '0 files modified, 0 replacements total')),
    ({'content': [{'type': 'text', 'text': ''}]}, None, ('text', '')),
    ({'_format': 'command_output', 'returncode': 2, 'output': 'bad'}, None, ('text', 'returncode=2\nbad')),
    ({'_format': 'text', 'message': 'hello'}, None, ('text', 'hello')),
    ({'_display': {'text': 'hello', 'format': 'html'}}, None, ('html', 'hello')),
    ({'_display': {'raw': '', 'text': 'not selected'}}, None, ('text', '')),
    ({'_format': 'missing', 'text': 'fallback'}, None, ('text', 'fallback')),
    ({'data': {'path': 'old.txt'}}, None, ('text', 'File written: old.txt')),
    ({'data': {'path': 'old.txt', 'applied': 2, 'rejected': 0}}, None, ('text', 'apply_diff on old.txt: 2 applied, 0 rejected')),
    ({'content': [{'type': 'image', 'mime_type': 'image/png', 'data': 'abcd'}]}, None, ('text', '[image: image/png, 4 base64 chars]')),
    (None, {"result_format": 'text'}, ('text', 'None')),
    ([1, 2], {"result_format": 'text'}, ('text', '[1, 2]')),
    ([1, 2], {"result_format": 'json'}, ('json', '[\n  1,\n  2\n]')),
    ({'returncode': 0, 'output': 'done'}, {"result_format": 'command_output'}, ('text', 'returncode=0\ndone')),
    ({'text': 'fallback'}, {"result_format": 'missing'}, ('text', 'fallback')),
    ({"_format": "json", "text": "do not use plain text"}, None, ("json", json.dumps({"_format": "json", "text": "do not use plain text"}, ensure_ascii=False, indent=2))),
    ({"_format": "json", "text": "do not use plain text"}, {"result_format": "text"}, ("json", json.dumps({"_format": "json", "text": "do not use plain text"}, ensure_ascii=False, indent=2))),
    ({"data": {"result": "canonical result"}, "_display": "other text", "_format": "json"}, {"result_format": "json"}, ("text", "canonical result")),
    ({"data": {"result": ""}, "_display": "other text", "_format": "json"}, {"result_format": "json"}, ("text", "")),
    ({"content": [{"type": "resource", "resource": {"text": "hello"}}]}, None, ("text", json.dumps({"type": "resource", "resource": {"text": "hello"}}, ensure_ascii=False, indent=2))),
])
def test_default_formatter_outputs(result, spec, expected):
    assert result_to_raw("external_tool", result, tool_spec=spec) == expected


@pytest.mark.parametrize("result", [
    {'data': {'results': []}},
    {'content': []},
    {'data': {'path': 'a.py', 'unrelated': True}},
    {'ok': False, 'path': 'a.py', 'bytes': 1},
    {'data': {'results': [{'file': 'a.py', 'line': 1}], 'count': 1, 'truncated': False}},
    {'data': {'results': [{'entries': [{'name': 'x', 'type': 'unknown'}]}], 'totalFiles': 0, 'totalDirs': 0}},
    {'content': [{'type': 'unrecognized', 'text': 'not a supported MCP part'}]},
    {'data': {'results': [{'success': True, 'path': 'a.py', 'type': []}]}},
    {'content': [{'type': []}]},
    {'data': {'results': [{'entries': [{'name': 'x', 'type': []}]}], 'totalFiles': 0, 'totalDirs': 0}},
    'plain',
])
def test_unrecognized_structures_fall_back_to_json(result):
    format_, text = result_to_raw("external_tool", result)
    assert format_ == "json"
    assert json.loads(text) == result


@pytest.mark.parametrize("mode,allow,deny,expected", [
    ("all", [], ["manage_memory:!list"], ["list"]),
    ("allowlist", ["manage_memory:list"], [], ["list"]),
    ("allowlist", ["manage_memory:!upsert,delete"], [], ["list"]),
    ("allowlist", ["list_memories"], [], ["list"]),
    ("all", [], ["save_memory", "delete_memory"], ["list"]),
    ("all", [], ["manage_memory:delete"], ["list", "upsert"]),
    ("allowlist", ["manage_memory"], ["delete_memory"], ["list", "upsert"]),
])
def test_manage_memory_action_permissions_prune_schema_and_enforce_synonyms(mode, allow, deny, expected):
    import copy
    from engine.builtin.knowledge_inject import PLUGIN_META
    from engine.inference.ai_step import _is_pure_tool_call
    from engine.inference.pseudo_tools import build_node_tool_specs, is_tool_action_allowed
    from engine.node import Node, ToolAccess

    spec = next(spec for spec in PLUGIN_META["tools"] if spec["name"] == "manage_memory")
    original = copy.deepcopy(spec["input_schema"])
    node = Node(id="reader", type="ai", control_tools="none", tool_access=ToolAccess(
        mode=mode, allow=allow, deny=deny,
    ))
    allowed_names, visible = build_node_tool_specs(registry=SimpleNamespace(list_specs=lambda: [spec]), node=node)
    assert "manage_memory" in allowed_names
    assert visible[0]["function"]["parameters"]["properties"]["action"]["enum"] == expected
    assert spec["input_schema"] == original
    for action, canonical in [("list", "list"), ("get", "list"), ("read", "list"),
                              ("upsert", "upsert"), ("save", "upsert"), ("create", "upsert"),
                              ("update", "upsert"), ("delete", "delete"), ("remove", "delete")]:
        assert is_tool_action_allowed(node, "manage_memory", {"action": action}) == (canonical in expected)
    assert is_tool_action_allowed(node, "manage_memory", {}) == ("list" in expected)
    assert is_tool_action_allowed(node, "manage_memory", {"content": "write"}) == ("upsert" in expected)
    for legacy, action in [("list_memories", "list"), ("save_memory", "upsert"), ("delete_memory", "delete")]:
        assert is_tool_action_allowed(node, legacy, {}) == (action in expected)
    assert _is_pure_tool_call("manage_memory", {"action": "list"}, spec)
    assert _is_pure_tool_call("manage_memory", {}, spec)
    assert not _is_pure_tool_call("manage_memory", {"action": "delete"}, spec)
    assert not _is_pure_tool_call("manage_memory", {"content": "write"}, spec)
    assert _is_pure_tool_call("read_file", {}, None)
    assert _is_pure_tool_call("custom_reader", {}, {"pure": True})


@pytest.mark.parametrize("prompt,estimate,threshold,source,effective", [
    (80000, 12000, 160000, "provider_reported", 80000),
    (0, 40000, 100000, "char_estimate", 40000),
    (-1, 200000, 160000, "char_estimate", 200000),
])
def test_get_context_window_exposes_token_metrics_and_does_not_limit_calls(tmp_path, monkeypatch, prompt, estimate, threshold, source, effective):
    from toolbox.builtins import tasks
    import httpx

    cfg = {} if threshold == 160000 else {"engine": {"compact": {"threshold_tokens": threshold}}}
    monkeypatch.setattr(tasks, "load_runtime_config", lambda root: cfg)
    urls = []
    def respond(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"prompt_tokens": prompt, "estimated_tokens": estimate, "message_count": 12})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            ctx = SimpleNamespace(http=http, supervisor_url="http://supervisor", workspace_root=tmp_path,
                                  route_session_id=lambda: "parent", task_id="same-task")
            for _ in range(4):
                result = await tasks.get_context_window({}, ctx)
                assert result["ok"]
                data = result["data"]
                assert data["effective_tokens"] == effective
                assert data["source"] == source
                assert data["compact_threshold"] == threshold
                assert data["utilization_percent"] == pytest.approx(effective / threshold * 100)
                assert data["remaining_tokens"] == max(0, threshold - effective)
                assert data["message_count"] == 12
                for text in [f"{effective:,}", f"{threshold:,}", source, "messages=12", f"prompt_tokens={prompt}"]:
                    assert text in data["result"]
    asyncio.run(run())
    assert urls == ["http://supervisor/v1/sessions/parent/context_usage"] * 4


def test_manage_memory_dispatch_and_cache_invalidation(tmp_path):
    from engine.builtin import knowledge_inject as memory
    _LEGACY_TOOL_ALIASES = ToolRegistry._LEGACY_ALIAS_MAP

    ctx = SimpleNamespace(workspace_root=tmp_path, node_id="main", caller_node_id="", node_extra={}, _node_id="main", _node_extra={}, workspace_name="")
    async def run():
        assert (await memory.manage_memory({"action": "create", "book": "test", "id": "entry", "content": "remember"}, ctx))["ok"]
        result = await memory.manage_memory({"action": "read", "book": "test", "id": "entry"}, ctx)
        assert result["data"]["entries"][0]["content"] == "remember"
        memory._hit_cache["entry"] = "now"
        assert (await memory.manage_memory({"action": "remove", "book": "test", "id": "entry"}, ctx))["ok"]
        assert "entry" not in memory._hit_cache
        assert not (await memory.manage_memory({"action": "unknown"}, ctx))["ok"]
    asyncio.run(run())
    public = {spec["name"] for spec in memory.PLUGIN_META["tools"]}
    assert "manage_memory" in public
    assert not public.intersection({"save_memory", "list_memories", "delete_memory"})
    assert _LEGACY_TOOL_ALIASES["save_memory"] == ("manage_memory", "upsert")
    assert _LEGACY_TOOL_ALIASES["list_memories"] == ("manage_memory", "list")
    assert _LEGACY_TOOL_ALIASES["delete_memory"] == ("manage_memory", "delete")


@pytest.mark.parametrize("name,args,blocked", [
    ("manage_memory", {"action": "upsert", "content": "write"}, True),
    ("manage_memory", {"action": "delete", "id": "entry"}, True),
    ("manage_memory", {"content": "implicit write"}, True),
    ("save_memory", {"content": "legacy write"}, True),
    ("delete_memory", {"id": "entry"}, True),
    ("manage_memory", {"action": "list"}, False),
])
def test_ai_step_rejects_ungranted_memory_actions_before_execution(monkeypatch, name, args, blocked):
    from engine.inference import ai_step
    from engine.inference.tool_format import NativeToolFormatter
    from engine.node import Node, ToolAccess

    node = Node(id="reader", type="ai", tool_access=ToolAccess(mode="allowlist", allow=["manage_memory:list"]))
    ls = SimpleNamespace(node=node, messages=[], formatter=NativeToolFormatter(),
        allowed_real_tools={"manage_memory", "save_memory", "list_memories", "delete_memory"},
        allowed_pseudo_tools=set(), provider=SimpleNamespace(name="test"), last_usage={}, openai_tools=[],
        rctx=SimpleNamespace(tool_call_log=[], response_info={}, current_llm_request_id=""),
        _reasoning_started_iso="", _reasoning_ended_iso="")
    tc = SimpleNamespace(id="call", name=name, arguments=args)
    resp = SimpleNamespace(tool_calls=[tc], text="", provider_meta={}, reasoning="", thought_summary="", inline_data=[])
    observed = []
    sentinel = object()
    async def stop_before_execution(point, context):
        assert point == "before_tool_call"
        observed.append(context.extra)
        return SimpleNamespace(action=sentinel)
    monkeypatch.setattr(ai_step.hook_registry, "afire", stop_before_execution)
    monkeypatch.setattr(ai_step, "_shadow_write", lambda *a, **kw: None)
    assert asyncio.run(ai_step._handle_tool_calls_inner(ls, resp, 0)) is sentinel
    assert observed[0]["unauthorized_count"] == int(blocked)
    assert len(observed[0]["real_tool_calls"]) == int(not blocked)
    assert len(ls.rctx.tool_call_log) == int(not blocked)


def test_legacy_manager_actions_cannot_override_authorized_alias(tmp_path):
    from engine.inference.pseudo_tools import _filter_tool_specs, is_tool_action_allowed
    from engine.node import Node, ToolAccess

    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    for old, action in [("list_memories", "list"), ("save_memory", "upsert"), ("delete_memory", "delete")]:
        name, args = registry._resolve_alias(old, {"action": "forged"})
        assert name == "manage_memory" and args["action"] == action
    node = Node(id="skill-reader", type="ai", tool_access=ToolAccess(mode="all", deny=["delete_skill"]))
    spec = {"name": "manage_skill", "input_schema": {"properties": {"action": {"enum": ["list", "upsert", "delete"]}}}}
    assert _filter_tool_specs(node, [spec])[0]["input_schema"]["properties"]["action"]["enum"] == ["list", "upsert"]
    assert not is_tool_action_allowed(node, "manage_skill", {"action": "remove"})
    assert not is_tool_action_allowed(node, "delete_skill", {})
    assert not is_tool_action_allowed(node, "list_skills", {"action": "remove"})

@pytest.fixture
def read_file_contract(tmp_path):
    import json
    import httpx
    from toolbox.builtins.read_file import read_file

    (tmp_path / "a.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    requested_paths = []

    async def run(args, cancel_path=None):
        def respond(request):
            if request.url.path == "/v1/ops/request":
                payload = json.loads(request.content)
                assert payload["op"] == "read_file"
                path = payload["parameters"]["path"]
                requested_paths.append(path)
                decision = ({"safety_level": "approval_required", "approval_id": "stop"}
                            if path == cancel_path else {"safety_level": "allow"})
                return httpx.Response(200, json=decision)
            if request.url.path == "/v1/approvals/stop":
                return httpx.Response(200, json={"status": "cancelled"})
            assert request.url.path == "/v1/sessions/s/cancelled"
            return httpx.Response(200, json={"cancelled": False})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            ctx = ToolContext(supervisor_url="http://supervisor", session_id="s", run_id="r",
                              worker_id="w", workspace_root=tmp_path, http=http, registry=None)
            return await read_file(args, ctx)

    return run, requested_paths


@pytest.mark.parametrize("start,end", [("startLine", "endLine"), ("start_line", "end_line")])
def test_read_file_single_path_line_range(read_file_contract, start, end):
    run, paths = read_file_contract
    result = asyncio.run(run({"path": "a.txt", start: 2, end: 3}))
    assert result["ok"] and paths == ["a.txt"]
    entry = result["data"]["results"][0]
    assert (entry["startLine"], entry["endLine"], entry["totalLines"]) == (2, 3, 4)
    assert entry["content"] == "   2 | two\n   3 | three"


@pytest.mark.parametrize("key", ["files", "items"])
def test_read_file_batch_summary_precedes_bodies(read_file_contract, key):
    run, paths = read_file_contract
    result = asyncio.run(run({key: [
        {"path": "a.txt", "startLine": 2, "endLine": 3}, {"path": "b.txt"},
        {"path": "missing.txt"},
    ]}))
    data = result["data"]
    assert not result["ok"] and paths == ["a.txt", "b.txt", "missing.txt"]
    assert (data["successCount"], data["failCount"], data["totalCount"]) == (2, 1, 3)
    assert data["result"].startswith(
        "[Batch read: 2 succeeded, 1 failed]\n"
        "- a.txt: ok (L2-3/4)\n- b.txt: ok (L1-2/2)\n"
        "- missing.txt: ERROR (File not found)\n"
    )
    assert "   2 | two\n   3 | three" in data["result"]
    assert "   1 | alpha\n   2 | beta" in data["result"]


@pytest.mark.parametrize("args,error", [
    ({"files": [{"path": "a.txt"}] * 9}, "too many files in batch (9 > 8); read at most 8 files per call"),
    ({"files": [{"path": "a.txt"}, "b.txt"]}, "invalid file entry in files list (expected object)"),
    ({"files": []}, "empty files list"),
    ({"files": "a.txt"}, "empty files list"),
    ({"path": "a.txt", "startLine": 3, "endLine": 2}, "invalid line range (startLine/endLine must be >= 1 and endLine >= startLine)"),
    ({"files": [{"path": "a.txt", "startLine": 0}]}, "invalid line range (startLine/endLine must be >= 1 and endLine >= startLine)"),
    ({"path": "a.txt", "endLine": "bad"}, "invalid line range (startLine/endLine must be >= 1 and endLine >= startLine)"),
])
def test_read_file_rejects_invalid_arguments_before_approval(read_file_contract, args, error):
    run, paths = read_file_contract
    result = asyncio.run(run(args))
    assert not result["ok"] and not result["success"]
    assert result["error"] == error
    assert error in result["data"]["result"]
    assert paths == []


def test_read_file_cancelled_approval_stops_batch(read_file_contract):
    run, paths = read_file_contract
    result = asyncio.run(run({"files": [
        {"path": "a.txt"}, {"path": "b.txt"}, {"path": "missing.txt"},
    ]}, cancel_path="b.txt"))
    assert not result["ok"] and result["cancelled"] is True
    assert paths == ["a.txt", "b.txt"]
    data = result["data"]
    assert (data["successCount"], data["failCount"], data["totalCount"]) == (1, 1, 2)
    assert [entry["path"] for entry in data["results"]] == paths
    assert data["results"][-1]["cancelled"] is True
    assert data["results"][-1]["error"] == "task cancelled"


def test_read_file_schema_promotes_single_path_and_bounds_batches(tmp_path):
    registry = ToolRegistry(workspace_root=tmp_path, tools_dir=tmp_path / "tools")
    spec = registry.get_spec("read_file")
    props = spec["input_schema"]["properties"]
    assert set(props) == {"path", "startLine", "endLine", "files"}
    assert props["startLine"]["type"] == props["endLine"]["type"] == "integer"
    assert (props["files"]["minItems"], props["files"]["maxItems"]) == (1, 8)
    assert "Deprecated" not in props["path"]["description"]
