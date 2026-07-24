# Agent Compatibility

本页只定义 Codex、opencode 和 Claude Code 的能力差异，不描述安装、接手顺序或具体运行命令。

## 支持矩阵

| 能力 | Codex Plugin | opencode | Claude Code |
|---|---:|---:|---:|
| 顶层主控 | 是 | 是 | 是 |
| 非 GUI Python workflow | 是 | 是 | 是 |
| 主控派生并分配子 agent | 是 | 是 | 是 |
| portable `skills/` | 是 | 是 | 是 |
| 状态、QA、进度证据 | 是 | 是 | 是 |
| Codex 插件调用 | 是 | 否 | 否 |
| opencode 本地插件 | 否 | 是 | 否 |
| Claude Code marketplace | 否 | 否 | 是 |
| LexTranslator/xTranslator GUI | 是 | 否 | 否 |
| Computer Use、pywinauto、UI Automation | 是 | 否 | 否 |

Gemini CLI 没有支持计划。

## GUI 边界

Codex 是唯一 GUI 入口。以下能力不会因为安装 opencode 本地插件或 Claude marketplace 而扩展到其他入口：

- `resource_locks` 中的 `gui:desktop`；
- LexTranslator/xTranslator GUI 后备；
- Computer Use；
- pywinauto/UI Automation；
- 桌面坐标和窗口操作。

opencode 或 Claude Code 遇到这些任务时，标准结果是 `blocked`，并设置 `handoff_target=codex`。

## 主控与子 Agent

Codex、opencode 和 Claude Code 都可以作为顶层主控，通过公开 `smt.py`
JSON 结果选择下一步；底层状态读取和任务选择由公开控制器及运行期 Skill
完成。它们可以把已由内部调度器确认的可并行 lane 分配给自己派生的子
agent。

顶层主控不领取子任务。只有被主控分派的子 agent 才能通过项目领取协议处理 `qa/workflow_tasks.json` 中的任务；具体协议只在 `agent_workflow.md` 维护。

## Codex 性能边界

其他入口的支持不能进入 Codex 默认翻译热路径：

- 不默认运行 adapter capability 探测；
- 不默认导出 agent context；
- 不默认生成 `qa/agent_handoff.json`；
- 不改变 `write_codex_handoff.py` 和进度卡的默认合同；
- Skill registry、agent handoff 和 marketplace 校验只在显式命令或 CI 中运行。
