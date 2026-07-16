# Claude Code Adapter

本页供 Agent 配置或接手 Claude Code 非 GUI 顶层入口。Claude Code 通过 marketplace 读取本仓库筛选后的非 GUI Skills；不能使用 Codex GUI、Computer Use 或 Codex 插件调用能力。

## 触发条件

仅在用户明确要求安装、验证或使用 Claude Code 入口时使用本页。普通非 GUI 汉化推进遵循 [Non-GUI Agent Workflow](./agent_workflow.md)。

## 前置检查

1. 确认仓库中的 `.claude-plugin/marketplace.json` 与 `.claude-plugin/plugin.json` 校验通过。
2. 确认 marketplace、Claude plugin、Codex plugin 和 `pyproject.toml` 版本一致。
3. 确认 marketplace 只暴露非 GUI Skills。
4. 接手已有工作区时，从 `.skyrim-chs-workspace.json` 读取插件源和游戏身份。

## 安装动作

当用户要求从 GitHub 安装时，提供 Claude Code `/plugin` 命令：

```text
/plugin marketplace add iambupu/SkyrimModTranslation@master
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

本地仓库调试：

```text
/plugin marketplace add D:\bupuy\Documents\SkyrimModTranslation
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

这些命令必须由 Claude Code 的 `/plugin` 处理，不能当作 Codex 或 PowerShell 命令执行。marketplace 元数据合同见 [Claude Code Marketplace](./claude_code_marketplace.md)。

## 验证证据

从插件源运行：

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\validate_claude_plugin_marketplace.py
python scripts\list_agent_skills.py --agent claude-code
```

只有 marketplace、capability 和 Skill 列表均通过时，Agent 才能报告 Claude Code 入口可用。

## 接手上下文

通用 checkpoint 检查、环境变量和接手顺序见 [Non-GUI Agent Workflow 的断点恢复](./agent_workflow.md#断点恢复)。Claude Code 接手仍优先读取 `qa/agent_handoff.json`，再读取 workflow state、workflow tasks 和 policy；它是顶层主控，不直接领取子任务。

## 停止条件

- marketplace、plugin manifest、版本或 Skill 清单校验失败；
- 工作区 marker 缺失、冲突或游戏身份不明确；
- handoff/context 生成失败；
- 当前任务需要 GUI、Computer Use、pywinauto/UI Automation 或 `gui:desktop` 锁。

GUI 任务记录 `blocked` 和 `handoff_target=codex`，不得把 marketplace 安装解释成获得 Codex 桌面能力。
