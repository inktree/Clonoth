<div align="center">

<img src="docs/assets/logo.png" width="360" />

# Clonoth

</div>

Clonoth 是一个模块化、可扩展的多平台 AI Agent 框架。面向长期运行的 Agent 服务场景，将任务调度、模型推理、平台接入、工具调用、技能注入和记忆管理拆分为独立模块。

## 架构概览

Clonoth 采用 Supervisor + Engine + Bot Adapter 的多进程架构。

- **Supervisor** 负责进程管理、会话管理、任务路由、审批策略、事件流广播（WebSocket `/v1/ws`）、用量计费统计（SQLite `usage_store`）、本地 ONNX TTS / Mimo 语音合成转写、计划任务和管理接口。
- **Engine** 负责执行 AI 节点，包含模块化推理循环（`engine/inference/`）、内建插件体系（`engine/builtin/`：渐进式知识/技能大纲注入、记忆自动提取、Dream 记忆归并、上下文压缩、MCP 运行时、异步工具调度）、Hook 执行与信号总线（`engine/signals/`）。
- **Bot Adapter** 负责接入外部平台（Discord、QQ/OneBot、Web 前端），通过共享的确定性文本切分与 TTS 标签解析模块（`adapters/message_text.py`）处理长消息分段、代码围栏闭合与多后端语音收发。
- **SDK (`clonoth_sdk/`)** 为平台适配器提供纯 WebSocket 事件路由（`EventRouter`）、会话与触发状态管理（`SessionState`）、审批去重与自动放行（`ApprovalTracker`）及回调协议（`AdapterCallbacks`）。

这种拆分方式使平台接入、运行时控制和模型执行可以独立演进，也便于在不同部署环境中按需组合。

## 核心特性

### Provider 插件化

`providers` 包提供统一的模型 Provider 接口。`ProviderRegistry` 负责发现和注册 Provider 实现，新增模型后端时通常只需要新增对应的 `providers/*.py` 文件，并按约定暴露实现类。

当前目录中包含 OpenAI、OpenAI Responses、Anthropic、Gemini、DeepSeek 等 Provider 实现，统一支持流式推理思考链（Reasoning）回传、Prompt Cache 断点对齐与多维 Token/费用统计。

### Hook 系统

`engine/hooks` 提供 Hook 系统。Hook 处理器通过 `Handler`、`HookContext` 和 `HookResult` 与执行循环交互，可以在模型调用、工具调用或步骤执行前后插入检查、改写或拦截逻辑。

Hook 系统将运行时策略从主推理流程中拆出，便于测试和维护。

### 外部插件

`plugins` 目录支持外部插件。插件通过 `PLUGIN_META`（自动发现）或 `register(ctx)` 函数（收到 EngineContext 统一注册面入口）注册 hook handler、工具、HTTP 路由和前端贡献（面板/槽位/样式）。示例文件 `plugins/example_hook.py.disabled` 展示了基本协议，去掉 `.disabled` 后缀即可作为插件模板启用；运行时装卸见设置区「插件」管理页。

### 多平台接入与本地语音（TTS / ASR）

Clonoth 内置完整的 **Web 前端**、**Discord 适配器**与 **QQ / OneBot (NapCat) 适配器**：

- **确定性消息切分（`adapters/message_text.py`）**：统一处理 Discord / OneBot 的 `[SPLIT]` / `[LEFT]` 标记、自然边界长文切分、缩进保留与跨段代码块（` ``` `）自动闭合重开，无需额外的后置 QA 节点。
- **双后端语音支持**：统一通过 Supervisor `/v1/audio/synthesize` 与 `/v1/audio/transcribe` 提供语音能力，支持 `<tts backend="ryza_onnx" preset="normal_cn">...</tts>`（本地 Ryza v2ProPlus ONNX 引擎，支持按需懒加载与空闲自动卸载释放显存/内存）与 `<tts backend="mimo">...</tts>`（Mimo 云端语音合成/识别）。

### 工具系统

`tools`（外部子进程工具）和 `toolbox`（内建进程内工具与远程 Worker 转发）提供统一 `(args, ctx)` ABI 的工具注册与执行支持，内置文件读写、差异补丁（`apply_diff`）、目录遍历（`list_dir`）、全文检索（`search_in_files`）、命令执行（支持超时自动升级异步通知）、多模态媒体理解（`read_media`）、Discord Bridge 管理与 MCP 持久连接池。

### 技能与记忆系统（渐进式大纲注入）

`engine/builtin/knowledge_inject` 统一管理 `skills/` 与长期记忆的上下文注入：

- **渐进式披露（Progressive Disclosure）**：超长技能与记忆自动提取结构化标题大纲注入系统提示词，Agent 按需调用工具拉取全文，大幅降低常驻 Prompt Token 消耗并提升缓存命中率。
- **自动提取与 Dream 归并**：支持对话空闲/增量触发 `engine/builtin/memory_extract.py` 提取记忆碎片，并由 `engine/builtin/dream.py` 定时归并整理。

## 目录结构

```text
Clonoth/
├── engine/                     # AI 节点执行核心
│   ├── inference/              # 推理主循环 (ai_step, llm_call, tool_format, pseudo_handlers, async_tools)
│   ├── builtin/                # 内建核心插件 (knowledge_inject, memory_extract, dream, compact, mcp)
│   ├── hooks/                  # Hook 注册表与生命周期拦截协议
│   ├── faces/                  # 插件能力注册面 (tools, nodes, routes, prompt_sections, ui)
│   ├── signals/                # 进程内信号总线与 JSONL 桥接
│   ├── tool_result_formatters.py # 单阶段工具结果摘要/详情格式化器
│   └── runner.py               # Worker 进程主循环与任务拉取
├── supervisor/                 # 控制面与调度中心
│   ├── api.py                  # FastAPI HTTP + WebSocket (/v1/ws) 接口
│   ├── task_router.py          # 任务完成态统一状态机路由与分支合并
│   ├── session.py              # 会话管理、Fork/Merge 分支与级联清理
│   ├── local_onnx_tts.py       # 本地 Ryza ONNX TTS 懒加载与空闲自动卸载引擎
│   ├── usage_store.py          # SQLite 多维 Token 用量与模型计费统计
│   └── process_manager.py      # Engine Workers 与平台适配器子进程生命周期管理
├── providers/                  # 模型后端实现 (openai, openai_responses, anthropic, gemini, deepseek)
├── toolbox/                    # 内建工具集 (builtins/)、统一上下文与远程 Worker 运行时
├── tools/                      # 外部子进程工具 (discord_manage, clear_context, read_media, clonoth_debug)
├── clonoth_sdk/                # 平台无关协议 SDK (ClonothClient, EventRouter, SessionState, ApprovalTracker)
├── adapters/                   # 多平台接入层
│   ├── message_text.py         # 跨平台确定性文本切分、代码围栏闭合与 <tts> 标签解析
│   ├── discord/                # Discord Bot 适配器与本地 Bridge Server
│   ├── onebot/                 # QQ / OneBot v11 (NapCat) 适配器
│   └── web/                    # React + TypeScript + Vite 管理与对话前端
├── plugins/                    # 外部扩展插件 (ide, plugin_manager, message_retry, reroll_button)
├── remote_worker_sdk/          # 远程工具执行 Worker SDK
├── config/                     # 运行时配置 (runtime.yaml) 与节点定义 (nodes/*.yaml)
├── skills/                     # 技能说明文档库 (SKILL.md)
└── docs/                       # 架构设计与模块说明文档
```

## 快速开始

### 1. 准备环境

需要 Python 3.11 或更高版本。如需 Web 前端，还需要 Node.js 18+ 和 npm。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，填入 LLM Provider 的 API Key 和端点：

```bash
cp .env.example .env
```

最小可运行配置只需填写三个变量：

```env
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-your-key-here
OPENAI_MODEL=gpt-4o-mini
```

支持任意 OpenAI 兼容端点（本地 Ollama、代理服务等）。其他 Provider（Anthropic、DeepSeek、Gemini）的配置见 `.env.example` 中的注释。

### 3. 准备运行配置

```bash
mkdir -p data
cp config.example.yaml data/config.yaml
cp policy.example.yaml data/policy.yaml
```

配置文件的职责：

- **`.env`**：环境变量，存放 API Key、Bot Token 等敏感信息。不提交到仓库。
- **`data/config.yaml`**：Provider 选择、模型、base URL 与语音配置。支持 `${ENV_VAR}` 引用环境变量。不提交到仓库。
- **`data/policy.yaml`**：安全策略（文件读写权限、命令执行审批规则、`extra_roots`）。
- **`config/runtime.yaml`**：Engine 与 Supervisor 运行时参数（步数限制、压缩阈值、worker 数量、适配器与本地 TTS 开关等），可提交到仓库。
- **`config/runtime.local.yaml` & `config/nodes/*.local.yaml`**（可选）：本机私有覆盖配置，自动与主配置深度合并，已被 Git 忽略，便于多工作树/本地真机调试。

`data/config.yaml` 默认通过 `${OPENAI_BASE_URL}` 等引用 `.env` 中的变量，通常无需额外编辑。

### 4. 构建 Web 前端（可选）

```bash
cd adapters/web/frontend
npm ci
npm run build
cd ../../..
```

Supervisor 启动后会自动挂载 `adapters/web/frontend/dist/` 到 `/web/` 路径。如果不需要 Web 界面，可跳过此步。

### 5. 启动服务

```bash
python main.py
```

Supervisor 默认监听 `127.0.0.1:8765`。可通过环境变量修改：

```env
CLONOTH_HOST=0.0.0.0
CLONOTH_PORT=8765
```

启动后访问 `http://localhost:8765/web/` 进入 Web 界面。

生产环境建议使用 systemd、PM2 等进程管理器。如果使用 PM2，可参考根目录中的 `ecosystem.config.js`。

### 6. 一步部署（快捷命令）

以上步骤合并为一条命令：

```bash
python3.11 -m venv .venv && \
. .venv/bin/activate && \
python -m pip install -U pip && \
python -m pip install -r requirements.txt && \
cp .env.example .env && \
mkdir -p data && \
( test -f data/config.yaml || cp config.example.yaml data/config.yaml ) && \
( test -f data/policy.yaml || cp policy.example.yaml data/policy.yaml ) && \
( cd adapters/web/frontend && npm ci && npm run build ) && \
echo "编辑 .env 填入 API Key，然后运行 python main.py"
```

### 7. 运行测试

```bash
pytest
```

### 环境变量参考

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `OPENAI_BASE_URL` | 全局 LLM API 端点 | `https://api.openai.com/v1` |
| `OPENAI_API_KEY` | 全局 LLM API Key | 无（必填） |
| `OPENAI_MODEL` | 全局默认模型 | `gpt-4o-mini` |
| `ANTHROPIC_API_KEY` | Anthropic Provider Key | fallback 到全局 |
| `DEEPSEEK_API_KEY` | DeepSeek Provider Key | fallback 到全局 |
| `GEMINI_API_KEY` | Gemini Provider Key | fallback 到全局 |
| `CLONOTH_HOST` | Supervisor 监听地址 | `127.0.0.1` |
| `CLONOTH_PORT` | Supervisor 监听端口 | `8765` |
| `CLONOTH_LOG_LEVEL` | 日志级别 | `info` |
| `CLONOTH_ADMIN_TOKEN` | Admin API 认证 token | 自动生成 |

## 详细文档

更多设计和集成说明见 `docs/` 目录：

- [架构说明](docs/architecture.md)
- [Bot 集成指南](docs/bot_integration.md)
- [工具系统](docs/tools_and_evolution.md)
- [插件系统](docs/plugin-system.md)
- [Skill 系统](docs/skills.md)
- [配置说明](docs/configuration.md)

## 扩展入口

- 新增模型后端：在 `providers/` 中新增 Provider 实现，并接入 `ProviderRegistry` 约定。
- 新增 Hook：实现 `Handler`，通过 Hook 注册表注册到对应 Hook 点。
- 新增外部插件：在 `plugins/` 中创建插件文件，声明 `PLUGIN_META` 或提供 `register(ctx)` 函数。
- 新增工具：在 `tools/` 或 `toolbox/` 的约定位置实现工具，并完成注册。
- 新增技能：在 `skills/` 中添加技能说明文件，并配置触发策略。

## 许可

请以仓库中实际提供的许可文件为准。
