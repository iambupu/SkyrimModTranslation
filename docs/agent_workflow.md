# Agent Workflow

Agent workflow core 由 Python 状态机、policy、锁、QA 报告和 handoff 文件组成。Codex Plugin、opencode 和 Claude Code 都必须通过这些核心文件推进工作，不能绕过它们。

Claude Code 还可以通过 `.claude-plugin/marketplace.json` 作为 Claude Code marketplace 安装，但该 marketplace 只暴露非 GUI Skills；GUI 操作仍然只由 Codex adapter 执行。

## Read Order

通用 agent 接手时按顺序读取：

1. `qa/agent_handoff.json`
2. `qa/codex_handoff.json`（兼容 fallback）
3. `qa/workflow_state.json`
4. `qa/workflow_tasks.json`
5. `qa/translation_readiness.json`
6. 插件源 `config/workflow_policy.json`
7. 插件源 `config/agent_capabilities.example.json`

Codex 仍可继续优先读取 `qa/codex_handoff.json`。

## Resume Checkpoint

`qa/agent_handoff.json` 包含 `resume_checkpoint`，用于低上下文断点恢复。它记录：

- 最小 `next_read_set`
- 下一步低风险动作摘要
- 关键 QA/证据报告引用
- `mod/`、`translated/`、`work/`、`out/` 和核心 QA JSON 的 stale 检查快照

`resume_checkpoint` 只是恢复索引，不是状态机。恢复时先读它来减少探索范围；如果 watched path 的 `latest_mtime_utc` 晚于 checkpoint 生成时间，必须先刷新 readiness、workflow state、workflow tasks 和 handoff，再继续执行。

## Subagent Task Flow

```text
controller reads qa/workflow_tasks.json lanes
-> spawn or assign a subagent for one Mod/resource lane
-> subagent calls claim_workflow_task.py --parallel-only
-> subagent executes only the claimed task.command
-> subagent calls claim_workflow_task.py --complete
-> controller aggregates results
-> controller refreshes readiness/state/tasks/codex handoff/progress
-> controller writes agent-neutral handoff only when preparing cross-adapter takeover
-> append qa/workflow_agent_runs.jsonl only for recovery attempts or explicit trace records
```

## Non-GUI Complete Support

opencode 和 Claude Code 的完整支持范围是非 GUI workflow：

- 只读审计
- QA/report 写入
- 子 agent 并发任务编排
- 非 GUI controller workflow
- workflow policy 授权内的受控 Python 入口

它们不支持 GUI fallback。GUI-only workflow step 的正确处理是 blocked + handoff target。

## Codex Performance Rule

新增 agent 支持不能挂到 Codex 默认热路径。以下命令只在显式调用或 CI 中运行：

- `scripts/validate_agent_capabilities.py`
- `scripts/list_agent_skills.py`
- `scripts/export_agent_context.py`
- `scripts/write_agent_handoff.py`
- `scripts/validate_claude_plugin_marketplace.py`

其中 agent capabilities、adapter manifest、Skill registry 和 Claude marketplace 都是插件源仓库元数据，不会由初始化脚本复制到工作区。
