# Clonoth 语音输入与 TTS 实现参考

本文记录 Clonoth Web 与 Discord 的语音输入、Mimo ASR、Mimo 在线 TTS、本地 Ryza ONNX TTS，以及参考音频路由的当前实现方式，供后续维护、迁移和第三方接入参考。

本文以当前代码为准。Web 与 Discord 对 `<tts>` 属性的支持范围不同，接入时不能混用。

## 1. 总体流程

当前 Web 语音链路分为两部分。

第一部分是语音输入：

1. 用户在聊天输入栏点击麦克风按钮，或使用 `Alt+M` 快捷键开始录音。
2. 再次点击麦克风按钮，或再次按 `Alt+M` 停止录音。
3. 前端使用 `MediaRecorder` 录音。
4. 浏览器录音通常是 `webm`，前端会用 `AudioContext` 解码，再编码为 `wav`。
5. 前端调用 Supervisor 的 `/v1/audio/transcribe`。
6. 后端调用 Mimo ASR，把音频识别为文字。
7. 前端把识别结果自动发送给助手。

第二部分是语音回复：

1. 聊天输入栏有 TTS 模式、TTS 后端、本地参考音频预设和音量控件。
2. 需要自动语音回复时，前端在用户消息末尾加入短标记 `[TTS:auto]`。
3. `web_tts_reply_policy` skill 被 `[TTS:auto]` 激活。
4. 助手按 skill 规则，在回复中用 `<tts>...</tts>` 或 `[[tts]]...[[/tts]]` 标出适合朗读的内容。
5. 前端检测这些 TTS 标记段。
6. 每个 TTS 段单独调用 `/v1/audio/synthesize`，并传递网页中选择的 `mimo` 或 `ryza_onnx` 后端。
7. 选择本地后端时，前端还会传递所选的参考音频预设；选择 Mimo 时，不会把本地预设传给 Mimo。
8. 后端完成合成并保存音频附件。
9. 前端按顺序播放这些音频。

## 2. Mimo ASR 接入

Mimo ASR 使用官方 V2.5 chat-completions 风格接口。

默认配置：

```yaml
mimo_audio:
  base_url: "${MIMO_BASE_URL}"        # 默认 https://api.xiaomimimo.com
  api_key: "${MIMO_API_KEY}"
  asr_path: "${MIMO_ASR_PATH}"        # 默认 /v1/chat/completions
  asr_model: "${MIMO_ASR_MODEL}"      # 默认 mimo-v2.5-asr
  asr_language: "${MIMO_ASR_LANGUAGE}" # 默认 auto
```

请求要点：

1. 鉴权使用 `api-key` 请求头。
2. 音频要转成 data URL：

```text
data:{MIME_TYPE};base64,{BASE64_AUDIO}
```

3. data URL 放在 `messages[0].content` 的 `input_audio.data` 中。
4. 语言参数放在 `asr_options.language` 中。

Mimo ASR 只支持 `wav` 和 `mp3`。Web 前端会优先把麦克风录音转为 `wav` 后上传。后端仍保留 `ffmpeg_path` 配置，用于 QQ 或其他非 Web 音频来源的格式转换。

## 3. 统一 TTS 接口与后端选择

Supervisor 对外提供统一接口：

```text
POST /v1/audio/synthesize
```

常用请求字段：

| 字段 | 适用范围 | 说明 |
|---|---|---|
| `text` | 全部 | 必填，待合成文本 |
| `conversation_key` | 全部 | 音频附件所属会话 |
| `backend` | 全部 | `mimo` 或 `ryza_onnx` |
| `response_format` 或 `format` | 全部 | 输出格式；本地后端只支持 `wav` |
| `voice` | Mimo；本地兼容 | Mimo 音色；本地后端也把它作为预设的兼容字段 |
| `preset` | 本地 | 本地参考音频预设 |
| `instruction` | Mimo | 自然语言风格指令 |
| `model` | Mimo | 覆盖默认 Mimo TTS 模型 |

后端选择顺序如下：

1. 请求体中的 `backend`。
2. Supervisor 进程配置中的 `tts_backend`。
3. 未配置时使用 `mimo`。

不应让语言检测覆盖明确的 `backend` 或 `preset`。调用方知道所需后端和参考音频时，应当明确传递。

### 3.1 Mimo TTS 接入

Mimo TTS 使用官方 V2.5 chat-completions 风格接口。

默认配置：

```yaml
mimo_audio:
  tts_path: "${MIMO_TTS_PATH}"      # 默认 /v1/chat/completions
  tts_model: "${MIMO_TTS_MODEL}"    # 默认 mimo-v2.5-tts
  tts_voice: "${MIMO_TTS_VOICE}"    # 默认 mimo_default
  tts_format: "${MIMO_TTS_FORMAT}"  # 默认 wav
```

请求要点：

1. 鉴权使用 `api-key` 请求头。
2. 目标合成文本放在 `role: assistant` 的 message 中。
3. 自然语言风格指令可以放在 `role: user` 的 message 中。
4. 音频参数放在 `audio` 字段中，例如：

```json
{
  "format": "wav",
  "voice": "mimo_default"
}
```

5. 返回音频从 `message.audio.data` 读取 base64。
6. 后端把音频保存为附件，前端通过 `/v1/attachments/file?path=...` 播放。

### 3.2 本地 Ryza ONNX TTS

本地后端名称为：

```text
ryza_onnx
```

它使用四个参考音频预设：

| 预设 | 语言 | 情景 |
|---|---|---|
| `normal_cn` | 中文 | 正常说话 |
| `normal_ja` | 日语 | 正常说话 |
| `whisper_cn` | 中文 | 低语 |
| `whisper_ja` | 日语 | 低语 |

示例请求：

```json
{
  "text": "请放松下来。",
  "conversation_key": "web:example",
  "backend": "ryza_onnx",
  "preset": "whisper_cn",
  "response_format": "wav"
}
```

本地后端的约束：

1. 单次文本最长 510 个字符。
2. 只支持 WAV 输出。
3. 模型在首次请求时加载，后续请求复用同一个引擎。
4. 合成请求会串行执行，避免多个请求同时占用同一个推理引擎。
5. 显式传递 `preset` 时，以该预设为准，不再根据文本语言改写。
6. 未显式传递 `preset` 或 `voice` 时，先使用 `local_onnx_tts_default_preset`。如果文本含有平假名或片假名，则在保留正常或低语情景的前提下切换到日语预设。
7. 当前自动语言判断只检查假名。只有汉字的日语文本可能无法被可靠识别，因此调用方应尽量明确传递日语预设。

相关进程配置：

```yaml
supervisor:
  process_manager:
    tts_backend: "ryza_onnx"          # ryza_onnx | mimo
    local_onnx_tts_path: "/path/to/ryza_v2proplus_onnx_deploy"
    local_onnx_tts_provider: "cpu"    # cpu | hybrid | cuda
    local_onnx_tts_validate_hashes: false
    local_onnx_tts_default_preset: "normal_cn"
    local_onnx_tts_seed: 1234
```

## 4. Web 输入栏操作逻辑

聊天输入栏包含几个和语音相关的控件。

### 4.1 麦克风按钮

行为：

1. 未录音时点击：开始录音。
2. 录音中点击：停止录音并识别。
3. 识别完成后：自动发送识别文本。
4. 如果输入框已有文本，会把已有文本和识别文本合并后发送。

快捷键：

```text
Alt+M
```

未录音时开始录音，录音中停止并识别。

录音有 120 秒上限。如果用户忘记停止录音，前端会自动停止 `MediaRecorder`，然后继续走原来的转 wav、ASR 和自动发送流程。手动停止、组件卸载或录音启动失败时，都会清理计时器和麦克风资源。

### 4.2 TTS 模式按钮

TTS 模式保存在前端本地偏好中。

当前模式：

1. `off`，界面显示“静音”：不自动语音回复。
2. `voice-input`，界面显示“语音”：语音输入后，下一条助手回复自动朗读。
3. `always`，界面显示“总是”：普通文字输入和语音输入都会触发自动朗读。

默认模式是 `voice-input`。

### 4.3 TTS 后端和参考音频

TTS 后端保存在前端本地偏好中。当前可选项为：

1. `mimo`，界面显示“Mimo”，使用在线 Mimo TTS。
2. `ryza_onnx`，界面显示“本地”，使用本地 Ryza ONNX TTS。

默认后端是 `mimo`。

选择“本地”后，输入栏显示参考音频预设选择器，可选中文、日文、中文低语和日文低语。它们分别对应 `normal_cn`、`normal_ja`、`whisper_cn` 和 `whisper_ja`。

选择 Mimo 后，本地预设选择器会隐藏。Mimo 使用语音设置页中的 `tts_voice`，网页播放请求不会把本地 `preset` 传给 Mimo。

### 4.4 TTS 音量滑块

输入栏中有 TTS 音量滑块。音量保存在本地偏好里。

手动朗读和自动朗读都会使用这个音量。

## 5. TTS 规则注入方式

旧实现曾经把完整 TTS 长规则附加到每条用户消息末尾。这个方式已经废弃。

当前方式：

1. 完整规则放在 skill 中：

```text
skills/web_tts_reply_policy/SKILL.md
```

2. 前端只发送短标记：

```text
[TTS:auto]
```

3. skill 通过关键词 `[TTS:auto]` 激活。
4. UI 层会过滤 `[TTS:auto]`，不显示给用户。
5. UI 层仍保留旧长提示过滤逻辑，用于兼容旧历史消息。

这样做的原因：

1. 减少重复 token。
2. 避免污染用户消息。
3. 避免上下文压缩破坏完整规则。
4. 让规则保存在稳定位置，便于维护和迁移。

## 6. 助手回复中的 TTS 标记

### 6.1 Web 标记

助手在适合语音播放时，使用以下标记：

```text
<tts>适合朗读的内容</tts>
```

也支持：

```text
[[tts]]适合朗读的内容[[/tts]]
```

规则：

1. 可以有多个 TTS 段。
2. 每个 TTS 段会单独合成。
3. 多个 TTS 段按顺序播放。
4. 普通正文仍然正常输出。
5. `<tts>` 只表示适合朗读的部分，不替代完整回答。
6. 页面显示时会隐藏 `<tts>` 和 `[[tts]]` 标记本身，只显示其中内容。
7. Web 当前只识别无属性的 `<tts>正文</tts>`。Web 的后端和本地预设由输入栏偏好决定，不读取标签上的 `backend`、`preset`、`voice` 或 `instruction`。

### 6.2 Discord 逐段路由标记

Discord 支持在每个 `<tts>` 段上明确指定合成参数。

本地中文低语示例：

```text
<tts backend="ryza_onnx" preset="whisper_cn">请慢慢放松下来。</tts>
```

本地日语正常说话示例：

```text
<tts backend="ryza_onnx" preset="normal_ja">こんばんは。</tts>
```

Mimo 示例：

```text
<tts backend="mimo" voice="冰糖" instruction="请用平静的语气朗读">测试内容。</tts>
```

Discord 标签允许的属性：

| 属性 | 说明 |
|---|---|
| `backend` | 只接受 `mimo` 或 `ryza_onnx` |
| `preset` | 只接受四个本地参考音频预设 |
| `voice` | Mimo 音色；未指定时可使用 Discord 运行配置中的默认音色 |
| `instruction` | Mimo 的自然语言风格指令 |

Discord 的处理规则：

1. 每段独立解析和合成，因此同一条回复可以混用中文、日语、正常和低语预设。
2. 标签中的参数优先于 Discord 运行时默认值。
3. 标签没有指定 `backend` 时，先使用 `CLONOTH_DISCORD_TTS_BACKEND`；仍为空时，由 Supervisor 的 `tts_backend` 决定。
4. 标签没有指定 `voice` 或 `instruction` 时，可以使用 Discord 运行时默认值。
5. 显式 `preset` 会原样传给 Supervisor，不会被 Discord 的语言检测覆盖。
6. `[[tts]]...[[/tts]]` 旧格式仍受支持，但不能携带逐段属性。
7. 不支持的属性会被忽略；不支持的 `backend` 或 `preset` 不会传给 Supervisor。

不应放入 TTS 段的内容：

1. 代码。
2. 日志。
3. 表格。
4. diff。
5. 工具结果。
6. 长列表。
7. 文件路径、命令、复杂参数和复杂符号。

如果正文涉及这些内容，应在 `<tts>` 中改写为简短自然语言说明。

## 7. Mimo 风格标签和音频标签

Mimo 支持在 TTS 文本中加入标签控制。

整体风格标签应放在目标文本开头。括号可以是半角、全角或方括号。

示例：

```text
（温柔）这是一段温柔的说明。
(开心) 这个结果已经完成。
[粤语] 呢个测试已经完成。
```

音频标签可以插入文本任意位置。

示例：

```text
[深呼吸] 我先说明结论。
这个地方已经修好了。[轻笑]
```

使用要求：

1. 标签要克制使用。
2. 不要每句话都加标签。
3. 默认不要主动使用唱歌标签。
4. 只有用户明确要求唱歌、歌词或唱歌模式测试时，才使用 `（唱歌）`。

## 8. 口播文本要求

`<tts>` 内应写成适合直接口播的自然语言。

需要避免：

1. Markdown 列表。
2. 横杠项目符号。
3. 表格分隔符。
4. 引用块。
5. 代码式缩进。
6. 类似代码排版的符号。

例如，不要在 `<tts>` 中写：

```text
- 第一项
---
```

正常口播中的顺序表达可以使用，例如：

```text
第一，先检查配置。
第二，再运行构建。
第三，最后刷新页面。
```

不要主要依赖前端正则过滤来修正口播文本。应在生成 `<tts>` 内容时主动避免会被语音引擎读错的排版符号。

## 9. 自动朗读策略

自动朗读只处理明确标记的 TTS 段。

也就是说，自动朗读不会把整条助手回复当作 TTS 文本。

这样可以避免以下内容被误读：

1. 代码。
2. diff。
3. 工具调用结果。
4. Markdown 格式。
5. 短残留片段。

手动朗读按钮仍然可以使用全文回退逻辑。

## 10. 多段 TTS 和流式播放

当前前端支持流式检测 TTS 段。

当流式文本中出现完整闭合的：

```text
<tts>...</tts>
```

或：

```text
[[tts]]...[[/tts]]
```

前端会立即把这一段加入播放队列，不必等整条助手消息完成。

这里所说的前端检测仍以无属性 `<tts>...</tts>` 和旧格式为准。Discord 的带属性标签由 Discord 适配器解析，不经过 Web 播放队列。

实现要点：

1. 每个 TTS 段单独调用 `/v1/audio/synthesize`。
2. 多段按顺序排队播放。
3. 已处理过的段会记录 key，避免流式更新时重复合成。
4. 历史消息不会自动朗读，避免刷新后重复播放。
5. 页面级全局队列保存在 `window.__clonothTtsPlaybackState`，避免 `MessageCard` 生命周期打断播放。
6. 每段 TTS 音频有 120 秒播放上限。超过上限时，前端会暂停音频、清空 `src` 并调用 `load()` 释放资源，然后继续处理队列中的下一段。

## 11. Discord 运行配置

Discord 语音相关环境变量如下：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CLONOTH_DISCORD_VOICE_INPUT` | `true` | 是否转写音频附件 |
| `CLONOTH_DISCORD_VOICE_REPLY` | `true` | 是否允许发送合成音频回复 |
| `CLONOTH_DISCORD_VOICE_REPLY_ON_VOICE_INPUT` | `true` | 收到语音输入时是否自动附加语音回复 |
| `CLONOTH_DISCORD_TTS_FORMAT` | `wav` | 输出格式 |
| `CLONOTH_DISCORD_TTS_BACKEND` | 空 | Discord 默认后端；空值表示交给 Supervisor 默认配置 |
| `CLONOTH_DISCORD_TTS_VOICE` | 空 | 未在标签指定时使用的默认 Mimo 音色 |
| `CLONOTH_DISCORD_TTS_INSTRUCTION` | 空 | 未在标签指定时使用的默认 Mimo 风格指令 |
| `CLONOTH_DISCORD_DELETE_AUDIO_AFTER_USE` | `true` | 发送或转写后是否删除临时音频 |

建议模型在 Discord 回复中明确输出逐段参数。这样可以避免全局默认后端或默认中文预设影响日语、低语等情景。

## 12. 与 WorkingGroup 的关系

不要为了 TTS 修改 WorkingGroup 原有展示逻辑。

之前曾尝试修改 `WorkingGroup` 的 key，使它不随 active/done 状态变化重建。这个改动会影响原有 Working 区域和思维链展示，因此已经回滚。

当前原则：

1. WorkingGroup 保持原有展示和折叠逻辑。
2. TTS 播放稳定性由页面级全局播放队列处理。
3. 不用改 WorkingGroup key 来解决播放问题。

## 13. 迁移要点

如果把这套语音能力迁移到其他项目，建议保留以下设计：

1. Web 端麦克风录音先转 `wav`，再上传 ASR。
2. 后端提供 `/v1/audio/transcribe` 和 `/v1/audio/synthesize` 两个接口。
3. TTS 接口应把在线服务和本地模型作为并列后端，不应让其中一个覆盖另一个的配置入口。
4. 后端、语言、正常或低语情景应分别建模。调用方能够确定时，应显式传递 `backend` 和 `preset`。
5. 自动朗读只处理明确标记的 TTS 段。
6. 完整规则放到 skill、系统提示或稳定配置中。
7. 每条消息只使用短触发标记，例如 `[TTS:auto]`。
8. 多段 TTS 要逐段合成，顺序播放，并允许每段保留自己的路由参数。
9. 流式阶段发现完整 TTS 段后应提前合成。
10. UI 层需要隐藏 `[TTS:auto]`、`<tts>` 和 `[[tts]]` 等控制标记。
11. 不要把代码、diff、日志或复杂符号直接送进 TTS。
12. Web 录音和 TTS 播放都应设置时长上限。当前默认是 120 秒，避免忘记停止录音或异常音频导致持续占用资源。
13. 不要为了 TTS 破坏原有消息展示组件的生命周期逻辑。
14. 文档应明确各平台的标签协议。不能假定 Web、Discord 和其他适配器都支持相同的 `<tts>` 属性。
