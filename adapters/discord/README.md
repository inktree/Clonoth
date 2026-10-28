# Clonoth Discord 适配器

基于 discord.py 的 Discord Bot 适配器。通过 Clonoth SDK 与 Supervisor 通信，支持多节点委派、审批按钮、附件处理和 Bridge Server。

## 目录结构

```
adapters/
├── message_text.py      # 跨平台确定性分段、代码围栏闭合与 <tts> 标签解析
└── discord/
    ├── app.py           # 入口：配置常量、DiscordRuntime、SDK 初始化、事件注册
    ├── main.py          # 兼容性入口（调用 app.main()）
    ├── agent.py         # Bridge Server、消息处理主逻辑、!model 命令、审批/取消 View
    ├── callbacks.py     # EventRouter 回调实现（发送 Discord 消息、编辑进度、React）
    ├── context.py       # 群聊/私聊上下文构建（历史记录、成员身份、提及解析）
    ├── messaging.py     # 附件收集、分块发送、语音转写/合成调用与安全重启
    └── __init__.py
```

## 前置依赖

- Python 3.11+
- discord.py 2.x
- aiohttp（Bridge Server）
- PyYAML
- python-dotenv（可选，自动加载 .env）
- clonoth_sdk（项目根目录下的 SDK 包）

```bash
pip install discord.py aiohttp pyyaml python-dotenv
```

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `DISCORD_TOKEN` | 是 | — | Discord Bot Token |
| `DISCORD_SUPERUSERS` | 建议 | 空 | 管理员 Discord User ID，逗号分隔 |
| `CLONOTH_URL` | 否 | `http://127.0.0.1:8765` | Supervisor API 地址 |
| `CLONOTH_WORKSPACE` | 否 | 项目根目录 | 工作区路径，用于附件存储和 admin token 读取 |
| `DISCORD_LOG_CHANNEL` | 否 | `0` | 日志频道 ID（0 表示不发送日志） |
| `DISCORD_BRIDGE_PORT` | 否 | `8768` | Bridge Server 监听端口 |
| `DISCORD_ENTRY_NODE` | 否 | `main` | 入口节点 ID |
| `DISCORD_NODE_NAMES` | 否 | `{}` | 节点显示名映射，JSON 格式 |
| `DISCORD_HISTORY_LEN` | 否 | `15` | 每个频道保留的历史消息条数 |
| `CLONOTH_DISCORD_VOICE_INPUT` | 否 | `true` | 是否启用 Discord 音频附件转写 |
| `CLONOTH_DISCORD_VOICE_REPLY` | 否 | `true` | 是否允许 Discord 音频附件回复 |
| `CLONOTH_DISCORD_VOICE_REPLY_ON_VOICE_INPUT` | 否 | `true` | 用户发送音频附件时是否自动生成音频回复 |

## 部署步骤

### 1. 创建 Discord Application

1. 前往 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用。
2. 在 Bot 页面获取 Token，开启以下 Privileged Gateway Intents：
   - Message Content Intent
   - Server Members Intent
3. 使用 OAuth2 URL Generator 生成邀请链接，勾选 `bot` 和 `applications.commands` scope，权限至少包含：Send Messages、Embed Links、Attach Files、Add Reactions、Read Message History、Use External Emojis。

### 2. 配置环境变量

在项目根目录创建 `.env` 文件（或通过 systemd、docker-compose 注入）：

```bash
DISCORD_TOKEN=your_bot_token_here
DISCORD_SUPERUSERS=123456789012345678,987654321098765432
CLONOTH_URL=http://127.0.0.1:8765
DISCORD_LOG_CHANNEL=1234567890
DISCORD_BRIDGE_PORT=8768
DISCORD_ENTRY_NODE=main
```

### 3. 确保 Supervisor 已启动

Discord Bot 依赖 Supervisor API。启动 Bot 前确认 Supervisor 已在 `CLONOTH_URL` 指定的地址上运行。Bot 启动时会自动重试连接最多 10 次。

### 4. 启动 Bot

```bash
# 方式一：直接运行
cd /path/to/clonoth
python -m adapters.discord.app

# 方式二：通过兼容性入口
python adapters/discord/main.py
```

### 5. systemd 服务（推荐）

```ini
[Unit]
Description=Clonoth Discord Bot
After=network.target clonoth-supervisor.service

[Service]
Type=simple
User=www
WorkingDirectory=/path/to/clonoth
EnvironmentFile=/path/to/clonoth/.env
ExecStart=/usr/local/bin/python3.11 -m adapters.discord.app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 架构说明

### Bridge Server

Bot 启动后会在 `127.0.0.1:{DISCORD_BRIDGE_PORT}` 开启一个本地 HTTP 服务，供 `discord_manage` 与 `clear_context` 等外部工具调用：

- `POST /discord` — 在 Bot 进程内执行异步 `discord.py` 代码并返回 JSON 结果

### 消息流程

```
用户消息 → discord.py on_message
  → 构建上下文（群聊/私聊历史、成员身份、附件与语音转写）
  → SDK submit_inbound → Supervisor (群聊默认 use_branch=True 并发分支)
  → Supervisor 调度 Engine 执行并向 /v1/ws 推送实时事件
  → EventRouter 消费 WebSocket 事件流
  → callbacks 调用 message_text.py 确定性切分文本、闭合代码块并合成/发送 TTS 语音
```

### 附件处理

附件通过 `_collect_attachments()` 从 Discord CDN（含引用消息与转发消息 `message_snapshots`）提取并上传至 Supervisor（或写入 `data/attachments/`）。

### 音频消息处理（ASR & 双后端 TTS）

Discord 用户发送音频附件时，适配器调用 Supervisor 的 `/v1/audio/transcribe`（Mimo ASR）转写为文本，并以语音输入模式提交给 Clonoth。

回复中显式标记的 `<tts ...>...</tts>`（或 `[[tts]]...[[/tts]]`）片段会调用 Supervisor 的 `/v1/audio/synthesize` 合成语音附件并发送：
- **本地 Ryza ONNX (`backend="ryza_onnx"`)**：支持 `normal_cn`、`normal_ja`、`whisper_cn`、`whisper_ja` 预设，输出 32kHz WAV，空闲自动卸载模型释放显存。
- **Mimo 云端 (`backend="mimo"`)**：支持自定义 `voice` 与 `instruction`。

## Docker 注意事项

- Bot 本身只需出站网络连接（连 Discord Gateway + Supervisor API），不需要开放入站端口。
- `data/attachments/` 目录需挂载为持久卷，或确保 Bot 和 Supervisor 共享同一个卷。
- Bridge Server 端口需在 Bot 和 Supervisor 之间可达。

## 多实例部署

同一台机器部署多个 Bot 实例时，确保每个实例的 `DISCORD_BRIDGE_PORT` 不同。
