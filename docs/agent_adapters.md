# Agent 入口索引

本页只说明如何选择入口。能力差异见 [Agent Compatibility](./agent_compatibility.md)，非 GUI 接手协议见 [Non-GUI Agent Workflow](./agent_workflow.md)。

| 入口 | 适用场景 | 详细文档 |
|---|---|---|
| Codex | 默认完整入口；支持非 GUI 流程和受控 GUI 后备 | [Codex 接手指南](./codex_workflow.md) |
| opencode | 工作区本地插件；只处理非 GUI 流程 | [opencode Adapter](./opencode_adapter.md) |
| Claude Code | Claude marketplace；只处理非 GUI 流程 | [Claude Code Adapter](./claude_code_adapter.md)、[Claude Code Marketplace](./claude_code_marketplace.md) |

## 选择入口

需要 LexTranslator、xTranslator、Computer Use 或窗口操作时，选择 Codex。

只需要非 GUI 命令行流程时，可以选择任一入口。能力差异、主控/子 agent 边界和 GUI handoff 只在 [Agent Compatibility](./agent_compatibility.md) 维护；接手文件顺序只在 [Non-GUI Agent Workflow](./agent_workflow.md) 维护。本页不复制安装命令、接手协议或恢复步骤。
