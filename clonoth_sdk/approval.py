"""审批策略逻辑 — 去重与自动放行。"""
from __future__ import annotations

import asyncio

from .client import ClonothClient


class ApprovalTracker:
    """审批去重追踪器。

    维护已处理的审批 ID 集合，防止同一审批被 outbound_poller 多次处理。
    提取自 bot_adapter.py 的全局 _handled_approval_ids 集合。
    当集合超过 max_size 时自动清空，与原始行为一致（L1290 清理逻辑）。
    """

    def __init__(self, max_size: int = 500):
        self._handled: set[str] = set()
        self._max_size = max_size

    def is_handled(self, approval_id: str) -> bool:
        """检查审批是否已被处理过。"""
        return approval_id in self._handled

    def mark_handled(self, approval_id: str) -> None:
        """标记审批为已处理。超过上限时清空集合防止无限增长。"""
        if len(self._handled) > self._max_size:
            self._handled.clear()
        self._handled.add(approval_id)

    def clear(self) -> None:
        """手动清空去重集合。"""
        self._handled.clear()

    def __len__(self) -> int:
        return len(self._handled)


async def auto_approve(
    client: ClonothClient,
    approval_id: str,
) -> bool:
    """自动放行审批，失败重试。"""
    for attempt in range(2):
        try:
            ok = await client.approve(
                approval_id, decision="allow", comment="auto-approved by SDK",
            )
            if ok:
                return True
        except Exception:
            pass
        if attempt < 1:
            await asyncio.sleep(1)
    return False
