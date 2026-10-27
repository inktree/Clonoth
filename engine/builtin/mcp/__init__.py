"""MCP 工具桥：内置目录插件。

三个进程面按可用 face 分流（参照 retry_api / plugin_manager 的双进程形态）：

- engine 进程（tools face 可用）：PLUGIN_META 声明三个配置 CRUD 元工具；
  构造时启动后台桥任务——先做一次全量发现，之后每 10 秒比对
  data/mcp_clients.yaml 的 mtime，变化即重发现。发现结果通过
  register_builtin_tool 注册为一等工具（mcp_{cid}_{name}），写入 reload
  快照，registry.reload() 后天然存活；每个 disposer 同时归档到
  HookRegistry 的 disposal ledger，插件卸载时全部摘除。
- supervisor 进程（routes face 可用）：挂三个配置端点，mount="admin/config"
  保持与旧 admin_api 内联版本完全相同的路径与响应形状，前端零改动。
- 两进程共享：mcp_runtime.py 作为插件私有库（协议客户端运行时）。

CRUD 工具保存/删除配置后立即在本 worker 刷新，并 POST /v1/tools/reload
bump reload-seq：其他 worker 的 MCP 工具经快照存活、经 mtime 监视在 10 秒
内跟进新配置。

已知边界：引擎启动后的第一个任务可能赶不上首次发现完成（旧实现由
runner 启动预载保证），从第二个任务起工具必然可见；内置插件的卸载是
临时性的（下一个 AI 任务会重新自动注册），这是全部内置插件的既有语义。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Callable

import yaml
from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import mcp_runtime

logger = logging.getLogger(__name__)

_WATCH_INTERVAL_SEC = 10.0


def _ok(result_text: str, **fields: Any) -> dict[str, Any]:
    return {"ok": True, "data": {"result": result_text, **fields}}


def _err(message: Any, **fields: Any) -> dict[str, Any]:
    text = str(message)
    data = {"result": f"ERROR: {text}", **fields}
    response: dict[str, Any] = {"ok": False, "error": text, "data": data}
    response.update(fields)
    return response


# ---------------------------------------------------------------------------
#  CRUD meta tools（engine 进程，经 PLUGIN_META["tools"] 由 loader 注册）
# ---------------------------------------------------------------------------

async def create_or_update_mcp_client(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from toolbox._common import request_guard

    cid = str(args.get("id") or "").strip()
    _op, err = await request_guard(ctx, "write_file", {"path": "data/mcp_clients.yaml", "mcp_client_id": cid})
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))
    try:
        spec = mcp_runtime.upsert_client(ctx.workspace_root, args)
    except Exception as e:
        return _err(e)
    tool_count = await _refresh_and_notify(ctx)
    return _ok(
        f"MCP client saved: {spec.get('id', '')} ({tool_count} tools live)",
        client=spec,
        path="data/mcp_clients.yaml",
        tools=tool_count,
    )


async def list_mcp_clients(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    try:
        clients = mcp_runtime.list_clients(ctx.workspace_root)
        if not clients:
            return _ok("0 MCP clients (data/mcp_clients.yaml is empty)", clients=clients)
        lines = [f"{len(clients)} MCP clients (config: data/mcp_clients.yaml):"]
        for c in clients:
            cid = str(c.get("id") or "?")
            transport = str(c.get("transport") or "stdio")
            enabled = "enabled" if c.get("enabled", True) else "disabled"
            if transport == "stdio":
                cmd = str(c.get("command") or "")
                cargs = " ".join(str(x) for x in (c.get("args") or []))
                target = f"{cmd} {cargs}".strip()
            else:
                target = str(c.get("url") or "")
            desc = " ".join(str(c.get("description") or "").split())
            line = f"- {cid} | {transport} | {enabled}"
            if target:
                line += f" | {target}"
            if desc:
                line += f" | {desc}"
            lines.append(line)
        return _ok("\n".join(lines), clients=clients)
    except Exception as e:
        return _err(e)


async def delete_mcp_client(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from toolbox._common import request_guard

    client_id = str(args.get("id", "")).strip()
    if not client_id:
        return _err("empty client id")
    _op, err = await request_guard(ctx, "write_file", {"path": "data/mcp_clients.yaml", "delete_mcp_client": client_id})
    if err is not None:
        return _err(err.get("error", "denied"), cancelled=bool(err.get("cancelled", False)))
    try:
        ok = mcp_runtime.delete_client(ctx.workspace_root, client_id)
        if not ok:
            return _err(f"client not found: {client_id}")
        tool_count = await _refresh_and_notify(ctx)
        return _ok(f"MCP client deleted: {client_id} ({tool_count} tools live)", deleted=True, id=client_id, tools=tool_count)
    except Exception as e:
        return _err(e)


async def _refresh_and_notify(ctx: Any) -> int:
    """配置变更后：本 worker 立即刷新，并 bump reload-seq 通知其他 worker。

    Why: CRUD 之前保存配置后既不刷新也不通知，新客户端的工具要等手动
    reload_tools 或进程重启才出现。How: 先对 ctx.registry 跑一次
    refresh_mcp_tools（持有 mcp_* 工具的 worker 立即可用），再用引擎
    http 客户端（自带 admin token）POST /v1/tools/reload；其他 worker 的
    既有 mcp_* 工具经 reload 快照存活，新配置由各自的 mtime 监视跟进。
    Purpose: 配置变更秒级生效，不再依赖重启。
    """
    count = 0
    registry = getattr(ctx, "registry", None)
    if registry is not None:
        try:
            count = await refresh_mcp_tools(registry)
        except Exception:
            logger.warning("post-CRUD MCP refresh failed", exc_info=True)
    http = getattr(ctx, "http", None)
    supervisor_url = str(getattr(ctx, "supervisor_url", "") or "").rstrip("/")
    if http is not None and supervisor_url:
        try:
            await http.post(f"{supervisor_url}/v1/tools/reload")
        except Exception:
            pass  # 其他 worker 由 mtime 监视兜底
    return count


# ---------------------------------------------------------------------------
#  动态工具发现（engine 进程后台桥）
# ---------------------------------------------------------------------------

# 模块级桥状态：__init__ 每次进入 AI 节点都会重跑（builtin 自动发现的既有
# 语义），后台任务与 disposer 列表必须跨重入存活。
_BRIDGE: dict[str, Any] = {
    "started": False,
    "hooks": None,
    "disposers": [],
    "lock": None,
}


def _start_engine_bridge(tools_face: Any, ctx: Any) -> None:
    if _BRIDGE["started"]:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环（非 engine 推理路径）：不启动后台桥。
        return
    _BRIDGE["started"] = True
    _BRIDGE["hooks"] = getattr(ctx, "hooks", None)
    loop.create_task(_bridge_loop(tools_face))


async def _bridge_loop(registry: Any) -> None:
    ws = Path(getattr(registry, "workspace_root"))
    await refresh_mcp_tools(registry)
    last_mtime = _config_mtime(ws)
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_SEC)
        try:
            mtime = _config_mtime(ws)
            if mtime != last_mtime:
                last_mtime = mtime
                await refresh_mcp_tools(registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("mcp bridge watch iteration failed", exc_info=True)


def _config_mtime(workspace_root: Path) -> int | None:
    try:
        return (Path(workspace_root) / "data" / "mcp_clients.yaml").stat().st_mtime_ns
    except OSError:
        return None


def _sanitize_schema_patterns(schema: dict[str, Any]) -> None:
    # [2026-09-29] Why: MCP servers may publish regex `pattern` values with
    # escapes that are invalid in ECMA-262 (observed: "^agent\\_run\\_" from
    # the exa server — markdown escaping leaking into a machine field).
    # OpenAI-compatible providers such as DeepSeek compile patterns and reject
    # the whole request on failure, while Anthropic treats schemas as opaque,
    # so the defect only surfaces on fallback. How: walk the schema, rewrite
    # `\_` to `_` (identical literal match), then drop any pattern that still
    # fails to compile locally. Purpose: one upstream defect must not break an
    # entire channel; sanitizing at registration covers every provider at one
    # place. Dropping is transport-safe because pattern is advisory only.
    stack: list[Any] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in list(node.items()):
                if key == "pattern" and isinstance(value, str):
                    fixed = value.replace("\\_", "_")
                    try:
                        re.compile(fixed)
                    except re.error:
                        logger.warning(
                            "MCP tool schema pattern dropped (invalid regex): %r", value
                        )
                        del node[key]
                        continue
                    if fixed != value:
                        logger.info(
                            "MCP tool schema pattern sanitized: %r -> %r", value, fixed
                        )
                    node[key] = fixed
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(item for item in node if isinstance(item, (dict, list)))


async def refresh_mcp_tools(registry: Any) -> int:
    """重发现 MCP 客户端并把其工具注册为一等工具。

    先逆序调用上一轮全部 disposer（配置删除的客户端随之摘除），再重新
    发现注册。注册走 register_builtin_tool：写入 reload 快照，且 disposer
    归档进 disposal ledger（插件卸载可逆）。
    """
    lock = _BRIDGE.get("lock")
    if lock is None:
        lock = _BRIDGE["lock"] = asyncio.Lock()
    async with lock:
        for dispose in _BRIDGE["disposers"]:
            try:
                dispose()
            except Exception:
                pass
            else:
                forget = getattr(_BRIDGE.get("hooks"), "remove_plugin_disposer", None)
                if callable(forget):
                    forget(dispose)
        _BRIDGE["disposers"].clear()

        count = 0
        ws = Path(getattr(registry, "workspace_root"))
        try:
            clients = mcp_runtime.list_clients(ws)
        except Exception:
            return 0

        for client in clients:
            if not isinstance(client, dict):
                continue
            cid = str(client.get("id") or "").strip()
            if not cid or not client.get("enabled", True):
                continue
            try:
                result = await mcp_runtime.list_tools(ws, cid)
                # 兼容统一 data 包装与旧顶层两种 schema（自旧 load_mcp_tools 原样保留）。
                result_data = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else {}
                tools = result_data.get("tools") if isinstance(result_data.get("tools"), list) else (result.get("tools") if isinstance(result, dict) else [])
                if not isinstance(tools, list):
                    continue
            except Exception:
                logger.warning("MCP client '%s' tool list unavailable, skipping", cid)
                continue

            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                raw_name = str(tool.get("name") or "").strip()
                if not raw_name:
                    continue
                reg_name = f"mcp_{cid}_{raw_name}"
                desc = str(tool.get("description") or "").strip()
                schema = tool.get("input_schema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}, "required": []}
                _sanitize_schema_patterns(schema)
                _mcp_pure = bool(
                    (isinstance(tool.get("annotations"), dict) and tool["annotations"].get("readOnlyHint"))
                    or any(k in raw_name.lower() for k in (
                        "search", "query", "list", "get_", "fetch", "resolve",
                        "current_time", "relative_time", "days_in_month",
                        "get_timestamp", "convert_time", "get_week_year",
                        "db_info", "build_prompt", "rea_search", "rea_schema",
                        "jsr_list", "jsr_schema",
                    ))
                )
                dispose = registry.register_builtin_tool(
                    reg_name,
                    f"[MCP:{cid}] {desc}" if desc else f"[MCP:{cid}] {raw_name}",
                    schema,
                    _make_mcp_tool(ws, cid, raw_name),
                    pure=_mcp_pure,
                )
                _BRIDGE["disposers"].append(dispose)
                hooks = _BRIDGE.get("hooks")
                add = getattr(hooks, "add_plugin_disposer", None)
                if callable(add):
                    try:
                        add("mcp", dispose)
                    except Exception:
                        pass
                count += 1
        return count


def _make_mcp_tool(workspace_root: Path, client_id: str, tool_name: str) -> Callable:
    """可取消的 MCP 工具调用包装（自 toolbox/registry.py 原样迁入）。"""

    async def _call(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        # Why: MCP tool calls previously had no cancel support — the
        # engine loop only checks cancel between tools, not during a tool's await.
        # How: run the MCP call in a background task and poll check_cancelled every
        # 2 seconds. If cancel is detected, cancel the background task and return a
        # cancelled result. Purpose: users can stop stuck MCP calls without restarting.
        try:
            mcp_task = asyncio.ensure_future(
                mcp_runtime.call_tool(workspace_root, client_id, tool_name, args)
            )
            while not mcp_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(mcp_task), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
                if mcp_task.done():
                    break
                # Check cancel
                if hasattr(ctx, "check_cancelled") and await ctx.check_cancelled():
                    mcp_task.cancel()
                    try:
                        await mcp_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    return {"ok": False, "error": f"MCP tool '{tool_name}' cancelled by user", "cancelled": True}
            return mcp_task.result()
        except asyncio.CancelledError:
            return {"ok": False, "error": f"MCP tool '{tool_name}' cancelled", "cancelled": True}
        except Exception as e:
            return _err_tool(str(e), mcp_client=client_id, mcp_tool=tool_name)

    return _call


def _err_tool(message: str, **fields: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, "data": {"result": f"ERROR: {message}", **fields}}


# ---------------------------------------------------------------------------
#  supervisor 进程：admin/config 配置端点
# ---------------------------------------------------------------------------

class RawContent(BaseModel):
    content: str


def _workspace_root(request: Request) -> Path:
    return Path(request.app.state.state.workspace_root)


def list_mcp_clients_endpoint(request: Request) -> list[dict[str, Any]]:
    ws = _workspace_root(request)
    p = ws / "data" / "mcp_clients.yaml"
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    clients = data.get("clients") if isinstance(data, dict) else None
    if not isinstance(clients, dict):
        return []
    res: list[dict[str, Any]] = []
    for cid, spec in sorted(clients.items()):
        if not isinstance(spec, dict):
            continue
        item: dict[str, Any] = {"id": str(cid)}
        item.update(spec)
        res.append(item)
    return res


def get_mcp_clients_raw_endpoint(request: Request) -> dict[str, str]:
    p = _workspace_root(request) / "data" / "mcp_clients.yaml"
    if not p.exists():
        return {"content": "version: 1\nclients: {}\n"}
    return {"content": p.read_text(encoding="utf-8")}


def update_mcp_clients_raw_endpoint(payload: RawContent, request: Request) -> dict[str, Any]:
    p = _workspace_root(request) / "data" / "mcp_clients.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload.content, encoding="utf-8")
    return {"ok": True}


def _register_admin_routes(routes: Any) -> None:
    from engine.faces.routes import static_router

    router = APIRouter()
    router.add_api_route("/mcp-clients", list_mcp_clients_endpoint, methods=["GET"])
    router.add_api_route("/mcp-clients/raw", get_mcp_clients_raw_endpoint, methods=["GET"])
    router.add_api_route("/mcp-clients/raw", update_mcp_clients_raw_endpoint, methods=["PUT"])
    routes.register(router, mount="admin/config", description="MCP 客户端配置端点")

    # 面板与依赖资源需要鉴权；宿主用 Bearer 换取短期静态资源 Cookie。
    client = APIRouter()
    client.include_router(
        static_router(Path(__file__).parent / "web"),
        prefix="/web",
    )
    routes.register(client, description="MCP 设置面板静态资源（需鉴权）")


# ---------------------------------------------------------------------------
#  Handler 与 PLUGIN_META
# ---------------------------------------------------------------------------

class McpPlugin:
    """按可用 face 分流注册：supervisor 挂配置端点，engine 启动工具桥。"""

    name = "mcp"
    priority = 50

    def __init__(self, ctx: Any = None) -> None:
        contributions = getattr(ctx, "contributions", None)
        get_face = getattr(contributions, "get", lambda _n: None)
        routes = get_face("routes")
        tools = get_face("tools")
        if routes is not None:
            _register_admin_routes(routes)
        if tools is not None:
            _start_engine_bridge(tools, ctx)


PLUGIN_META = {
    "handler_class": "McpPlugin",
    "hook_points": [],
    "priority": 50,
    "wants_context": True,
    "description": "MCP 客户端工具桥：动态工具发现（engine）、配置 CRUD 元工具、admin/config 端点与设置面板（supervisor）",
    "author": "core",
    "web": {
        "panels": [
            {
                "id": "settings",
                "slot": "settings",
                "order": 10,
                "title": "MCP",
                "icon": "cable",
                "entry": "/v1/plugins/mcp/web/",
            }
        ],
    },
    "tools": [
        {
            "name": "create_or_update_mcp_client",
            "description": "Create or update an MCP client config in data/mcp_clients.yaml. Supports stdio, sse, and streamable_http transports. New tools go live immediately on this worker; other workers follow within seconds.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "transport": {"type": "string", "enum": ["stdio", "sse", "streamable_http", "streamable-http", "http"]},
                    "command": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "env": {"type": "object"},
                    "url": {"type": "string"},
                    "headers": {"type": "object"},
                },
                "required": ["id", "transport"],
            },
            "func": create_or_update_mcp_client,
        },
        {
            "name": "list_mcp_clients",
            "pure": True,
            "description": "List configured MCP clients.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
            "func": list_mcp_clients,
        },
        {
            "name": "delete_mcp_client",
            "description": "Delete an MCP client config by id. Its tools are removed from this worker immediately; other workers follow within seconds.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                },
                "required": ["id"],
            },
            "func": delete_mcp_client,
        },
    ],
}
