"""Signal 数据类型定义。

Signal 是信号系统的核心数据结构，表示一个已发生的事件。
所有字段不可变（frozen），保证信号在传播过程中不会被篡改。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Signal:
    """一个不可变的信号事件。

    Attributes:
        name: 信号名称，使用点分层级命名（如 'llm.call.start'）。
        payload: 信号携带的数据，由发送方自行定义。
        ts: 信号创建时的 Unix 时间戳（秒），自动填充。
        trace_id: 可选的追踪 ID，用于关联同一请求链路上的多个信号。
        span_id: 可选的 span ID，由 SignalBus.span() 自动注入，
                 用于关联 start/end 配对信号。
    """
    name: str
    payload: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    trace_id: Optional[str] = None
    span_id: Optional[str] = None


def make_span_id() -> str:
    """生成一个短 span ID（UUID 前 8 位），用于 start/end 配对。"""
    return uuid.uuid4().hex[:8]
