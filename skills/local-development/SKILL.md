---
name: local-development
description: BotTTS 本地开发、调试和运行流程
enabled: true
strategy: constant
order: 0
priority: 10
node_ids:
  - local_assistant
---

# BotTTS 本地开发流程

当用户要求你维护、运行、调试或部署当前 BotTTS 项目时，优先按照本流程处理。

## 项目关系

当前工作区采用业务层与框架层分离的结构：

| 目录 | 角色 | 说明 |
|---|---|---|
| `Clonoth/` | 框架运行实例 | Supervisor、Engine、Web、工具、运行时配置和本地数据。 |
| `BotTTS/` | 当前业务 Bot 项目 | 节点、skills、文档、脚本和后续平台适配器。 |
| `EreunaMain/` | 参考实现 | 现有 Discord Bot 项目，只作为结构和流程参考。 |

## 基本原则

1. 优先在 `BotTTS/` 中维护业务内容。
2. 不要把 BotTTS 的业务 prompt、skills、平台适配逻辑直接写进 Clonoth 框架源码。
3. `Clonoth/data/config.yaml`、`Clonoth/.env` 是当前本地运行实例的真实配置，不要轻易覆盖。
4. `BotTTS/.env.example` 只是参考模板，不是运行时配置源。
5. 修改 BotTTS 节点或 skills 后，应执行同步脚本，再启动或重启 Clonoth。

## 常用命令

### 同步 BotTTS 配置到 Clonoth

```powershell
cd ..\BotTTS
.\scripts\sync-to-clonoth.ps1
```

同步内容：

- `BotTTS/config/nodes/*.yaml` -> `Clonoth/config/nodes/`
- `BotTTS/skills/*` -> `Clonoth/skills/`

### 启动本地完整环境

```powershell
cd ..\BotTTS
.\scripts\run-local.ps1
```

### 只启动后端和 Web，不启动本地 Shell/TUI

```powershell
cd ..\BotTTS
.\scripts\run-local-no-shell.ps1
```

### 访问 Web

```text
http://127.0.0.1:8765/web/
```

### 访问管理后台

```text
http://127.0.0.1:8765/admin/
```

## 本地运行配置

本地模型和端口配置优先检查：

1. `Clonoth/.env`
2. `Clonoth/data/config.yaml`
3. 当前 PowerShell 会话中的环境变量

常见模型配置：

```yaml
version: 1
provider: openai

openai:
  base_url: "http://127.0.0.1:11434/v1"
  api_key: "ollama"
  model: "qwen2.5:7b"
```

## 修改流程

当用户要求修改 BotTTS 的行为时：

1. 判断修改对象：节点、skill、文档、脚本或 Clonoth 运行配置。
2. 如果是业务行为，优先修改 `BotTTS/config/nodes/` 或 `BotTTS/skills/`。
3. 如果是本地运行配置，再考虑修改 `Clonoth/config/runtime.yaml`、`Clonoth/data/config.yaml` 或 `Clonoth/.env`。
4. 修改后运行同步脚本。
5. 必要时重启 Clonoth。

## 不要做的事

- 不要把平台接入逻辑提前塞进 `local_assistant`。
- 不要为了 BotTTS 业务需求随意修改 Clonoth 框架源码。
- 不要覆盖 `Clonoth/data/config.yaml` 中用户已经配置好的模型信息。
- 不要把真实 API Key、Token 写入 BotTTS 文档或示例文件。
