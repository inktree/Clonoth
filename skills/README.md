# Clonoth Skills

这里维护按需激活（`strategy: normal`、`scan_depth: 0`）的业务与运维 Skills。所有已落地的常规开发规范不再挂载为 `constant` 常驻 Skill。

## 当前 Skills

| Skill | 策略 | 触发条件与用途 |
|---|---|---|
| `clonoth-ryza-tts-ops` | normal | Ryza ONNX TTS 运维记录：hybrid 环境、性能与显存实测、空闲卸载、CUDA context 残留与测试脚本。 |
| `clonoth-ws-eventstream-troubleshooting` | normal | Clonoth SDK WebSocket 事件流（`/v1/ws`）中断排查、websockets v14+ 头参数与重启方式选择。 |
| `discord-voice-reply-policy` | normal | Discord 语音消息转写与 `<tts>` 语音合成回复协议说明。 |
| `web_tts_reply_policy` | normal | Web 前端看到 `[TTS:auto]` 时的 `<tts>` 语音回复标记规则。 |
