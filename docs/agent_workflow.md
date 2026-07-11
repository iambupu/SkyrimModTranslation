# Agent Workflow

Agent 推进汉化任务时，必须围绕 Python 状态机、policy、锁、QA 报告和接手文件工作。Codex、opencode 和 Claude Code 都读这些文件，不能绕过它们猜下一步。

Claude Code 可以通过 `.claude-plugin/marketplace.json` 安装本仓库的 Skill，但只包含非 GUI Skill。GUI 操作仍然只由 Codex 执行。

opencode 使用自己的本地插件机制。`init_opencode.py` 会在工作区生成 `.opencode/plugins/skyrim-chs.js` 和 `.opencode/skills/` 发现指针；前者注入环境变量和恢复提示，后者指向插件源 Skill 正文，两者都不提供 GUI 或 Codex 插件能力。

## 接手顺序

接手工作区时按顺序读取：

1. `qa/agent_handoff.json`
2. `qa/codex_handoff.json`（兼容 fallback）
3. `qa/workflow_state.json`
4. `qa/workflow_tasks.json`
5. `qa/translation_readiness.json`
6. 插件源 `config/workflow_policy.json`
7. 插件源 `config/agent_capabilities.example.json`

Codex 仍可继续优先读取 `qa/codex_handoff.json`。

## 断点恢复

`qa/agent_handoff.json` 包含 `resume_checkpoint`，用于中断后少读文件。它记录：

- 最小 `next_read_set`
- 下一步低风险动作摘要
- 关键 QA/证据报告引用
- `mod/`、`translated/`、`work/`、`out/` 和核心 QA JSON 的 stale 检查快照

`resume_checkpoint` 只是恢复索引，不是状态机。恢复时先运行 `write_agent_handoff.py --check-freshness`；脚本会比较纳秒级快照、路径存在状态和扫描完整性。返回码 `2` 时，先刷新 readiness、workflow state、workflow tasks、handoff 和 context，再继续。

## 子任务流程

```text
controller reads qa/workflow_tasks.json lanes
-> controller follows workflow-subagent-orchestration
-> assigns a subagent to one Mod/resource lane
-> subagent calls claim_workflow_task.py --parallel-only
-> subagent executes only the claimed task.command
-> subagent calls claim_workflow_task.py --complete
-> controller aggregates results
-> controller refreshes readiness/state/tasks/codex handoff/progress
-> controller writes agent handoff only when preparing opencode/Claude Code takeover
-> append qa/workflow_agent_runs.jsonl only for recovery attempts or explicit trace records
```

## opencode / Claude Code 能做什么

opencode 和 Claude Code 支持这些非 GUI 工作：

- 只读审计
- QA/report 写入
- 子 agent 并发任务编排
- 非 GUI 主控流程
- workflow policy 授权内的受控 Python 入口

它们不支持 GUI 后备。遇到 GUI 步骤时，结果应是 `blocked` 加 `handoff_target=codex`。

## Codex 性能规则

新增 agent 支持不能挂到 Codex 默认流程。以下命令只在手工调用或 CI 中运行：

- `scripts/validate_agent_capabilities.py`
- `scripts/list_agent_skills.py`
- `scripts/export_agent_context.py`
- `scripts/write_agent_handoff.py`
- `scripts/validate_claude_plugin_marketplace.py`

其中 agent capabilities、adapter manifest、Skill registry 和 Claude marketplace 都是插件源仓库元数据。opencode 的 `.opencode/plugins/skyrim-chs.js` 和轻量 Skill 指针是工作区本地配置；插件源脚本和 runtime Skill 正文不会复制到工作区。
