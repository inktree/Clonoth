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
    for name in {"list_dir", "read_file", "list_schedules", "get_context_window"}:
        assert definitions[name]["pure"] is True
    assert definitions["write_file"]["pure"] is False
    assert not ({"cancel_active_tasks", "update_todo_list", "get_todo_list"} & definitions.keys())
