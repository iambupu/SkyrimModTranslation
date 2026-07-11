# Agent Compatibility

Codex 仍是默认完整入口。opencode 和 Claude Code 读同一套工作区状态和 QA 报告，但只能做非 GUI 工作。

## 支持矩阵

| 能力 | Codex Plugin | opencode | Claude Code |
|---|---:|---:|---:|
| Codex 插件调用 | 是 | 否 | 否 |
| opencode 本地插件 | 否 | 是 | 否 |
| Claude Code marketplace | 否 | 否 | 是 |
| CLI/agent 主控 | 是 | 是 | 是 |
| 非 GUI Python workflow | 是 | 是 | 是 |
| 子 agent 并发任务编排 | 是 | 可作为主控读取/分派 | 可作为主控读取/分派 |
| portable `skills/` 读取 | 是 | 是 | 是 |
| 状态、QA、进度证据 | 是 | 是 | 是 |
| LexTranslator/xTranslator GUI | 是 | 否 | 否 |
| Computer Use / UI Automation | 是 | 否 | 否 |

Gemini CLI 没有支持计划。

## 不拖慢 Codex

opencode 和 Claude Code 的支持不能拖慢 Codex 默认流程：

- 不在 Codex 默认流程中调用 adapter 能力探测。
- 不在 Codex 默认流程中导出大上下文包。
- 不改变 `write_codex_handoff.py` 的默认输出合同。
- 新增 agent context、agent handoff 和 Claude marketplace 校验只在手工命令或 CI 中运行。

## GUI 只给 Codex

GUI 操作只属于 Codex。opencode 和 Claude Code 遇到以下任务时必须停下，并交回 Codex：

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

## 共享内容

Claude Code 通过 `.claude-plugin/marketplace.json` 支持 `/plugin marketplace add`，但 marketplace 只暴露非 GUI Skill。不要把它理解成 Claude Code 获得了 GUI、Computer Use 或 Codex 插件能力。

opencode 使用工作区本地 `.opencode/plugins/skyrim-chs.js` 注入环境变量和恢复提示，并用 `.opencode/skills/` 轻量指针接入原生 Skill 发现；不增加 GUI 能力。

三个入口共享：

- `scripts/`
- `skills/`
- 插件源 `config/workflow_policy.json`
- 插件源 `config/agent_capabilities.example.json`
- `qa/workflow_state.json`
- `qa/workflow_tasks.json`
- `.workflow/progress_card.*`
- `qa/workflow_agent_runs.jsonl`

`qa/agent_handoff.json` 还包含 `resume_checkpoint`，用于跨入口接手或中断后恢复时减少重复读取。它只做索引和过期判断，不能替代 `workflow_state.json`、`workflow_tasks.json` 或 QA 门禁。

根目录 `skills/` 是唯一权威运行 Skill 目录。不要复制第二套 Skill 正文；opencode 工作区里的 `.opencode/skills/` 只保存指向插件源的发现指针。
在初始化后的工作区中运行辅助脚本时，脚本会通过 `SKYRIM_CHS_PLUGIN_ROOT` 或工作区 marker 读取插件源的适配器元数据和 Skills。工作区只保存 QA 状态、运行输出和本机工具配置。

## 入口

以下是插件源仓库中的短命令；初始化后的工作区请按 [agent_adapters.md](./agent_adapters.md) 从 `.skyrim-chs-workspace.json` 读取插件源路径，或设置 `SKYRIM_CHS_PLUGIN_ROOT` 后调用插件源脚本。

```powershell
python scripts\validate_agent_capabilities.py --example
python scripts\list_agent_skills.py --agent opencode
python scripts\list_agent_skills.py --agent claude-code
python scripts\validate_claude_plugin_marketplace.py
```

导出 agent context 或 agent handoff 时必须明确指定工作区，避免把 `qa/agent_context_prompts/` 写到插件源码仓库：

```powershell
$env:SKYRIM_CHS_PLUGIN_ROOT = "D:\bupuy\Documents\SkyrimModTranslation"
$env:SKYRIM_CHS_WORKSPACE_ROOT = "D:\SkyrimCHS\YourWorkspace"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\write_agent_handoff.py"
python "$env:SKYRIM_CHS_PLUGIN_ROOT\scripts\export_agent_context.py" --agent claude-code --output qa/agent_context_prompts/latest.claude-code.context.md
```

opencode 和 Claude Code 是顶层非 GUI 入口，不直接领取 `qa/workflow_tasks.json` 子任务；子任务领取只属于主控分派的子 agent。
