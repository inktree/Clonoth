---
name: local-troubleshooting
description: BotTTS 本地运行故障排查流程
enabled: true
strategy: normal
keywords:
  - 启动失败
  - 运行失败
  - 报错
  - 前端打不开
  - web打不开
  - 端口占用
  - 模型连不上
  - npm
  - pnpm
  - python main.py
  - supervisor
  - Clonoth
order: 20
priority: 6
node_ids:
  - local_assistant
---

# BotTTS 本地故障排查流程

当用户反馈本地运行失败、Web 打不开、模型不可用或依赖安装失败时，按照以下顺序排查。

## 第一步：确认当前运行方式

先确认用户是通过哪种方式启动：

```powershell
cd ..\BotTTS
.\scripts\run-local.ps1
```

或：

```powershell
.\scripts\run-local-no-shell.ps1
```

如果用户直接在 `Clonoth/` 下执行 `python main.py`，也可以运行，但不会自动同步 BotTTS 的节点和 skills。

## 第二步：检查配置同步

确认以下文件是否存在：

```text
Clonoth/config/nodes/local_assistant.yaml
Clonoth/config/nodes/qq_assistant.yaml
Clonoth/skills/local-development/SKILL.md
Clonoth/skills/project-structure/SKILL.md
Clonoth/skills/local-troubleshooting/SKILL.md
```

如不存在，执行：

```powershell
cd ..\BotTTS
.\scripts\sync-to-clonoth.ps1
```

## 第三步：检查入口节点

检查：

```text
Clonoth/config/runtime.yaml
```

应包含：

```yaml
shell:
  entry_node_id: "local_assistant"
```

如果不是，说明本地 Shell/TUI 入口仍然指向其他节点。

## 第四步：检查 Web 前端

前端构建产物应存在：

```text
Clonoth/adapters/web/frontend/dist/
```

如果不存在，执行：

```powershell
cd adapters\web\frontend
pnpm install
pnpm run build
```

访问地址：

```text
http://127.0.0.1:8765/web/
```

## 第五步：检查 Supervisor

默认地址：

```text
http://127.0.0.1:8765
```

如果访问失败，检查：

- Clonoth 是否仍在运行。
- 端口 `8765` 是否被占用。
- 是否修改过 `CLONOTH_PORT`。
- 日志文件：`Clonoth/data/logs/supervisor.log`。

## 第六步：检查模型配置

模型配置优先查看：

```text
Clonoth/data/config.yaml
Clonoth/.env
```

OpenAI 兼容接口常见值：

```text
OPENAI_BASE_URL=http://127.0.0.1:11434/v1
OPENAI_API_KEY=ollama
OPENAI_MODEL=qwen2.5:7b
```

如果使用 Ollama，需要确认 Ollama 已运行并且模型已拉取。

## 第七步：Windows 常见问题

### PowerShell 禁止脚本执行

执行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### npm / pnpm 依赖安装失败

优先使用 `pnpm`：

```powershell
cd adapters\web\frontend
pnpm install
pnpm run build
```

如果 npm 镜像 404，切换源：

```powershell
npm config set registry https://registry.npmjs.org/
```

### platform.shell.tui 启动错误

如果出现：

```text
No module named 'platform.shell'; 'platform' is not a package
```

说明 shell 启动模块路径与 Python 标准库 `platform` 冲突。当前本地实例已做兼容修复，应使用 `shell.tui` / `shell.cli`。
