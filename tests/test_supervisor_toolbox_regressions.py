"""Regression coverage for scheduler isolation and tool metadata/upserts."""
import asyncio
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from supervisor import scheduler
from supervisor.task_router import TaskRouterMixin
from toolbox.builtins import schedules
from toolbox.builtins.apply_diff import _find_and_replace
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
