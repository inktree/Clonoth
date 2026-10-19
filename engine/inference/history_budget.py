"""历史 tool 结果的累计预算控制。

问题背景：
    ``tool_step.truncate_tool_result`` 只限制**单条** tool 结果的字符数。
    但历史会累积——十几条 50K 的 grep/list_dir 结果就能堆到 50 万字符，
    每个请求都全量重发，最终把上游 provider 的请求体撑爆
    （表现为 ``finish_reason=MALFORMED_RESPONSE``，部分代理会误报为 safety filter）。

解决思路：
    在构造发给 LLM 的消息列表时，对所有历史 tool 结果施加一个**共享总预算**。
    优先保留最近的 tool 结果（最相关），预算用尽后把更早的结果压缩到保底长度。

关键约束：
    **只裁剪 content，绝不删除消息。**
    assistant.tool_calls 与 role=tool 结果必须严格一一配对，
    删除任何一条都会让 provider 报 400。因此这里的做法是把
    旧结果替换为「首部片段 + 截断说明」，消息本身继续保留。

    裁剪只作用于发出去的副本，不改 ``ls.messages`` 运行时状态，
    完整内容仍可从 ConversationStore / artifact 文件回溯。
"""
from __future__ import annotations

from typing import Any, Mapping

# 历史 tool 结果总预算（字符）。约 15 万字符 ≈ 5~7 万 token，
# 留足空间给 system prompt、对话正文和本轮新产生的工具输出。
_DEFAULT_HISTORY_TOOL_BUDGET = 150_000

# 被压缩的旧 tool 结果至少保留多少字符，保证模型还能看出这条是什么。
_DEFAULT_FLOOR_CHARS = 1_200

# 单条结果超过这个长度才值得压缩，避免在短消息上做无意义的字符串操作。
_MIN_COMPRESS_CHARS = 2_000


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def get_history_tool_budget(config: Mapping[str, Any] | None) -> int:
    """从 runtime 配置读取历史 tool 结果总预算。"""
    tool_trace = (config or {}).get("engine", {}).get("tool_trace", {})
    if not isinstance(tool_trace, Mapping):
        return _DEFAULT_HISTORY_TOOL_BUDGET
    return _coerce_positive_int(
        tool_trace.get("max_history_tool_chars"), _DEFAULT_HISTORY_TOOL_BUDGET
    )


def _is_tool_result_message(msg: Mapping[str, Any]) -> bool:
    """判断一条存储层消息是否为工具结果。"""
    if msg.get("role") == "tool":
        return True
    meta = msg.get("_meta")
    if isinstance(meta, Mapping) and meta.get("message_type") == "tool_result":
        return True
    return False


def _content_text(msg: Mapping[str, Any]) -> str:
    """取出消息的文本内容（兼容 str 和 multimodal list）。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(p.get("text") or "")
            for p in content
            if isinstance(p, Mapping) and p.get("text")
        ]
        return "".join(parts)
    return ""


def bound_tool_history(
    messages: list[dict],
    *,
    total_budget: int = _DEFAULT_HISTORY_TOOL_BUDGET,
    floor_chars: int = _DEFAULT_FLOOR_CHARS,
) -> tuple[list[dict], dict[str, Any]]:
    """对历史 tool 结果施加共享总预算，返回 (新消息列表, 统计信息)。

    从最新往旧遍历：预算充足时原样保留；预算不足时把旧结果压缩到 floor_chars。
    只复制被修改的消息，其余沿用原对象引用，避免无谓的内存拷贝。

    返回的统计信息用于日志/事件观测，不含敏感内容。
    """
    stats: dict[str, Any] = {
        "budget": int(total_budget),
        "before_chars": 0,
        "after_chars": 0,
        "compressed_count": 0,
    }

    if not messages:
        return messages, stats

    # 先统计当前总量，用于判断是否需要动手
    sizes: list[tuple[int, int]] = []  # (index, len)
    for idx, msg in enumerate(messages):
        if not _is_tool_result_message(msg):
            continue
        text = _content_text(msg)
        if len(text) < _MIN_COMPRESS_CHARS:
            continue
        sizes.append((idx, len(text)))

    for _, size in sizes:
        stats["before_chars"] += size
    stats["after_chars"] = stats["before_chars"]

    if not sizes or stats["before_chars"] <= total_budget:
        return messages, stats

    # 从新到旧发放预算
    remaining = int(total_budget)
    # 记录每条 tool 结果允许保留的字符上限
    allowance: dict[int, int] = {}
    for idx, size in reversed(sizes):
        if remaining >= size:
            allowance[idx] = size
            remaining -= size
        else:
            # 预算不够了：这条以及更早的全都压到 floor
            allowance[idx] = max(1, min(floor_chars, size))

    # 应用裁剪
    result: list[dict] = list(messages)
    for idx, size in sizes:
        allow = allowance.get(idx, size)
        if allow >= size:
            continue
        text = _content_text(messages[idx])
        omitted = size - allow
        notice = (
            f"\n\n...[历史工具输出已压缩，省略 {omitted:,} 字符。"
            f"如仍需要完整内容，请用 read_file 或 grep 重新读取对应文件。]"
        )
        new_msg = dict(messages[idx])
        new_msg["content"] = text[:allow] + notice
        result[idx] = new_msg
        stats["after_chars"] -= omitted
        stats["compressed_count"] += 1

    return result, stats
