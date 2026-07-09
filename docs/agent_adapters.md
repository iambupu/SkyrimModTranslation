# Agent 入口说明

本页说明 Codex、opencode 和 Claude Code 怎么接同一个汉化工作区。这里不比较模型，只写三件事：谁能做什么、接手先读什么、遇到 GUI 步骤怎么停。

- Codex 是默认入口，也是唯一能处理 GUI 后备的入口。
- opencode 适合命令行使用，只处理非 GUI 步骤；初始化脚本会生成工作区本地 opencode 插件。
- Claude Code 也只处理非 GUI 步骤，可以通过 `.claude-plugin/marketplace.json` 安装 Skill。

opencode 和 Claude Code 是顶层入口，不是子任务执行器。`qa/workflow_tasks.json` 里的任务应由主控分派给子 agent 领取，不要让顶层入口直接抢任务。

## 接手时先读

接手一个工作区时，按这个顺序读：

1. `qa/agent_handoff.json`
2. `qa/codex_handoff.json`（兼容 fallback）
3. `qa/workflow_state.json`
4. `qa/workflow_tasks.json`
5. `qa/translation_readiness.json`
6. 插件源 `config/workflow_policy.json`
7. 插件源 `config/agent_capabilities.example.json`

初始化后的工作区不包含 `scripts/`、`skills/` 或适配器源码。需要运行脚本时，先从 `.skyrim-chs-workspace.json` 或 `SKYRIM_CHS_PLUGIN_ROOT` 找到插件源仓库，再调用插件源脚本。

## 断点恢复

`qa/agent_handoff.json` 里的 `resume_checkpoint` 用来减少重复读取。opencode 或 Claude Code 接手时，可以先按 `next_read_set` 和 `artifact_refs` 读取最相关的报告、译文和产物。

这个 checkpoint 只做索引，不能取代 `qa/workflow_state.json` 或 `qa/workflow_tasks.json`。如果 `stale_if_newer_than.watch` 里的路径比 checkpoint 更新，先刷新状态链，再继续。

## 子任务领取

`qa/workflow_tasks.json` 是主控生成的任务视图。领取任务的是主控分派的子 agent，不是 opencode 或 Claude Code 这类顶层入口。

子 agent 领取协议：

```powershell
python scripts\claim_workflow_task.py --mod-name <ModName> --owner <SubagentId> --parallel-only
python scripts\claim_workflow_task.py --mod-name <ModName> --resource-lock <ResourceLock> --owner <SubagentId> --parallel-only
python scripts\claim_workflow_task.py --task-id <TaskId> --owner <SubagentId> --complete --complete-status done --exit-code 0 --output-tail "<short result>"
```

子 agent 只能执行领取结果里的 `command`。一批并发任务结束后，由主控串行刷新 readiness、workflow state、workflow tasks、Codex handoff、进度卡和 blockers。只有准备交给 opencode 或 Claude Code 接手时，才运行 `write_agent_handoff.py`。

## GUI 只给 Codex

下面这些步骤只能由 Codex 处理：

- `resource_locks` 含 `gui:desktop`
- LexTranslator/xTranslator GUI fallback
- Computer Use
- pywinauto/UI Automation
- 桌面坐标或窗口操作

opencode 和 Claude Code 遇到这些步骤时要停下，记录 `handoff_target=codex`。Claude Code marketplace 安装不会让 Claude Code 获得 Codex GUI、Computer Use 或 Codex 插件调用能力。

## 手工命令

这些脚本只在手工检查、初始化或 CI 中运行，不进入 Codex 默认翻译流程：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --no-launch
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --no-launch
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent opencode
python scripts\list_agent_skills.py --agent claude-code
python scripts\validate_claude_plugin_marketplace.py
```

`init_opencode.py` 是 opencode 的一键初始化和启动入口。它只写入目标工作区的 `opencode.json`、`.opencode/` 配置、本地插件、接手报告和上下文包；不会把插件源 `scripts/`、`skills/` 或适配器源码复制到工作区。

这里生成的是目标工作区内的 `.opencode/plugins/skyrim-chs.js` 本地插件，只用于注入环境变量和恢复提示。插件源码仓库不跟踪 `.opencode/plugins/`，opencode 也不会因此获得 GUI 能力。

推荐用 `uv run` 少输环境配置命令；没有 uv 时，仍然可以直接用 `python`。

`write_agent_handoff.py` 和 `export_agent_context.py` 会读写工作区 `qa/`。如果从插件源仓库手工运行，必须明确指定插件源和目标工作区：

```powershell
$env:SKYRIM_CHS_PLUGIN_ROOT = "D:\bupuy\Documents\SkyrimModTranslation"
$env:SKYRIM_CHS_WORKSPACE_ROOT = "D:\SkyrimCHS\YourWorkspace"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\write_agent_handoff.py"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\export_agent_context.py" --agent claude-code --output qa/agent_context_prompts/latest.claude-code.context.md
```

`write_agent_handoff.py` 只在准备交给 opencode 或 Claude Code 接手时运行；Codex 默认视图仍由 `write_codex_handoff.py` 生成。
