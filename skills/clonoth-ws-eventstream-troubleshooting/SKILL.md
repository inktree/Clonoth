---
name: clonoth-ws-eventstream-troubleshooting
description: Clonoth SDK WebSocket 事件流中断排查：websockets 版本兼容、适配器收不到事件、重启手段选择
enabled: true
strategy: normal
keywords:
- websockets
- extra_headers
- additional_headers
- WS connection failed
- 事件流
- connection open
- ws_connect
- clonoth_websockets
priority: 70
scan_depth: 1
---

# Clonoth 事件流中断排查手册

## 症状

引擎实际在正常跑（`data/events.jsonl` 有 inbound → task → stream_text），但适配器收不到事件回推：

- Discord 端看不到任何消息
- 控制台一直显示「输出中」
- 适配器日志里 `connection open` 出现 0 次

## 已修过的坑：websockets >= 14 头参数改名

`clonoth_sdk/client.py` 的 `ws_connect()` 里，websockets >= 14 把手握头参数从 `extra_headers` 改名为 `additional_headers`。用旧名会抛：

```
TypeError: BaseEventLoop.create_connection() got an unexpected keyword argument 'extra_headers'
```

已修复为按 websockets 主版本号动态选择参数名，并在 `requirements.txt` 补 `websockets>=12.0`。

## 判断链路是否活着（别踩探针陷阱）

SDK 的 `ws_connect` 生成器内部会把 ping 帧 `continue` 掉，所以**拿不到心跳**。

正确做法：用裸 `websockets.connect` 直连：

```
ws://127.0.0.1:8765/v1/ws
```

系统空闲时，等满 30 秒心跳周期（`_WS_HEARTBEAT_SEC=30.0`），收到 `{"type": "ping"}` 才证明链路正常。

## 重启手段的选择

改了 SDK 后必须重启适配器进程，Python 不热重载。

| 手段 | 是否可用 |
|---|---|
| `[BOT_RESTART]` 走适配器消息通道 | ❌ 事件流断时消息送不到，会死锁 |
| `request_restart`（引擎侧直连 supervisor API） | ✅ 绕开适配器，事件流断时唯一可靠手段 |
| `/v1/admin/restart target=all` | ✅ 重启 supervisor 并重新拉起全部子进程 |

## 排查顺序

1. 看适配器日志（`data/logs/discord-adapter-*.log`）有没有那个 `TypeError`
2. 看 `connection open` 计数是否为 0
3. 裸 WebSocket 直连测心跳
4. 确认 websockets 版本，必要时重装
5. 用 `request_restart` 重启（不要用 `BOT_RESTART`）
