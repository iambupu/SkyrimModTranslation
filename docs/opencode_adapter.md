# opencode Adapter

opencode 是完整非 GUI adapter。它可以作为 CLI agent/controller 接入 workflow core，但不能执行 GUI/desktop automation。

## 一键初始化和启动

从插件源仓库运行：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

没有安装 uv 时继续使用 Python：

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

该命令会：

- 在目标不存在或为空时先调用 `init_workspace.py` 初始化工作区。
- 写入工作区内 `opencode.json`、`.opencode/AGENTS.md`、`.opencode/agents/skyrim-chs.md`、`.opencode/commands/skyrim-chs-*.md` 和 `.opencode/skyrim-chs-opencode.json`。
- 刷新 readiness、workflow state、workflow tasks、agent handoff 和 Codex handoff。
- 导出 `qa/agent_context_prompts/latest.opencode.context.md`。
- 默认启动 opencode TUI，并使用 `skyrim-chs` primary agent。

只生成配置和上下文、不启动 opencode：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --no-launch
```

非交互执行一次：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --launch-mode run --auto
```

`uv` 不是硬依赖。`scripts/init_opencode.py` 和所有 workflow 入口仍可用 `python` 直接运行；工作区 `--tool-setup auto` 会在检测到 uv 时优先使用 `uv venv` / `uv pip install` 准备 `tools/python-venv/`，失败时回退到 `venv + pip`。

如果当前目录是插件源仓库，也可直接运行 adapter 能力和 Skill 可见性检查：

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent opencode
```

需要导出某个工作区的 opencode 接手上下文时，必须显式指定插件源和工作区路径：

```powershell
$env:SKYRIM_CHS_PLUGIN_ROOT = "D:\bupuy\Documents\SkyrimModTranslation"
$env:SKYRIM_CHS_WORKSPACE_ROOT = "D:\SkyrimCHS\YourWorkspace"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\write_agent_handoff.py"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\export_agent_context.py" --agent opencode --output qa/agent_context_prompts/latest.opencode.context.md
```

如果当前目录是初始化后的工作区，先按 [agent_adapters.md](./agent_adapters.md) 从 `.skyrim-chs-workspace.json` 读取插件源路径，或设置 `SKYRIM_CHS_PLUGIN_ROOT`，再调用插件源脚本。

遇到 GUI-only workflow step 时记录 `blocked` 和 `handoff_target=codex`。

opencode 不直接领取 `qa/workflow_tasks.json` 子任务；子任务领取属于主控分派的子 agent。详细边界见 [agent_adapters.md](./agent_adapters.md)。
