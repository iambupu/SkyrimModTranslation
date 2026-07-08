# Claude Code Adapter

Claude Code 是完整非 GUI adapter，并且支持 Claude Code marketplace。它可以通过 `/plugin marketplace add` 安装本项目的非 GUI Skill 入口，也可以作为 CLI agent/controller 接入 workflow core，但不能执行 GUI/desktop automation。

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

如果当前目录是插件源仓库，可直接运行 adapter 能力和 Skill 可见性检查：

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent claude-code
```

需要导出某个工作区的 Claude Code 接手上下文时，必须显式指定插件源和工作区路径：

```powershell
$env:SKYRIM_CHS_PLUGIN_ROOT = "D:\bupuy\Documents\SkyrimModTranslation"
$env:SKYRIM_CHS_WORKSPACE_ROOT = "D:\SkyrimCHS\YourWorkspace"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\write_agent_handoff.py"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\export_agent_context.py" --agent claude-code --output qa/agent_context_prompts/latest.claude-code.context.md
```

如果当前目录是初始化后的工作区，先按 [agent_adapters.md](./agent_adapters.md) 从 `.skyrim-chs-workspace.json` 读取插件源路径，或设置 `SKYRIM_CHS_PLUGIN_ROOT`，再调用插件源脚本。

Claude marketplace 只安装非 GUI Skill 指引，不提供 Codex GUI、Computer Use 或 Codex 插件调用能力。

遇到 GUI-only workflow step 时记录 `blocked` 和 `handoff_target=codex`。

Claude Code 不直接领取 `qa/workflow_tasks.json` 子任务；子任务领取属于主控分派的子 agent。详细边界见 [agent_adapters.md](./agent_adapters.md)。
