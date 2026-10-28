---
name: discord-voice-reply-policy
description: Discord 音频消息与 TTS 回复协议。使用现有 <tts> 标记合成语音（支持 ryza_onnx 与 mimo），不要搜索或寻找额外 TTS 工具。
enabled: true
strategy: normal
keywords:
- Discord 语音消息
- Discord 语音附件
- discord_voice_reply
priority: 90
scan_depth: 0
---

# Discord 音频消息与 TTS 回复规则

Discord 适配器已接入音频输入转写与语音输出合成。该能力不是模型需要调用的工具，也不需要搜索接口。

## 输入

用户发送 Discord 音频消息或音频文件后，适配器会先调用 ASR 转写，再把转写文本交给模型。模型只需按照转写内容回答，不要尝试处理底层音频文件，除非用户明确要求检查文件本身。

## 输出

需要发送语音时，将适合朗读的自然语言放在 `<tts ...>` 和 `</tts>` 之间，由 Discord 适配器提取并调用 Supervisor `/v1/audio/synthesize` 发送音频附件：

- 本地参考音频（Ryza ONNX）：`<tts backend="ryza_onnx" preset="normal_cn">正文</tts>`（`preset` 可选 `normal_cn`、`normal_ja`、`whisper_cn`、`whisper_ja`；`normal_cn`/`whisper_cn` 需使用纯中文文本，不要混入拉丁字母）。
- Mimo 在线语音：`<tts backend="mimo" voice="音色名">正文</tts>`（`voice` 可省略以使用默认音色，支持 `（温柔）`、`[轻笑]` 等风格标签）。

必须遵守：

1. 不要搜索 TTS 工具，不要寻找语音端点，不要声称自己不能发送语音。
2. 正常文字部分应提供完整回答，TTS 作为附加语音版本，不要发送纯 TTS。
3. `<tts>` 内只放适合直接朗读的自然语言。代码、日志、表格、文件路径和工具结果不要放入其中。
4. 用户通过语音输入时，优先附加简短语音回复；文字输入时，仅在用户要求或场景确实合适时附加。群聊中保持克制。

## 故障判断

如果已正确输出 `<tts>` 但 Discord 未出现音频附件，应检查 Discord 适配器日志与 Supervisor `/v1/audio/synthesize` 接口日志，不要反复盲目重试。
