---
name: local-voice-development
description: BotTTS 本地语音工作流开发规范
enabled: true
strategy: constant
order: 18
priority: 8
node_ids:
  - local_assistant
---

# BotTTS 本地语音开发规范

当前 BotTTS 正在构建本地语音工作流。

当前阶段目标：

```text
ASR（非实时）
→ 文本 Agent
→ Streaming TTS
→ 本地自动播报
```

## 当前设计原则

### 输入端

采用：

```text
非实时录音
```

工作流：

```text
快捷键按下
→ 开始录音
→ 松开
→ 调用 ASR
```

当前阶段不做：

- VAD
- Wake Word
- 实时语音 websocket
- 双工语音

---

### Agent 层

Clonoth 保持纯文本 Agent。

不要：

- 修改 Clonoth engine 以支持音频。
- 把 ASR/TTS 强耦合进 tool runtime。
- 让 Agent 直接处理底层音频协议。

语音层属于：

```text
BotTTS/adapters/local_voice/
```

---

### 输出端

采用：

```text
句子级 Streaming TTS
```

而不是：

```text
完整回复结束后统一播报
```

原因：

- 长回复等待感太强。
- 用户会误以为没有响应。
- 流式播报更像真实语音助手。

---

## 当前推荐模型

### ASR

```text
mimo-v2.5-asr
```

### TTS

```text
mimo-v2.5-tts
```

当前阶段暂不优先：

- voiceclone
- voicedesign

---

## 当前推荐输出格式

TTS 输出：

```text
PCM16 streaming
```

原因：

- 可边接收边播放。
- 延迟低。
- 不需要等待完整 wav。

---

## ffmpeg 的角色

ffmpeg 属于：

```text
音频基础设施
```

负责：

- 转码。
- 重采样。
- wav/mp3 处理。
- 调试导出。

不要把 ffmpeg 当作实时播放核心。

---

## 当前推荐技术栈

### 音频录制

```text
sounddevice
soundfile
numpy
```

### 音频播放

```text
sounddevice
```

### API 调用

```text
httpx
openai SDK
```

### 流式回复

使用：

```text
Clonoth websocket event stream
```

接收：

```text
stream_delta
```

---

## 重要原则

### 不做 token 级 TTS

不要：

```text
token -> tts
```

应采用：

```text
句子缓冲
→
完整句子
→
TTS
```

---

### 不污染 Clonoth

语音 adapter 属于：

```text
BotTTS/adapters/local_voice/
```

除非是框架 bug，否则不要直接修改：

```text
Clonoth/engine/
Clonoth/supervisor/
```

---

### 当前优先级

优先：

- 本地稳定运行。
- 流式播报体验。
- 播放队列。
- 句子级调度。

暂缓：

- QQ 语音。
- 实时 ASR。
- Wake Word。
- 复杂情绪控制。
- 多角色实时切换。
