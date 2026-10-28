"""Regression tests for the dynamic manage_workers pseudo tool.

[AutoC 2026-09-12] Why: child management spans RunContext discovery, pseudo-tool
composition, and action routing. How: exercise each boundary with deterministic HTTP
stubs. Purpose: manage_workers appears only for active children and cannot target
unrelated session tasks.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from engine.context import RunContext
from engine.inference import pseudo_handlers
from engine.inference.pseudo_handlers import _handle_pseudo_tool
from engine.inference.pseudo_tools import _is_pseudo_tool_name, build_node_tool_specs
from engine.node import Node


class _EmptyRegistry:
    """Provide the minimal registry API needed by tool composition."""

    def list_specs(self) -> list[dict[str, Any]]:
        return []


class _Response:
    """Small HTTP response compatible with RunContext and pseudo handlers."""

    def __init__(self, payload: Any, status_code: int = 200, text: str = "OK") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self) -> Any:
        return self._payload


class _DiscoveryHttp:
    """Return parent running tasks and durable child-session rows."""

    async def get(self, url: str, **kwargs: Any) -> _Response:
        if url.endswith("/running_tasks"):
            return _Response({"tasks": [
                {
                    "task_id": "parent-task",
                    "node_id": "parent",
                    "status": "running",
                    "is_user_entry": True,
                    "caller_task_id": "",
                    "created_at": "2026-09-12T15:00:00+00:00",
                },
                {
                    "task_id": "sibling-user-task",
                    "node_id": "parent",
                    "status": "running",
                    "is_user_entry": True,
                    "caller_task_id": "",
                    "created_at": "2026-09-12T15:01:00+00:00",
                },
                {
                    "task_id": "legacy-child-task-abcdef",
                    "node_id": "scout",
                    "status": "running",
                    "is_user_entry": False,
                    "caller_task_id": "",
                    "created_at": "2026-09-12T15:02:00+00:00",
                },
            ]})
        if url.endswith("/children"):
            return _Response([
                {
                    "task_id": "legacy-child-task-abcdef",
                    "node_id": "scout",
                    "status": "running",
                    "started_at": "2026-09-12T15:02:00+00:00",
                    "session_id": "child-legacy",
                },
                {
                    "task_id": "spawned-child-task-123456",
                    "node_id": "_worker",
                    "status": "awaiting_approval",
                    "started_at": "2026-09-12T15:03:00+00:00",
                    "session_id": "child-spawned",
                },
                {
                    "task_id": "completed-child-task",
                    "node_id": "_worker",
                    "status": "completed",
                    "started_at": "2026-09-12T14:00:00+00:00",
                    "session_id": "child-completed",
                },
            ])
        raise AssertionError(f"unexpected GET {url}")


class _ActionHttp:
    """Record manage_workers mutation requests."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append({"url": url, **kwargs})
        return _Response({"ok": True})


def _tool_names(specs: list[dict[str, Any]]) -> set[str]:
    """Extract OpenAI function names from composed tool specs."""
    return {
        str((spec.get("function") or {}).get("name") or "")
        for spec in specs
        if isinstance(spec, dict)
    }


def _run_context(http: Any) -> RunContext:
    """Build a RunContext using one HTTP stub for both client fields."""
    return RunContext(
        workspace_root=Path("."),
        supervisor_url="http://supervisor.test",
        session_id="branch-current",
        parent_session_id="parent-session",
        worker_id="engine-worker",
        http=http,
        llm_http=http,
        task_id="parent-task",
    )


def test_run_context_lists_only_active_child_tasks_and_deduplicates() -> None:
    """Discovery should combine legacy and child-session tasks without user entries."""
    tasks = asyncio.run(_run_context(_DiscoveryHttp()).list_active_child_tasks())

    assert [task["task_id"] for task in tasks] == [
        "legacy-child-task-abcdef",
        "spawned-child-task-123456",
    ]
    assert tasks[1]["node_id"] == "_worker"
    assert tasks[1]["status"] == "awaiting_approval"


def test_manage_workers_is_permanently_injected_and_replaces_preempt_and_ask() -> None:
    """manage_workers is permanently injected when allowed (no cache churn), while ask and preempt_task specs are not injected."""
    node = Node(id="parent", type="ai")

    specs = build_node_tool_specs(registry=_EmptyRegistry(), node=node)[1]

    assert _is_pseudo_tool_name("manage_workers") is True
    assert "manage_workers" in _tool_names(specs)
    assert "ask" not in _tool_names(specs)
    assert "preempt_task" not in _tool_names(specs)


def test_manage_workers_respects_control_tool_allowlist() -> None:
    """Explicit control-tool policy should gate dynamic management injection."""
    excluded = Node(id="excluded", type="ai", control_tools=["spawn_worker"])
    included = Node(id="included", type="ai", control_tools=["manage_workers"])

    assert "manage_workers" not in _tool_names(build_node_tool_specs(
        registry=_EmptyRegistry(), node=excluded,
    )[1])
    assert "manage_workers" in _tool_names(build_node_tool_specs(
        registry=_EmptyRegistry(), node=included,
    )[1])


def _handler_state(http: Any, tasks: list[dict[str, Any]]) -> SimpleNamespace:
    """Build the minimal loop state consumed by manage_workers."""
    async def list_active_child_tasks() -> list[dict[str, Any]]:
        return list(tasks)

    rctx = SimpleNamespace(
        supervisor_url="http://supervisor.test",
        session_id="branch-current",
        parent_session_id="parent-session",
        task_id="parent-task",
        http=http,
        list_active_child_tasks=list_active_child_tasks,
    )
    return SimpleNamespace(rctx=rctx)


def _run_handler(monkeypatch: Any, args: dict[str, Any], *, tasks: list[dict[str, Any]], http: Any) -> dict[str, Any]:
    """Execute one manage_workers call and decode its emitted result."""
    emitted: list[str] = []

    def fake_emit(ls: Any, pseudo_call: Any, content: str, **kwargs: Any) -> None:
        emitted.append(content)

    monkeypatch.setattr(pseudo_handlers, "_emit_pseudo_tool_result", fake_emit)
    call = SimpleNamespace(id="manage-1", name="manage_workers", arguments=args)
    result = asyncio.run(_handle_pseudo_tool(_handler_state(http, tasks), call, step=0))

    assert result is None
    assert len(emitted) == 1
    return json.loads(emitted[0])


def test_manage_workers_list_formats_active_children(monkeypatch) -> None:
    """List should return compact task identity, state, timestamps, and elapsed time."""
    tasks = [{
        "task_id": "spawned-child-task-123456",
        "node_id": "_worker",
        "status": "running",
        "created_at": "2026-09-12T15:00:00+00:00",
    }]

    payload = _run_handler(monkeypatch, {"action": "list"}, tasks=tasks, http=_ActionHttp())

    assert payload["active_children"] == 1
    assert payload["tasks"][0]["task_id"] == "spawned-chil"
    assert payload["tasks"][0]["node_id"] == "_worker"
    assert isinstance(payload["tasks"][0]["elapsed_sec"], int)


def test_manage_workers_message_resolves_unique_task_prefix(monkeypatch) -> None:
    """Message should resolve a visible prefix before calling the preempt endpoint."""
    http = _ActionHttp()
    tasks = [{"task_id": "abcdef123456-full", "node_id": "_worker", "status": "running"}]

    payload = _run_handler(
        monkeypatch,
        {"action": "message", "task_id": "abcdef123456", "message": "请补充验证"},
        tasks=tasks,
        http=http,
    )

    assert payload["success"] is True
    assert http.posts == [{
        "url": "http://supervisor.test/v1/tasks/abcdef123456-full/preempt",
        "json": {"message": "请补充验证"},
        "timeout": 10.0,
    }]


def test_manage_workers_cancel_all_uses_parent_session_and_excludes_caller(monkeypatch) -> None:
    """Cancel-all must preserve the caller while stopping its active children."""
    http = _ActionHttp()

    payload = _run_handler(monkeypatch, {"action": "cancel"}, tasks=[], http=http)

    assert payload == {"success": True, "cancelled": "all"}
    assert http.posts == [{
        "url": "http://supervisor.test/v1/sessions/parent-session/cancel_active_tasks",
        "params": {"exclude_task_id": "parent-task"},
        "timeout": 10.0,
    }]


def test_manage_workers_rejects_missing_message_fields(monkeypatch) -> None:
    """Message action requires both an active child reference and text."""
    payload = _run_handler(
        monkeypatch,
        {"action": "message", "task_id": ""},
        tasks=[],
        http=_ActionHttp(),
    )

    assert payload == {
        "success": False,
        "error": "task_id and message are required for 'message' action",
    }


def test_manage_workers_preempt_and_node_id_resolution(monkeypatch) -> None:
    """manage_workers supports graceful preempt action and targeting by node_id."""
    http = _ActionHttp()
    tasks = [{"task_id": "998877665544-full", "node_id": "scout", "status": "running"}]

    payload = _run_handler(
        monkeypatch,
        {"action": "preempt", "target": "scout"},
        tasks=tasks,
        http=http,
    )
    assert payload["success"] is True
    assert http.posts == [{
        "url": "http://supervisor.test/v1/tasks/998877665544-full/preempt",
        "timeout": 10.0,
    }]

    http2 = _ActionHttp()
    payload2 = _run_handler(
        monkeypatch,
        {"action": "cancel", "node_id": "smith"},
        tasks=[],
        http=http2,
    )
    assert payload2 == {"success": True, "cancelled": "node:smith"}
    assert http2.posts == [{
        "url": "http://supervisor.test/v1/sessions/parent-session/cancel_active_tasks",
        "params": {"exclude_task_id": "parent-task", "node_id": "smith"},
        "timeout": 10.0,
    }]
