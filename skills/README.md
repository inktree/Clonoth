# BotTTS Skills

这里维护 BotTTS 的业务 skills。

当前阶段聚焦本地开发，不做 QQ / Discord 实际接入。

## 当前 skills

| Skill | 策略 | 用途 |
|---|---|---|
| `local-development` | constant | 本地开发、同步、启动流程。 |
| `project-structure` | constant | BotTTS 与 Clonoth 分层维护规范。 |
| `response-quality` | constant | 本地助手回复质量规范。 |
| `memory-guidelines` | constant | BotTTS 长期记忆提取与使用规范。 |
| `local-voice-development` | constant | 本地语音工作流开发规范。 |
| `local-troubleshooting` | normal | 本地运行故障排查，按关键词触发。 |

## 格式

每个 skill 位于独立目录中：

```text
skills/<skill-name>/SKILL.md
```

`SKILL.md` 使用 YAML frontmatter：

```yaml
---
name: skill-name
description: skill description
enabled: true
strategy: constant
order: 0
priority: 0
node_ids:
  - local_assistant
---
```

## 同步

执行：

```powershell
cd ..\BotTTS
.\scripts\sync-to-clonoth.ps1
```

同步到：

```text
Clonoth/skills/
```
