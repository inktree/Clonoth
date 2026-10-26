"""伪工具 spec 构建器和工具列表 helpers。

从 ai_step.py 中拆出，这些函数零外部依赖，纯数据结构构建。
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..node import Node


# ---------------------------------------------------------------------------
#  v3 伪工具名称
# ---------------------------------------------------------------------------

# [AutoC 2026-05-31] Why: ask is introduced as a terminal pseudo tool so
# nodes can explicitly request upstream clarification before workflow topology
# routing exists. How: include ask in the static pseudo-tool set next to finish.
# Purpose: route ask through pseudo_handlers instead of rejecting it as an
# unauthorized real tool.
# [AutoC 2026-09-12] Route spawn_worker through the pseudo-tool layer.
# Why: anonymous workers are control flow, not ToolRegistry implementations. How:
# add the stable name beside other static pseudo tools. Purpose: model calls reach
# pseudo_handlers instead of being rejected as unauthorized real tools.
_PSEUDO_TOOL_NAMES = frozenset({"finish", "ask", "intermediate_reply", "switch_node", "compact_context", "preempt_task", "spawn_worker", "manage_workers"})

# [2026-05-04] Dynamic delegate dispatch tools use a stable prefix plus target id.
# Why: the removed aggregate dispatch tools hid delegate choices behind one
# schema. How: keep only static pseudo-tool names in _PSEUDO_TOOL_NAMES and
# recognize dispatch:{target_id} through this prefix. Purpose: route dynamic tools
# through the existing pseudo-tool execution path without hard-coding node ids or
# reintroducing removed aggregate tools.
DISPATCH_TOOL_PREFIX = "dispatch_to_"


def _is_pseudo_tool_name(name: str) -> bool:
    """Return whether ``name`` is handled by the engine pseudo-tool layer."""
    tool_name = str(name or "").strip()
    # [2026-05-04] The dynamic dispatch pattern requires a concrete target id.
    # Why: dispatch: without a suffix cannot be executed by pseudo_handlers.
    # How: keep exact static-name matching and accept only dispatch:{target_id}
    # with a non-empty suffix. Purpose: avoid treating malformed dynamic names as
    # pseudo tools while preserving all valid per-target dispatch tools.
    return tool_name in _PSEUDO_TOOL_NAMES or (
        tool_name.startswith(DISPATCH_TOOL_PREFIX)
        and bool(tool_name[len(DISPATCH_TOOL_PREFIX):].strip())
    )


def allowed_pseudo_names(openai_tools: list[dict], node: Any = None) -> set[str]:
    """Derive the authorized pseudo-tool set from the composed tool list.

    [fix 2026-09-03] Why: pseudo calls were routed by static name only, so a model
    could invoke a control tool that was never injected for this node — e.g. a
    system node hallucinating switch_node from the task log it summarizes — and
    the handler executed it, switching the session entry. How: the authorized set
    is exactly what build_node_tool_specs injected. finish is the protocol
    terminator: hybrid mode injects no finish spec, but node prompts (e.g.
    system.compactor) still end tasks via a finish call, so finish stays allowed
    unless the node explicitly excluded it via control_tools.
    """
    names: set[str] = set()
    for spec in openai_tools or []:
        if not isinstance(spec, dict):
            continue
        name = str((spec.get("function") or {}).get("name") or "").strip()
        if name and _is_pseudo_tool_name(name):
            names.add(name)
    ct = getattr(node, "control_tools", "all") if node is not None else "all"
    if isinstance(ct, str):
        if ct.strip().lower() != "none":
            names.add("finish")
    elif isinstance(ct, list):
        if "finish" in {str(x).strip() for x in ct}:
            names.add("finish")
    return names


import re as _re

_TOOL_NAME_SANITIZE_RE = _re.compile(r'[^a-zA-Z0-9_-]')
# Reverse mapping: sanitized tool name suffix → original node id
_DISPATCH_TOOL_REVERSE: dict[str, str] = {}


def _sanitize_node_id_for_tool(node_id: str) -> str:
    """Sanitize a node id to comply with OpenAI tool name pattern [a-zA-Z0-9_-]."""
    return _TOOL_NAME_SANITIZE_RE.sub('_', str(node_id or '').strip())


def _dispatch_tool_name(target_id: str) -> str:
    """Build the public per-target dispatch tool name."""
    raw = str(target_id or '').strip()
    sanitized = _sanitize_node_id_for_tool(raw)
    name = f"{DISPATCH_TOOL_PREFIX}{sanitized}"
    _DISPATCH_TOOL_REVERSE[sanitized] = raw
    return name


def _dispatch_target_from_tool_name(tool_name: str) -> str:
    """Extract the original target id from a dispatch-{sanitized_id} tool name."""
    name = str(tool_name or "").strip()
    if not name.startswith(DISPATCH_TOOL_PREFIX):
        return ""
    sanitized_suffix = name[len(DISPATCH_TOOL_PREFIX):].strip()
    # Reverse lookup: sanitized → original node id
    # [AutoC 2026-09-12] Remove the duplicate return that followed this one.
    # Why: it was unreachable and suggested two competing dispatch-name formats.
    # How: keep only the reverse-map-aware result. Purpose: target resolution has
    # one explicit return path and static analysis no longer reports dead code.
    return _DISPATCH_TOOL_REVERSE.get(sanitized_suffix, sanitized_suffix)

# [2026-05-07] 控制流伪工具集合。
# 原因：finish 只表示“结束当前任务并提交结果”，不是可长期回放的业务工具。
# 做法：把控制流工具名集中声明，供运行期、存储层和 L2 历史构造层共用。
# 目的：普通工具仍保留 provider 所需配对，finish 不再污染长期历史。
# [AutoC 2026-05-31] Why: ask has the same terminal control-flow shape as
# finish in Phase 0. How: classify both names as control tools for shared history
# repair and future routing helpers. Purpose: keep control-flow bookkeeping
# consistent when ask appears in stored tool turns.
CONTROL_TOOL_NAMES = frozenset({"finish", "ask"})


def _is_control_tool_name(name: str) -> bool:
    """Return whether ``name`` is an engine control-flow tool."""
    return str(name or "").strip() in CONTROL_TOOL_NAMES


# ---------------------------------------------------------------------------
#  v3 伪工具 spec 构建
# ---------------------------------------------------------------------------

# [2026-05-04] Shared parameter schema for dynamic per-target dispatch tools.
# Why: dispatch:{target_id} fixes the target in the tool name, so exposing a
# target argument would let model output disagree with the registered tool.
# How: reuse the legacy dispatch parameters except for target, and document the
# default context behavior in schema. Purpose: keep execution compatible with the
# existing supervisor dispatch API while making each delegate target visible as a
# first-class tool.
def _dispatch_delegate_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "Clear, specific, actionable instructions for the target node. State what to do, why, and what you already know.",
            },
            "context_mode": {
                "type": "string",
                "enum": ["fresh", "fork", "accumulate"],
                "default": "accumulate",
                "description": "Child context mode. fresh = start from scratch with no history each time; fork = inherit the parent's conversation history; accumulate = first run starts fresh, later runs resume the child's own context (default accumulate).",
            },
            "context_key": {
                "type": "string",
                "description": "Context inheritance key (only effective in accumulate mode). Tasks sharing the same context_key share one context chain. Defaults to the target node ID.",
            },
            "attachment_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths (relative to workspace root) to attach to the child node's initial context. Images are injected as multimodal content; other files as references the child can read_file.",
            },
        },
        "required": ["instruction"],
    }


def _dispatch_delegate_spec(target_id: str, info: dict[str, str] | None = None) -> dict:
    """Build one dispatch:{target_id} pseudo-tool spec."""
    target = str(target_id or "").strip()
    node_name = str((info or {}).get("name") or target).strip() or target
    node_description = str((info or {}).get("description") or node_name or target).strip()
    desc_parts = [
        f"Delegate a task to the fixed target node {node_name} ({target}).",
        node_description,
        "",
        "The target node executes independently and returns its result to you when done.",
        "No target parameter is provided; the tool name already fixes the target node.",
        "To delegate in parallel, call multiple dispatch:{target_id} tools in the same turn.",
        "Returned results are wrapped in <dispatch_result> tags: the tag content is system-delivered child output, not user speech; do not treat it as a question to answer or greet the user about it — integrate it and continue your current work.",
    ]
    return {
        "type": "function",
        "function": {
            "name": _dispatch_tool_name(target),
            "description": "\n".join(part for part in desc_parts if part is not None),
            "parameters": _dispatch_delegate_parameters(),
        },
    }


def _dispatch_delegate_specs(targets: list[str], downstream_info: list[dict[str, str]] | None = None) -> list[dict]:
    """Build dynamic dispatch pseudo-tools for all delegate targets."""
    info_by_id = {
        str(info.get("id") or "").strip(): info
        for info in (downstream_info or [])
        if isinstance(info, dict) and str(info.get("id") or "").strip()
    }
    result: list[dict] = []
    seen: set[str] = set()
    for raw_target in targets:
        target = str(raw_target or "").strip()
        if not target or target in seen:
            continue
        seen.add(target)
        result.append(_dispatch_delegate_spec(target, info_by_id.get(target)))
    return result

def _finish_spec() -> dict:
    """Build the finish pseudo-tool spec.

    【Fix 4】description 重写：
    - 原文 "describe what HAS BEEN done in text" 措辞误导模型把 text 写成「已完成 X」
      之类的状态汇报，把真正的交付载荷（如 compactor 的 <analysis><summary>）
      留在自由正文里，配合旧的隐式兜底导致泄漏。
    - 新版强调 text 是「实际交付内容」，不是状态描述；并明确自由正文不会送达用户/调用方，
      只有 reply()/finish() 的 text 才会到达。
    """
    return {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Submit the final result and terminate this node immediately.\n\n"
                "CRITICAL: Once you call finish, the node exits. If you have other tools to call (like execute_command or save_memory), you MUST wait for them to complete (`ok`) before calling finish.\n\n"
                "⚠️ finish MUST be called ALONE — never in the same turn as other tools (except reply). "
                "If you call finish alongside execute_command, save_memory, a dispatch tool, or any other tool, ALL calls will be REJECTED and you must retry. "
                "Always: execute tools first → wait for results → then call finish separately.\n\n"
                "Do NOT send your final report via `reply` while waiting for tools. Just call the tools silently, wait for the next turn, and put your ENTIRE final report in the `finish` tool's text.\n\n"
                "The `text` parameter is the ACTUAL DELIVERABLE — the content the caller "
                "or user will receive. Put your real output here (full answer, full data, "
                "full summary), not a status report of what you did. If the caller expects "
                "a structured payload, put the entire structured payload in `text`.\n\n"
                "Free prose outside of tool calls is NOT delivered to the caller or user. "
                "Only `reply()` and `finish()` reach the recipient.\n\n"
                "Use cases:\n"
                "- Task completed: put the deliverable content in `text`.\n"
                "- Need more information: put your specific question in `text`. "
                "The caller or user will see it and respond.\n\n"
                "Parameters:\n"
                "- text: The actual deliverable content, or a question if you need more info.\n"
                "- attachment_paths: File paths to attach (optional).\n\n"
                "Do not include internal protocol markers or debug info in text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The final result text, or a specific question when more information is needed.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of accompanying file paths (use only when there are attachments such as images).",
                    },
                },
                "required": ["text"],
            },
        },
    }


def _ask_spec() -> dict:
    """Build the ask pseudo-tool spec.

    [AutoC 2026-05-31] Why: Phase 0 needs an explicit ask exit even though the
    supervisor still routes it like finish. How: expose a finish-shaped terminal
    tool without attachments and with clarification-oriented text. Purpose: let
    nodes signal “need more input” now, while preserving action="ask" for Phase 1
    topology routing.
    """
    return {
        "type": "function",
        "function": {
            "name": "ask",
            "description": (
                "Ask the upstream (caller or user) for additional information or clarification. "
                "This terminates the current task, similar to finish, but signals that you need "
                "more input before you can complete your work.\n\n"
                "Use cases:\n"
                "- You need clarification on requirements before proceeding.\n"
                "- You've hit a blocker and need the caller's decision.\n"
                "- You want to confirm your approach before investing effort.\n\n"
                "The upstream will see your question and can respond, which will create a new "
                "task for you to continue working.\n\n"
                "Parameters:\n"
                "- text: Your specific question or request for information."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Your question or request for additional information.",
                    },
                },
                "required": ["text"],
            },
        },
    }



def _reply_spec() -> dict:
    """Build the intermediate_reply pseudo-tool spec (non-terminating).

    [AutoC 2026-08-06] Renamed from reply to intermediate_reply.
    Sends an interim message to the user without terminating the task.
    """
    return {
        "type": "function",
        "function": {
            "name": "intermediate_reply",
            "description": (
                "Send an interim message to the user WITHOUT terminating this node.\n\n"
                "The node keeps running after this call — you can continue calling tools "
                "or do more work in subsequent turns.\n\n"
                "The `text` parameter is the message content the user will read. "
                "Anything you want the user to see mid-task must go here.\n\n"
                "When to use:\n"
                "- You have a partial result or progress update to share, but more work remains.\n"
                "- The user asked for multiple things and you want to respond to one "
                "while continuing to work on the rest.\n\n"
                "When NOT to use:\n"
                "- You are done with all work → just say your final answer directly (free prose).\n"
                "- You need more information from the user → use ask instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The message content the user will read.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of workspace-relative file paths to attach. "
                            "Files will be sent alongside the text message."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
    }


def _compact_context_spec() -> dict:
    """伪工具：手动触发上下文压缩。"""
    return {
        "type": "function",
        "function": {
            "name": "compact_context",
            "description": (
                "Compress the current conversation context by summarizing older messages.\n\n"
                "Use when context is getting very long and you want to free up token budget. "
                "Recent messages are preserved; older messages are replaced with a structured summary.\n\n"
                "Calling this suspends the current step to run the compaction sub-task and automatically "
                "resumes this node once compression finishes.\n"
                "⚠️ compact_context MUST be called ALONE in its own turn — never alongside other tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keep_recent": {
                        "description": "Number of recent conversation rounds to preserve (default uses system config, typically 6).",
                        "type": "integer",
                    },
                },
                "required": [],
            },
        },
    }


def _preempt_task_spec() -> dict:
    """伪工具：向正在运行的子任务注入追加指令。"""
    return {
        "type": "function",
        "function": {
            "name": "preempt_task",
            "description": (
                "Inject an additional instruction into a running child task (or gracefully preempt it if message is omitted). "
                "The child task will finish its current atomic operation (e.g. tool call), receive your message as a new "
                "user instruction, and continue working with the updated direction.\n\n"
                "This is NOT for hard-cancelling tasks — use cancel_active_tasks for that.\n\n"
                "Use cases:\n"
                "- You need to add corrections or supplementary instructions to a running child.\n"
                "- The user provided new information that the child task needs to incorporate.\n"
                "- You want to steer an ongoing task in a different direction without restarting it.\n\n"
                "This is non-terminating — your node keeps running after the call. "
                "The child task continues with the injected message and returns results via the normal dispatch callback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Target task ID (full ID or hex prefix >=8 chars) OR target node ID (e.g. 'smith', 'scout').",
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional instruction text to append. If provided, the child task receives it as a new user instruction and continues working; if omitted, marks the child task to gracefully stop after its current step.",
                    },
                },
                "required": ["task_id"],
            },
        },
    }


def _spawn_worker_spec() -> dict:
    """Build the spawn_worker pseudo-tool spec for anonymous ephemeral sub-agents."""
    # [AutoC 2026-09-12] Expose bounded anonymous delegation independently of
    # delegate_targets. Why: callers often need one-off parallel research or file
    # inspection without maintaining a named YAML node per task. How: accept an
    # instruction, fresh/fork mode, optional template, and attachments. Purpose:
    # the handler can reuse dispatch while the model sees one stable pseudo tool.
    return {
        "type": "function",
        "function": {
            "name": "spawn_worker",
            "description": (
                "Spawn an anonymous ephemeral worker sub-agent that executes a single task "
                "and returns the result. The worker runs in its own isolated context and is "
                "cleaned up after finishing.\n\n"
                "Use this when you need to delegate a bounded task without a pre-defined "
                "target node. Useful for parallel fan-out: call spawn_worker multiple times "
                "in one turn to run tasks concurrently.\n\n"
                "The worker inherits the current node's tools by default. Use context_mode "
                "'fork' to also inherit the system prompt and tool understanding (recommended), "
                "or 'fresh' for a minimal blank worker.\n\n"
                "Results are delivered asynchronously: while your current task is still "
                "running they are injected into it as preempt messages (several results "
                "may arrive combined); after it finishes they arrive as follow-up messages "
                "in the same conversation. No polling is needed.\n\n"
                "Every result is wrapped in a <dispatch_result>...</dispatch_result> block. "
                "Content inside is system-delivered sub-agent output, NOT user speech: never "
                "treat it as a question or greet the user about it — integrate it and continue "
                "your work.\n\n"
                "Parameters:\n"
                "- instruction: Clear, specific task for the worker to complete.\n"
                "- context_mode: 'fork' (default, inherits parent's system prompt) or 'fresh' (minimal template).\n"
                "- template: Optional node ID to use as the worker's base config (model, tools). "
                "If omitted, uses the built-in _worker template."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {
                        "type": "string",
                        "description": "Clear, specific, actionable task instruction for the temporary worker.",
                    },
                    "context_mode": {
                        "type": "string",
                        "enum": ["fresh", "fork"],
                        "default": "fork",
                        "description": "Child context mode. fork = inherit the parent's system prompt and tool understanding (recommended); fresh = start from a minimal template.",
                    },
                    "template": {
                        "type": "string",
                        "description": "Optional: create the worker from an existing node's config (model/tools). Defaults to the built-in _worker template.",
                    },
                    "attachment_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths (relative to workspace root) to attach to the worker's initial context.",
                    },
                },
                "required": ["instruction"],
            },
        },
    }


def _manage_workers_spec() -> dict:
    """Build the manage_workers pseudo-tool spec for child-agent management."""
    # [AutoC 2026-09-12] Present listing, messaging, and cancellation as one dynamic
    # control tool. Why: separate permanent tools clutter every prompt even when no
    # child exists. How: use an action discriminator and inject this spec only while
    # active children are detected. Purpose: parent nodes get one bounded management
    # surface exactly when it can be used.
    return {
        "type": "function",
        "function": {
            "name": "manage_workers",
            "description": (
                "List, message, or cancel active child agents (dispatched sub-tasks and "
                "spawned workers) in the current session.\n\n"
                "This tool only appears when there are active child tasks running.\n\n"
                "Actions:\n"
                "- list: Show all active child tasks with their status, node, and elapsed time.\n"
                "- message: Inject an additional instruction into a running child task "
                "(the child continues working with the updated direction).\n"
                "- cancel: Cancel a specific child task by task_id, or cancel all if task_id is omitted.\n\n"
                "Parameters:\n"
                "- action: Required. One of 'list', 'message', 'cancel'.\n"
                "- task_id: Required for 'message', optional for 'cancel' (omit to cancel all).\n"
                "- message: Required for 'message' action. The instruction to inject."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "message", "cancel"],
                        "description": "Action type: list = view active child tasks, message = inject an instruction into a child task, cancel = cancel child tasks.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Target task ID (full or prefix). Required for message; optional for cancel (omit = cancel all).",
                    },
                    "message": {
                        "type": "string",
                        "description": "Instruction text to inject into the child task (message action only).",
                    },
                },
                "required": ["action"],
            },
        },
    }


def _switch_node_spec(targets: list[str], switch_info: list[dict[str, str]] | None = None, *, current_node_id: str = "", current_node_name: str = "") -> dict:
    """构建 switch_node 伪工具定义——切换 session 的对话节点。"""
    desc_parts = [
        "Switch the conversation's dialogue node. The current node finishes on call; the user's next message is handled by the target node.",
        "",
        "When to use:",
        "- The user explicitly asks to switch to a node (e.g. 'switch to the coding node')",
        "- The current node is insufficient and another node should take over the conversation",
        "",
        "Notes:",
        "- The current node terminates immediately after the call; no further tool calls run",
        "- The target node can see the prior conversation history",
        "- Passing an empty string as target restores the default entry node",
    ]
    if switch_info:
        desc_parts.append("")
        desc_parts.append("Switchable nodes:")
        for info in switch_info:
            desc_parts.append(f"- {info.get('name', info['id'])} ({info['id']}): {info.get('description', '')}")
    # [2026-05-06] switch_node 的枚举必须与 Gemini 的 schema 规则兼容。
    # Why: target="" 是运行时恢复默认入口节点的约定，但 Gemini 会拒绝 enum
    # 中的空字符串成员。How: 只把非空目标节点 ID 去重后写入 enum，没有可选
    # 目标时直接省略 enum。Purpose: 保留 handler 对空 target 的兼容，同时避免
    # 模型请求因工具声明无效而在进入推理前失败。
    enum_targets: list[str] = []
    for raw_target in targets:
        target = str(raw_target or "").strip()
        if target and target not in enum_targets:
            enum_targets.append(target)
    target_prop: dict = {
        "type": "string",
        "description": "Target node ID. Pass an empty string to restore the default entry node.",
    }
    if enum_targets:
        target_prop["enum"] = enum_targets
    return {
        "type": "function",
        "function": {
            "name": "switch_node",
            "description": "\n".join(desc_parts),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": target_prop,
                    "text": {
                        "type": "string",
                        "description": "Reply text for the user (e.g. 'Switched to the coding node').",
                    },
                },
                "required": ["target", "text"],
            },
        },
    }


# ---------------------------------------------------------------------------
#  工具 spec 相关
# ---------------------------------------------------------------------------

# [2026-04-23] 异步工具提示：当 spec 中 async_mode=True 时，在 description 末尾追加说明，
# 告知模型该工具为后台执行、结果通过 preempt 自动回传。从 commit 7d10197 恢复。
_ASYNC_TOOL_HINT = (
    "\n\n⚡ This is an async tool. Execution runs in background; "
    "result will be delivered automatically via preempt when ready. "
    "You can continue working or finish — no need to wait."
)


def _sanitize_schema_patterns(obj: Any) -> Any:
    # [2026-09-29] 修复 MCP 工具 schema 中 RE2 不兼容的正则转义。
    # Why: Exa MCP 的 agent_run 工具 runId 参数 pattern 为 "^agent\\_run\\_"，
    # JSON 解码后 `\_` 在 RE2（DeepSeek/Gemini 校验器）里不是合法转义，
    # 整个工具列表被拒。How: 递归遍历 schema，把 pattern 字段里 `_` 前的
    # 反斜杠去掉（下划线是普通字符无需转义）。Purpose: 第三方 MCP 工具
    # schema 在各 provider 间通用。
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if k == "pattern" and isinstance(v, str):
                out[k] = v.replace("\\_", "_")
            else:
                out[k] = _sanitize_schema_patterns(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_schema_patterns(x) for x in obj]
    return obj


def _to_openai_tools(specs: list[dict]) -> list[dict]:
    # [2026-04-23] 恢复 async_mode 支持：遍历 spec 列表，对 async_mode=True 的工具
    # 在 description 末尾追加 _ASYNC_TOOL_HINT，使模型知道该工具为异步执行。
    result = []
    for s in specs:
        desc = s.get("description", "")
        if s.get("async_mode"):
            desc += _ASYNC_TOOL_HINT
        result.append({
            "type": "function",
            "function": {
                "name": s["name"],
                "description": desc,
                "parameters": _sanitize_schema_patterns(s.get("input_schema", {"type": "object", "properties": {}, "required": []})),
            },
        })
    return result


def _filter_tool_specs(node: "Node", all_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mode = (node.tool_access.mode or "none").lower()
    if mode == "all":
        denied = set(node.tool_access.deny)
        if denied:
            return [s for s in all_specs if s.get("name") not in denied]
        return list(all_specs)
    if mode == "allowlist":
        allowed = set(node.tool_access.allow)
        return [s for s in all_specs if s.get("name") in allowed]
    return []


def build_node_tool_specs(
    *,
    registry: Any,
    node: "Node",
    downstream_info: list[dict[str, str]] | None = None,
    switch_info: list[dict[str, str]] | None = None,
    task_context: dict[str, Any] | None = None,
    has_active_children: bool = False,
) -> tuple[set[str], list[dict]]:
    """Build the model-visible tool list and the allowed real-tool name set.

    Why: tool-list composition (node filtering, dynamic dispatch expansion,
    switch_node gating, control-tool injection) is presentation policy, not
    loop mechanics. How: run the same filtering and spec-expansion chain that
    was inlined in run_ai_node and return both the authorization set and the
    OpenAI tool list. Purpose: the entry function composes instead of building.
    """
    tool_specs = _filter_tool_specs(node, registry.list_specs())
    allowed = {s.get("name") for s in tool_specs if s.get("name")}
    openai_tools = _to_openai_tools(tool_specs) if tool_specs else []

    delegate_targets = list(node.delegate_targets)
    if delegate_targets:
        # [2026-05-04] Register one dynamic dispatch tool per delegate target.
        # Why: target selection should happen through tool choice, not through an
        # aggregate dispatch schema. Purpose: keep dynamic dispatch intact.
        openai_tools.extend(_dispatch_delegate_specs(delegate_targets, downstream_info))

    # [AutoC 2026-08-30] control_tools 门控：节点可声明 "none"（不注入任何
    # 控制类伪工具）或名称列表（只注入列出的）。Why: 极简节点（如只有
    # execute_command 的命令执行节点）不应被框架强塞 ask/reply/switch 等。
    # How: 统一收口为一个判定函数；缺省 "all" 保持旧行为。Purpose: 节点的
    # 工具面完全由节点自己声明。
    _ct_raw = getattr(node, "control_tools", "all")
    if isinstance(_ct_raw, str):
        _ct_none = _ct_raw.strip().lower() == "none"
        _ct_allow: set[str] | None = None
    elif isinstance(_ct_raw, list):
        _ct_none = False
        _ct_allow = {str(x).strip() for x in _ct_raw if isinstance(x, str)}
    else:
        _ct_none = False
        _ct_allow = None

    def _ctl(name: str) -> bool:
        if _ct_none:
            return False
        return _ct_allow is None or name in _ct_allow

    # switch_node 仅对非系统节点注入（系统节点如 memory_extractor 不应切换入口）
    _is_system_task = bool((task_context or {}).get("is_system_task"))
    if not _is_system_task and _ctl("switch_node"):
        _sw_targets = [info["id"] for info in (switch_info or [])]
        openai_tools.append(_switch_node_spec(
            _sw_targets, switch_info,
            current_node_id=node.id, current_node_name=node.name,
        ))

    # [AutoC 2026-08-06] hybrid 模式下不注入 finish 工具：free prose 即隐式 finish。
    # tool_only 模式下保留原行为。
    _output_mode = getattr(node, 'output_mode', 'hybrid')
    if _output_mode == 'tool_only' and _ctl("finish"):
        openai_tools.append(_finish_spec())
    # [AutoC 2026-05-31] ask 在所有模式下都可用：节点需要向上游请求信息时使用。
    if _ctl("ask"):
        openai_tools.append(_ask_spec())
    if _ctl("intermediate_reply"):
        openai_tools.append(_reply_spec())
    if _ctl("compact_context"):
        openai_tools.append(_compact_context_spec())
    if _ctl("preempt_task"):
        openai_tools.append(_preempt_task_spec())
    # [AutoC 2026-09-12] Inject spawn_worker for every node whose control-tool policy
    # permits it, even when delegate_targets is empty. Why: anonymous delegation is
    # specifically intended to avoid predefined targets. How: compose it beside
    # preempt_task and compact_context. Purpose: all ordinary AI nodes can fan out
    # bounded work while control_tools="none" still produces a minimal tool surface.
    if _ctl("spawn_worker"):
        openai_tools.append(_spawn_worker_spec())
    # [AutoC 2026-09-12] Keep child-management schema out of idle prompts.
    # Why: list/message/cancel have no valid target until a child agent is active.
    # How: require the RunContext discovery signal in addition to control_tools.
    # Purpose: reduce prompt clutter while exposing management during active fan-out.
    if has_active_children and _ctl("manage_workers"):
        openai_tools.append(_manage_workers_spec())
    return allowed, openai_tools
