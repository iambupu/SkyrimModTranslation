# Claude Code Adapter

Claude Code 可以作为非 GUI 入口使用。它通过 Claude Code marketplace 安装本仓库的 Skill 指引，能读工作区状态、运行项目 Python 脚本、写 QA 报告；不能操作桌面窗口，也不能使用 Codex 的 GUI 或 Computer Use 能力。

Marketplace 安装：

```text
/plugin marketplace add iambupu/SkyrimModTranslation@master
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

本地调试：

```text
/plugin marketplace add D:\bupuy\Documents\SkyrimModTranslation
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

Claude marketplace 元数据见 [claude_code_marketplace.md](./claude_code_marketplace.md)。

如果当前目录是插件源仓库，可以检查 Claude Code 能看到哪些能力和 Skill：

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent claude-code
```

需要给 Claude Code 导出接手上下文时，明确指定插件源和工作区路径：

```powershell
$env:SKYRIM_CHS_PLUGIN_ROOT = "D:\bupuy\Documents\SkyrimModTranslation"
$env:SKYRIM_CHS_WORKSPACE_ROOT = "D:\SkyrimCHS\YourWorkspace"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\write_agent_handoff.py"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\export_agent_context.py" --agent claude-code --output qa/agent_context_prompts/latest.claude-code.context.md
```

如果当前目录是初始化后的工作区，先按 [agent_adapters.md](./agent_adapters.md) 从 `.skyrim-chs-workspace.json` 读取插件源路径，或设置 `SKYRIM_CHS_PLUGIN_ROOT`，再调用插件源脚本。

Claude marketplace 只安装非 GUI Skill 指引，不提供 Codex GUI、Computer Use 或 Codex 插件调用能力。

遇到需要桌面 GUI 的步骤时，记录 `blocked` 和 `handoff_target=codex`。

Claude Code 不直接领取 `qa/workflow_tasks.json` 子任务；子任务领取属于主控分派的子 agent。详细边界见 [agent_adapters.md](./agent_adapters.md)。
