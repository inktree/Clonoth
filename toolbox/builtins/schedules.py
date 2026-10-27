"""Schedule management: create_schedule, list_schedules, delete_schedule."""
from __future__ import annotations

import re
from typing import Any

from ..context import ToolContext
from .._common import request_guard

_SCHEDULE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: schedule tools return structured management data but
    # still need a canonical readable transcript. How: place all structured fields
    # under data with a result summary. Purpose: align schedules with ok/data/error.
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    # [AutoC 2026-05-31] Why: schedule validation and approval failures should not
    # return legacy ok=false shapes. How: mirror optional flags under data and top
    # level while adding data.result. Purpose: keep scheduler failures readable.
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


async def create_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Create or update a scheduled task."""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return _err("empty schedule id")
    if not _SCHEDULE_ID_RE.fullmatch(sid):
        return _err("invalid schedule id: only [A-Za-z_][A-Za-z0-9_-]{0,63} allowed")

    schedules = load_schedules(ctx.workspace_root)
    existing: dict[str, Any] | None = None
    for s in schedules:
        if str(s.get("id") or "").strip() == sid:
            existing = dict(s)
            break
    prev = existing or {}

    cron_expr = str(args.get("cron") if args.get("cron") is not None else prev.get("cron") or "").strip()
    if not cron_expr:
        return _err("empty cron expression")
    parts = cron_expr.split()
    if len(parts) != 5:
        return _err("cron must be 5 fields: minute hour day month weekday")

    stype = str(args.get("type") if args.get("type") is not None else prev.get("type") or "message").strip()
    if stype not in ("message", "script"):
        return _err(f"invalid type: {stype}, must be 'message' or 'script'")

    text = str(args.get("text") if args.get("text") is not None else prev.get("text") or "").strip()
    if stype == "message" and not text:
        return _err("empty text (required for message type)")

    command = str(args.get("command") if args.get("command") is not None else prev.get("command") or "").strip()
    if stype == "script" and not command:
        return _err("empty command (required for script type)")

    conv_key = str(args.get("conversation_key") if args.get("conversation_key") is not None else prev.get("conversation_key") or "").strip()
    if not conv_key:
        # [2026-05-25] Auto-inherit from ToolContext (populated by supervisor
        # task_context → engine RunContext → ToolContext, zero I/O overhead).
        conv_key = str(getattr(ctx, "conversation_key", "") or "").strip()
    if not conv_key:
        conv_key = f"scheduler:{sid}"
    entry_node_id = str(args.get("entry_node_id") if args.get("entry_node_id") is not None else prev.get("entry_node_id") or "").strip()
    workflow_id = str(args.get("workflow_id") if args.get("workflow_id") is not None else prev.get("workflow_id") or "").strip()
    enabled = bool(args.get("enabled") if args.get("enabled") is not None else prev.get("enabled", True))
    once = bool(args.get("once") if args.get("once") is not None else prev.get("once", False))

    _op, err = await request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "schedule_id": sid})
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    # Reload after approval wait in case concurrent updates occurred
    schedules = load_schedules(ctx.workspace_root)
    entry: dict[str, Any] = {
        "id": sid,
        "cron": cron_expr,
        "conversation_key": conv_key,
        "enabled": enabled,
        "once": once,
    }
    if stype == "script":
        entry["type"] = "script"
        entry["command"] = command
        raw_timeout = args.get("timeout") if args.get("timeout") is not None else prev.get("timeout")
        timeout = int(raw_timeout or 30)
        entry["timeout"] = max(5, min(timeout, 300))
        raw_silent = args.get("silent") if args.get("silent") is not None else prev.get("silent", True)
        entry["silent"] = bool(raw_silent)
        if text:
            entry["text"] = text
    else:
        entry["text"] = text
    if entry_node_id:
        entry["entry_node_id"] = entry_node_id
    if workflow_id:
        entry["workflow_id"] = workflow_id

    replaced = False
    for i, s in enumerate(schedules):
        if str(s.get("id") or "").strip() == sid:
            schedules[i] = entry
            replaced = True
            break
    if not replaced:
        schedules.append(entry)

    save_schedules(ctx.workspace_root, schedules)
    return _ok(f"Schedule {'updated' if replaced else 'created'}: {sid}", schedule=entry, replaced=replaced)


async def list_schedules(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """List all scheduled tasks."""
    from supervisor.scheduler import load_schedules

    schedules = load_schedules(ctx.workspace_root)
    # [fix 2026-09-14] Why: result_to_raw 把 data.result 作为模型可见的全部文本
    # （第一短路路径），旧写法只给计数导致模型拿不到列表，工具无意义。How:
    # 每条一行纯索引（id/cron/类型/启停/once/ck，条数不限——单行长度固定，
    # 几十条也只有几 KB）；单项内容（text/command）不进清单，需要时凭 id
    # read_file data/schedules.yaml 查看。结构化 schedules 字段全量保留给
    # 前端/审计。Purpose: 一次调用掌握全部任务概貌，内容量由单行紧凑控制。
    if not schedules:
        return _ok("0 schedules (data/schedules.yaml is empty)", schedules=schedules)
    lines = [f"{len(schedules)} schedules (full entry details: read_file data/schedules.yaml):"]
    for s in schedules:
        sid = str(s.get("id") or "?")
        cron = str(s.get("cron") or "?")
        stype = "script" if s.get("type") == "script" else "message"
        enabled = "enabled" if s.get("enabled", True) else "disabled"
        once = ", once" if s.get("once") else ""
        ck = str(s.get("conversation_key") or "")
        # [fix 2026-09-14] 单行只保留索引性字段（id/cron/类型/启停/once/ck），
        # 不携带 text/command 内容——清单用于定位与决策，内容细节凭 id 去
        # read_file data/schedules.yaml 查询，避免单项描述堆占用上下文。
        line = f"- {sid} | cron '{cron}' | {stype} | {enabled}{once}"
        if ck:
            line += f" | ck={ck}"
        lines.append(line)
    return _ok("\n".join(lines), schedules=schedules)


async def delete_schedule(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete a scheduled task."""
    from supervisor.scheduler import load_schedules, save_schedules

    sid = str(args.get("id") or "").strip()
    if not sid:
        return _err("empty schedule id")

    _op, err = await request_guard(ctx, "write_file", {"path": "data/schedules.yaml", "delete_schedule": sid})
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))

    schedules = load_schedules(ctx.workspace_root)
    before = len(schedules)
    schedules = [s for s in schedules if str(s.get("id") or "").strip() != sid]
    if len(schedules) == before:
        return _err(f"schedule not found: {sid}")

    save_schedules(ctx.workspace_root, schedules)
    return _ok(f"Schedule deleted: {sid}", deleted=True, id=sid)
