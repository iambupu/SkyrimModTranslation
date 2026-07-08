# Agent Compatibility

本项目不是去掉 Codex，而是把 Codex Plugin、opencode 和 Claude Code 都作为一等入口接入同一套 Skyrim CHS workflow core。

## 支持矩阵

| 能力 | Codex Plugin | opencode | Claude Code |
|---|---:|---:|---:|
| Codex 插件调用 | 是 | 否 | 否 |
| Claude Code marketplace | 否 | 否 | 是 |
| CLI/agent 主控 | 是 | 是 | 是 |
| 非 GUI Python workflow | 是 | 是 | 是 |
| 子 agent 并发任务编排 | 是 | 可作为主控读取/分派 | 可作为主控读取/分派 |
| portable `skills/` 读取 | 是 | 是 | 是 |
| 状态、QA、进度证据 | 是 | 是 | 是 |
| LexTranslator/xTranslator GUI | 是 | 否 | 否 |
| Computer Use / UI Automation | 是 | 否 | 否 |

Gemini CLI 没有支持计划。

## 性能边界

现有 Codex 插件入口和默认 workflow 热路径不能被新增 adapter 支持拖慢：

- 不在 Codex 默认流程中调用 adapter capability 探测。
- 不在 Codex 默认流程中导出大上下文包。
- 不改变 `write_codex_handoff.py` 的默认输出合同。
- 新增 agent context、agent handoff 和 Claude marketplace 校验只在显式命令或 CI 中运行。

## Codex-only GUI Boundary

GUI 操作只属于 Codex adapter。opencode 和 Claude Code 遇到以下任务时必须 blocked 并 handoff 给 Codex：

- `resource_locks` 含 `gui:desktop`
- task metadata 标记 `requires_gui=true`
- LexTranslator/xTranslator GUI fallback
- Computer Use、pywinauto、UI Automation
- 任何桌面坐标或窗口操作

标准结果：

```json
{
  "status": "blocked",
  "blockers": ["requires Codex GUI automation"],
  "handoff_target": "codex"
}
```

## 共享核心

Claude Code 通过 `.claude-plugin/marketplace.json` 支持 `/plugin marketplace add`，但 marketplace 只暴露非 GUI Skill。不要把它理解成 Claude Code 获得了 GUI、Computer Use 或 Codex 插件能力。

三个入口共享：

- `scripts/`
- `skills/`
- 插件源 `config/workflow_policy.json`
- 插件源 `config/agent_capabilities.example.json`
- `qa/workflow_state.json`
- `qa/workflow_tasks.json`
- `.workflow/progress_card.*`
- `qa/workflow_agent_runs.jsonl`

`qa/agent_handoff.json` 还包含 `resume_checkpoint`，用于跨 adapter 接手或中断后恢复时先读最小上下文。它只做索引和 stale 判断，不能替代 `workflow_state.json`、`workflow_tasks.json` 或 QA 门禁。

根目录 `skills/` 是唯一运行 Skill 目录。不要为 opencode 或 Claude Code 复制第二套 Skill。
在初始化后的工作区中运行 agent 辅助脚本时，脚本会通过 `SKYRIM_CHS_PLUGIN_ROOT` 或工作区 marker 读取插件源的 adapter metadata 和 Skills；工作区只保存 QA 状态、运行输出和本机工具配置。

## 入口

以下是插件源仓库中的短命令；初始化后的工作区请按 [agent_adapters.md](./agent_adapters.md) 从 `.skyrim-chs-workspace.json` 读取插件源路径，或设置 `SKYRIM_CHS_PLUGIN_ROOT` 后调用插件源脚本。

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent opencode
python scripts\list_agent_skills.py --agent claude-code
python scripts\validate_claude_plugin_marketplace.py
```

导出 agent context 或 agent handoff 时必须显式指定工作区，避免把 `qa/agent_context_prompts/` 写到插件源码仓库：

```powershell
$env:SKYRIM_CHS_PLUGIN_ROOT = "D:\bupuy\Documents\SkyrimModTranslation"
$env:SKYRIM_CHS_WORKSPACE_ROOT = "D:\SkyrimCHS\YourWorkspace"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\write_agent_handoff.py"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\export_agent_context.py" --agent claude-code --output qa/agent_context_prompts/latest.claude-code.context.md
```

opencode 和 Claude Code 是顶层非 GUI adapter，不直接领取 `qa/workflow_tasks.json` 子任务；子任务领取只属于主控分派的子 agent。
