# Clonoth SDK

Pure protocol layer that encapsulates all communication between a Bot adapter and the Clonoth Supervisor. The SDK handles HTTP API calls, real-time WebSocket event streaming (`/v1/ws`), protocol state management, and approval deduplication/auto-approval — so adapters only need to implement platform-specific operations (sending messages, editing UI, etc.).

The SDK has **zero platform dependencies** (no discord.py, no telegram, etc.). Platform concerns live in the adapter.

## Architecture

Four layers, bottom to top:

| Layer | Class | Role |
|-------|-------|------|
| **HTTP + WS Client** | `ClonothClient` | Typed async methods wrapping Supervisor HTTP endpoints and WebSocket `/v1/ws` stream |
| **State** | `SessionState` | Centralized runtime state — triggers, session maps, channel context watermarks, and task states |
| **Event Router** | `EventRouter` | Streams `/v1/ws` events with exponential-backoff reconnect, dispatches protocol handlers, and notifies the adapter |
| **Callbacks** | `AdapterCallbacks` | `Protocol` interface — the adapter implements these to perform platform operations |

Supporting modules: `BotConfig` (configuration injection), `types` (dataclasses for API responses), `approval` (`ApprovalTracker` dedup + `auto_approve` retry logic; path trust classification is provided by Supervisor policy via `clonoth_runtime.classify_path`).

## Two-Layer Hook Architecture

```
  Supervisor event stream
        │
        ▼
  ┌─ Layer 1: on_raw_event hook ──────────────────────┐
  │  Adapter-registered interceptor.                   │
  │  Runs BEFORE SDK default processing.               │
  │  Return 'handled' → SDK skips this event.          │
  │  Return None → SDK continues to Layer 2.           │
  │  Use case: stream_delta animation, custom events.  │
  └────────────────────────────────────────────────────┘
        │
        ▼
  ┌─ SDK protocol processing ─────────────────────────┐
  │  Trigger matching, state updates, dedup,           │
  │  watermark advancement, approval classification.   │
  └────────────────────────────────────────────────────┘
        │
        ▼
  ┌─ Layer 2: AdapterCallbacks ───────────────────────┐
  │  SDK calls the appropriate callback method.        │
  │  Adapter performs platform I/O (send, edit, etc.). │
  └────────────────────────────────────────────────────┘
```

## Quick Start

```python
import asyncio
import sys
sys.path.insert(0, "/path/to/clonoth")

from clonoth_sdk import (
    BotConfig, ClonothClient, SessionState,
    EventRouter, AdapterCallbacks,
)

# 1. Configuration
config = BotConfig(
    base_url="http://127.0.0.1:8765",
    entry_node_id="my_main_node",
    conversation_key_prefix="discord",
)

# 2. Core objects
client = ClonothClient(config.base_url)
state = SessionState()

# 3. Implement AdapterCallbacks (all 15 async methods)
class MyAdapter:
    async def send_reply(self, trigger, text, attachments, *, main_state=None):
        print(f"Reply: {text}")
    # ... implement remaining 14 callbacks ...

callbacks = MyAdapter()

# 4. Create router and start event loop
router = EventRouter(client, state, callbacks, config)

# Optional: register Layer 1 hook for stream_delta handling
async def my_hook(event):
    if event.type == "stream_delta":
        # custom animation logic
        return "handled"  # skip SDK default
    return None  # let SDK handle

router.set_raw_event_hook(my_hook)

# 5. Run (blocks until cancelled or router.stop())
asyncio.run(router.run())
```

## File Inventory

| File | Description |
|------|-------------|
| `__init__.py` | Public API surface — re-exports all user-facing symbols |
| `client.py` | `ClonothClient` — async HTTP client + `ws_connect()` WebSocket stream client |
| `state.py` | `SessionState` + dataclasses (`TriggerInfo`, `MainTaskState`, `ChildTaskState`) — centralized runtime state |
| `callbacks.py` | `AdapterCallbacks` — `typing.Protocol` the adapter implements |
| `event_router.py` | `EventRouter` — WebSocket event loop, periodic sweep, Layer 1/2 dispatch, `strip_protocol_markers()` |
| `config.py` | `BotConfig` — configuration dataclass injected into router and client |
| `types.py` | `Event`, `InboundResult`, `RunningTask`, `HealthInfo`, `OpenAIConfig` — API response types |
| `approval.py` | `ApprovalTracker` (dedup) and `auto_approve` (workspace-level auto-approval) |

## SDK Boundary

**Inside SDK:**
- `ClonothClient` — all Supervisor HTTP API communication
- Data types — `InboundResult`, `Event`, `RunningTask`, etc.
- Approval policy — dedup, path classification, auto-approve
- `SessionState` — trigger lifecycle, session mapping, watermarks, task states
- `EventRouter` — event polling, protocol dispatch, 18 event handlers
- `AdapterCallbacks` — callback protocol definition
- `BotConfig` — configuration injection
- Protocol marker cleanup — `[CLONOTH_TOOL_TRACE]` stripping

**Outside SDK (adapter responsibility):**
- `[SPLIT]` message segmentation, `[REACT:xxx]` reaction extraction, `[BOT_RESTART]` signal handling
- `TextProcessor` / protocol marker display formatting
- Platform libraries (discord.py, telegram, etc.)
- Dot animation, typing throttle, streaming preview — display-layer logic
- Channel history queue management
- UI components (approval buttons, cancel buttons, embeds)

## Restart Scope

Modifying any file in `clonoth_sdk/` only requires restarting the **Bot process**. The Clonoth Supervisor backend is unaffected — no engine restart, no session loss.
