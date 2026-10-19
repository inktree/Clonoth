---
name: project-structure
description: BotTTS 与 Clonoth 分层维护规范
enabled: true
strategy: constant
order: 5
priority: 8
node_ids:
  - local_assistant
---

# BotTTS 项目结构维护规范

当用户要求调整项目结构、迁移配置、整理目录或参考 EreunaMain 的组织方式时，按照本规范处理。

## 分层模型

BotTTS 不是 Clonoth 的 fork，而是运行在 Clonoth 框架之上的业务项目。

推荐关系：

```text
BotTTS/                  # 业务层
  config/nodes/          # 业务节点
  skills/                # 业务技能
  adapters/              # 后续平台适配器
  scripts/               # 同步、启动、检查脚本
  docs/                  # BotTTS 文档

Clonoth/                 # 框架运行实例
  config/nodes/          # Clonoth 实际加载的节点
  skills/                # Clonoth 实际加载的 skills
  data/                  # 本地配置、数据、日志
  supervisor/ engine/    # 框架源码
```

## 与 EreunaMain 的对应关系

| EreunaMain | BotTTS | 说明 |
|---|---|---|
| `EreunaMain/nodes/` | `BotTTS/config/nodes/` | 业务节点源文件。 |
| `EreunaMain/skills/` | `BotTTS/skills/` | 业务 skills 源文件。 |
| `discord_bot.py` | 暂无，后续可放 `BotTTS/adapters/` | 平台 adapter。当前阶段暂不接入。 |
| 复制/软链接到 Clonoth | `sync-to-clonoth.ps1` | 当前使用复制同步，避免 Windows 软链接权限问题。 |

## 文件放置规则

### 应放在 BotTTS 中

- BotTTS 专属节点 prompt。
- BotTTS 专属 skills。
- 本地开发文档。
- 同步脚本。
- 未来 QQ / OneBot adapter。
- 业务侧部署说明。

### 可放在 Clonoth 中

- 当前运行实例的 `.env`。
- 当前运行实例的 `data/config.yaml`。
- 当前运行实例的 `data/policy.yaml`。
- 必须由 Clonoth 直接读取的 runtime 配置。

### 尽量不要修改

- `Clonoth/engine/`
- `Clonoth/supervisor/`
- `Clonoth/toolbox/`
- `Clonoth/providers/`

除非是框架 bug、兼容性修复或用户明确要求。

## 同步策略

当前默认使用复制同步：

```powershell
BotTTS/scripts/sync-to-clonoth.ps1
```

选择复制而不是软链接的原因：

- Windows 下软链接可能需要管理员权限或开发者模式。
- 复制更接近部署行为。
- 复制失败时更容易排查。

## 判断修改位置

当需要修改时，先判断：

1. 这是业务行为吗？
   - 是：改 `BotTTS/`。
2. 这是运行实例配置吗？
   - 是：改 `Clonoth/data/`、`Clonoth/.env` 或 `Clonoth/config/runtime.yaml`。
3. 这是框架 bug 吗？
   - 是：可以改 `Clonoth/` 源码，但应记录原因。
4. 这是平台接入吗？
   - 当前阶段先暂缓；如果必须做，放 `BotTTS/adapters/`。
