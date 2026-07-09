# opencode Adapter

opencode 可以作为命令行入口处理非 GUI 汉化流程。它能读工作区状态、运行项目 Python 脚本、写 QA 报告；不能操作桌面窗口，也不能替 Codex 处理 LexTranslator/xTranslator GUI 后备。

opencode 支持本地插件。项目级 `.opencode/plugins/` 会在启动时自动加载；这里用 `.opencode/plugins/skyrim-chs.js` 注入环境变量和断点恢复提示。

`.opencode/plugins/` 是工作区目录，不是插件源码仓库目录。只有运行 `init_opencode.py` 初始化某个工作区后，目标工作区里才会出现这个本地插件。

## 一键初始化和启动

从插件源仓库运行：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

没有安装 uv 时，继续使用 Python：

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

这个命令会：

- 在目标不存在或为空时先调用 `init_workspace.py` 初始化工作区。
- 写入 opencode 配置：`opencode.json`、`.opencode/AGENTS.md`、`.opencode/agents/skyrim-chs.md`、`.opencode/commands/skyrim-chs-*.md`、`.opencode/plugins/skyrim-chs.js` 和 `.opencode/skyrim-chs-opencode.json`。
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

`uv` 不是硬依赖。`scripts/init_opencode.py` 和所有流程入口仍可用 `python` 直接运行。工作区 `--tool-setup auto` 检测到 uv 时，会优先用 `uv venv` / `uv pip install` 准备 `tools/python-venv/`，失败时回退到 `venv + pip`。

本地插件只做两件事：

- 给 opencode shell 注入 `SKYRIM_CHS_PLUGIN_ROOT`、`SKYRIM_CHS_WORKSPACE_ROOT` 和 `OPENCODE_CONFIG_DIR`。
- 在会话压缩时追加最小恢复提示，提醒下一轮先读 context、handoff、workflow state 和 workflow tasks。

如果当前目录是插件源仓库，也可以检查 opencode 能看到哪些能力和 Skill：

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent opencode
```

需要给 opencode 导出接手上下文时，明确指定插件源和工作区路径：

```powershell
$env:SKYRIM_CHS_PLUGIN_ROOT = "D:\bupuy\Documents\SkyrimModTranslation"
$env:SKYRIM_CHS_WORKSPACE_ROOT = "D:\SkyrimCHS\YourWorkspace"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\write_agent_handoff.py"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\export_agent_context.py" --agent opencode --output qa/agent_context_prompts/latest.opencode.context.md
```

如果当前目录是初始化后的工作区，先按 [agent_adapters.md](./agent_adapters.md) 从 `.skyrim-chs-workspace.json` 读取插件源路径，或设置 `SKYRIM_CHS_PLUGIN_ROOT`，再调用插件源脚本。

遇到需要桌面 GUI 的步骤时，记录 `blocked` 和 `handoff_target=codex`。

opencode 不直接领取 `qa/workflow_tasks.json` 子任务；子任务领取属于主控分派的子 agent。详细边界见 [agent_adapters.md](./agent_adapters.md)。
