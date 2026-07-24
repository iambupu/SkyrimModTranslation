# Claude Code Adapter

Claude Code 通过 marketplace 使用本仓库筛选后的非 GUI Skills。它与
Codex 使用同一个公开 `smt.py` 控制器，但不能使用 Codex GUI、Computer
Use 或 Codex 插件调用能力。

## 安装

从 GitHub 安装：

```text
/plugin marketplace add iambupu/SkyrimModTranslation@master
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

本地仓库调试：

```text
/plugin marketplace add D:\bupuy\Documents\SkyrimModTranslation
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

这些是 Claude Code `/plugin` 命令，不是 PowerShell 命令。

## 验证

从插件源运行：

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\validate_claude_plugin_marketplace.py
python scripts\list_agent_skills.py --agent claude-code
```

marketplace 必须只暴露非 GUI Skills，且版本必须与 Claude/Codex 插件清单
和 `pyproject.toml` 一致。

## 使用

首次翻译和后续推进遵循
[Non-GUI Agent Workflow](./agent_workflow.md)：顶层 Claude Code 只调用
`smt.py --format json run|status|resume|doctor|output` 并读取公开 JSON。
`qa/agent_handoff.json`、workflow state/tasks 和 policy 只供公开 CLI 内部
及被授权的运行期 Skill 使用，不是 Claude Code 的第二套顶层入口。

遇到 `needs_gui`、Computer Use、pywinauto/UI Automation 或
`gui:desktop` 时，安全停止并交给 Codex。安装 marketplace 不会赋予这些
能力。
