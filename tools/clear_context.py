from __future__ import annotations

"""
External tool (Clonoth).

The engine parses SPEC via AST at registration time.
At invocation this file runs as a subprocess:
  - Input: tool arguments as JSON on stdin
  - Output: result as JSON on stdout
  - Sensitive env vars are stripped
"""

SPEC = {'description': '清空指定 Discord 频道的对话上下文（Bot 端历史 + Engine session 上下文）。⚠️ 仅在用户明确要求清理/重置上下文时才可调用，禁止 AI 自行决定调用。副作用：会断开当前 session 的所有子任务回调链。channel_id 为 Discord 频道 ID。',
 'input_schema': {'properties': {'channel_id': {'description': 'Discord 频道 ID', 'type': 'string'}},
                  'required': ['channel_id'],
                  'type': 'object'},
 'name': 'clear_context'}

TIMEOUT_SEC = 30.0

SUPERVISOR_URL = "http://127.0.0.1:8765"


if __name__ == "__main__":
    import json, os, sys
    from pathlib import Path
    _input = json.loads(sys.stdin.read())

    def output(result, ok=True, error=""):
        payload = {"ok": ok, "data": {"result": result}}
        if not ok:
            payload["error"] = error or result
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(0 if ok else 1)

    def fail(error):
        # [2026-10-19] Why: previously every failure path still exited with a
        # hardcoded ok=True so the engine rendered "Context cleared" even when
        # the reset API returned 401. How: route all failures through output()
        # with ok=False so data.result carries the real reason.
        # Purpose: a failed reset must never look like a successful one.
        output(f"ERROR: {error}", ok=False, error=str(error))

    args = _input
    import httpx

    channel_id = str(args.get("channel_id", "")).strip()
    if not channel_id:
        fail("channel_id is required")

    # [2026-10-19] Why: /v1/conversations/reset requires admin auth, but this
    # tool previously sent no credentials and silently swallowed the 401.
    # How: read the admin token the same way the engine and bot do — env var
    # first, then data/.admin_token — and send it as a Bearer header.
    # Purpose: the reset request can actually succeed.
    admin_token = os.environ.get("CLONOTH_ADMIN_TOKEN", "").strip()
    if not admin_token:
        workspace_root = Path(os.environ.get("CLONOTH_WORKSPACE") or Path(__file__).resolve().parent.parent)
        token_file = workspace_root / "data" / ".admin_token"
        if token_file.is_file():
            admin_token = token_file.read_text(encoding="utf-8").strip()
    supervisor_url = os.environ.get("CLONOTH_SUPERVISOR_URL", SUPERVISOR_URL).rstrip("/")

    headers = {"Authorization": f"Bearer {admin_token}"} if admin_token else {}

    results = {}
    errors = []

    # 1. 清 Bot 端 _channel_history
    # [2026-10-19] Why: the previous code interpolated channel_id as a bare
    # token and keyed the dict with a str, so _channel_history[int] was never
    # reached. How: coerce to int when numeric and emit the key via json.dumps.
    # Purpose: the bot-side history entry is actually cleared.
    try:
        ch_key = int(channel_id) if channel_id.isdigit() else channel_id
        bridge_port = int(args.get("port") or 8768)
        resp = httpx.post(
            f"http://127.0.0.1:{bridge_port}/discord",
            json={"code": f"_channel_history[{json.dumps(ch_key)}] = []\nreturn {{'cleared': True}}"},
            timeout=10.0,
        )
        bot_result = resp.json()
        if bot_result.get("ok"):
            results["bot_history"] = "cleared"
        else:
            results["bot_history"] = f"failed: {bot_result.get('error', '')}"
            errors.append(f"bot_history: {results['bot_history']}")
    except (httpx.HTTPError, ValueError) as e:
        results["bot_history"] = f"error: {e}"
        errors.append(f"bot_history: {e}")

    # 2. 调用 supervisor 的 conversation reset API
    # [2026-10-19] Why: reset 会级联清理所有 entry branch，其中包含正在执行
    # 本工具的那个 branch，导致当前任务被自己标成 cancelled，状态消息变成
    # "任务已取消"。How: 从引擎注入的环境变量取出自身 task/session 并作为
    # 排除项发给 supervisor。Purpose: 清理动作不再砍掉自己脚下的分支。
    self_session_id = os.environ.get("CLONOTH_SESSION_ID", "").strip()
    self_task_id = os.environ.get("CLONOTH_TASK_ID", "").strip()
    conversation_key = f"discord:{channel_id}"
    try:
        resp = httpx.post(
            f"{supervisor_url}/v1/conversations/reset",
            json={
                "conversation_key": conversation_key,
                "exclude_session_id": self_session_id,
                "exclude_task_id": self_task_id,
            },
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            results["session_reset"] = "ok"
            results["old_session_id"] = data.get("old_session_id", "")
            results["cleared_children"] = data.get("cleared_children", 0)
            results["context_files_cleaned"] = data.get("context_files_cleaned", 0)
        elif resp.status_code == 404:
            # conversation not found = already clean, not a failure
            results["session_reset"] = "already clean (no active session)"
        elif resp.status_code == 401:
            results["session_reset"] = "failed: 401 unauthorized (missing or stale admin token)"
            errors.append("session_reset: 401 unauthorized — check data/.admin_token")
        else:
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except ValueError:
                detail = resp.text[:120]
            results["session_reset"] = f"failed: {resp.status_code} {detail}".strip()
            errors.append(f"session_reset: {resp.status_code} {detail}".strip())
    except (httpx.HTTPError, ValueError) as e:
        results["session_reset"] = f"error: {e}"
        errors.append(f"session_reset: {e}")

    results["conversation_key"] = conversation_key

    if errors:
        summary = "Context reset incomplete — " + "; ".join(errors)
        results["errors"] = errors
        print(json.dumps({"ok": False, "error": summary,
                          "data": {"result": summary, **results}}, ensure_ascii=False))
        sys.exit(1)

    print(json.dumps({"ok": True, "data": {"result": "Context cleared", **results}}, ensure_ascii=False))
    sys.exit(0)
