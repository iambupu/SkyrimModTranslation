# Agent Adapters

本项目支持三个一等入口：Codex Plugin、opencode、Claude Code。

- Codex 是默认完整 adapter，保留 Codex 插件调用、GUI、Computer Use 和桌面自动化能力。
- opencode 是完整非 GUI adapter，可以作为顶层 CLI agent/controller 使用共享 workflow core。
- Claude Code 是完整非 GUI adapter，并通过 `.claude-plugin/marketplace.json` 暴露非 GUI Skill 入口。

顶层 adapter 不是子任务执行器。不要让 opencode 或 Claude Code 直接领取 `qa/workflow_tasks.json` 中的子任务。

## Read Order

顶层 adapter 接手工作区时先读：

1. `qa/agent_handoff.json`
2. `qa/codex_handoff.json`（兼容 fallback）
3. `qa/workflow_state.json`
4. `qa/workflow_tasks.json`
5. `qa/translation_readiness.json`
6. 插件源 `config/workflow_policy.json`
7. 插件源 `config/agent_capabilities.example.json`

初始化后的工作区不包含 `scripts/`、`skills/` 或 adapter 源码。需要运行插件脚本时，先从 `.skyrim-chs-workspace.json` 或 `SKYRIM_CHS_PLUGIN_ROOT` 找到插件源仓库，再调用插件源脚本。

## Low-Context Resume

`qa/agent_handoff.json` 的 `resume_checkpoint` 是断点恢复入口索引。opencode 或 Claude Code 接手时可以先读它，按 `next_read_set` 和 `artifact_refs` 缩小上下文读取范围，避免重新扫描全部报告、译文和产物。

这个 checkpoint 不替代 `qa/workflow_state.json` 或 `qa/workflow_tasks.json`。如果 `stale_if_newer_than.watch` 中任一路径的 `latest_mtime_utc` 晚于 checkpoint 生成时间，先刷新状态链，再信任 handoff。

## Subagent Task Claims

`qa/workflow_tasks.json` 是主控生成的可调度视图。领取其中子任务的是主控分派的子 agent，不是 opencode/Claude Code 这类顶层 adapter。

子 agent 领取协议：

```powershell
python scripts\claim_workflow_task.py --mod-name <ModName> --owner <SubagentId> --parallel-only
python scripts\claim_workflow_task.py --mod-name <ModName> --resource-lock <ResourceLock> --owner <SubagentId> --parallel-only
python scripts\claim_workflow_task.py --task-id <TaskId> --owner <SubagentId> --complete --complete-status done --exit-code 0 --output-tail "<short result>"
```

子 agent 只能执行领取结果里的 `command`。并发批次结束后，由主控串行刷新 readiness、workflow state、workflow tasks、Codex handoff、进度卡和 blockers；只有在准备跨 adapter 接手时，才显式运行 `write_agent_handoff.py` 生成 agent-neutral handoff。

## Codex-Only GUI Boundary

GUI 操作只属于 Codex adapter：

- `resource_locks` 含 `gui:desktop`
- LexTranslator/xTranslator GUI fallback
- Computer Use
- pywinauto/UI Automation
- 桌面坐标或窗口操作

opencode 和 Claude Code 遇到 GUI-only 步骤时必须阻断，并记录 `handoff_target=codex`。Claude Code marketplace 安装不代表 Claude Code 获得 Codex GUI、Computer Use 或 Codex 插件调用能力。

## Explicit Adapter Helpers

这些脚本是显式 adapter 辅助，不在 Codex 默认翻译热路径中运行：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --no-launch
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace --no-launch
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent opencode
python scripts\list_agent_skills.py --agent claude-code
python scripts\validate_claude_plugin_marketplace.py
```

`init_opencode.py` 是 opencode 的一键初始化/启动入口。它只写入目标工作区的 `opencode.json`、`.opencode/` 配置、agent-neutral handoff 和 bounded context packet；不会复制插件源 `scripts/`、`skills/` 或 adapter 源码到工作区。

`uv run` 是推荐的便捷启动方式，但不是 adapter 能力前提；所有 helper 仍保留 `python` 直接运行路径。

`write_agent_handoff.py` 和 `export_agent_context.py` 会读写工作区 `qa/`。如果从插件源仓库手工运行，必须显式指定插件源和目标工作区：

```powershell
$env:SKYRIM_CHS_PLUGIN_ROOT = "D:\bupuy\Documents\SkyrimModTranslation"
$env:SKYRIM_CHS_WORKSPACE_ROOT = "D:\SkyrimCHS\YourWorkspace"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\write_agent_handoff.py"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\export_agent_context.py" --agent claude-code --output qa/agent_context_prompts/latest.claude-code.context.md
```

`write_agent_handoff.py` 只在准备 agent-neutral/opencode/Claude Code handoff 时运行；Codex 默认兼容视图仍由 `write_codex_handoff.py` 生成。
