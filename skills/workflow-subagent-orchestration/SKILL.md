---
name: workflow-subagent-orchestration
description: "用于主控 agent 分配和协调可并发的子智能体任务。中文触发：子智能体、子 agent、并发任务、并行汉化、分配任务、领取任务、Mod lane、resource lane、文件分片、多个 agent 协作、workflow_tasks。Use when a controller agent needs to fan out independent workflow_tasks.json lanes to controller-spawned subagents and aggregate their results. Do not use for blocked/qa_failed recovery, top-level adapter task claiming, GUI work, strict QA, final_mod assembly, or direct binary editing."
---

# Workflow Subagent Orchestration

## Goal

Windows 运行环境；所有可复用动作使用插件源 Python 入口。不得引入 Bash、WSL、Linux 命令或 shell 包装层。

Coordinate normal, evidence-bound parallel work. The active Codex, opencode, or Claude Code session remains the top-level controller. Only subagents spawned by that controller may claim tasks; the top-level adapter must not claim a lane as if it were a worker.

## Read First

1. The controller-specific handoff: Codex reads `qa/codex_handoff.json`; opencode and Claude Code read `qa/agent_handoff.json`, falling back to Codex handoff.
2. `qa/workflow_state.json`
3. `qa/workflow_tasks.json`
4. `config/workflow_policy.json` from the plugin source

Use this Skill only when pending tasks have `executable=true`, `can_run_parallel=true`, satisfied `dependencies`, and non-overlapping `resource_locks`.

## Controller Protocol

The controller owns state refresh, lane selection, concurrency limits, subagent creation, result aggregation, progress-card replay, and the decision to continue or stop.

Before fan-out, run the canonical state refresh chain from `workflow-policy-and-state`, then inspect the resulting queue:

```powershell
python scripts/run_workflow_tasks.py --max-workers <N> --dry-run
```

Prefer different `mod_lanes` for unrelated Mods. For one large Mod, use independent `file:<ModName>:<RelativePathOrHash>` or `resource:<ModName>:<Name>` lanes. A `mod:<ModName>` lock conflicts with every file/resource lane for that Mod.

## Subagent Protocol

Give each subagent one bounded lane. It must claim through the script instead of editing `qa/workflow_tasks.json`:

```powershell
python scripts/claim_workflow_task.py --mod-name <ModName> --owner <SubagentId> --parallel-only
python scripts/claim_workflow_task.py --mod-name <ModName> --resource-lock <ResourceLock> --owner <SubagentId> --parallel-only
```

The subagent executes only the returned `command`, keeps all access inside the workspace, and does not refresh global state. It then records the result:

```powershell
python scripts/claim_workflow_task.py --task-id <TaskId> --owner <SubagentId> --complete --complete-status done --exit-code 0 --output-tail "<short result>"
python scripts/claim_workflow_task.py --task-id <TaskId> --owner <SubagentId> --complete --complete-status failed --exit-code 1 --output-tail "<short error>"
```

A Mod-lane subagent may claim the next task for the same Mod after completing its current task. A resource-lane subagent must remain bound to the same Mod and resource lock.

## Serial Boundary

Never fan out:

- GUI automation or `gui:desktop`
- strict QA or final_mod assembly
- global readiness/state/tasks/handoff refresh
- shared glossary/RAG rebuilds
- Mod-wide writeback
- old orchestration entrypoints
- tasks with `can_run_parallel=false` or overlapping locks

## Batch Completion

After all subagents return, the controller serially refreshes readiness, workflow state, workflow tasks, and Codex handoff. Generate `qa/agent_handoff.json` only for an explicit cross-adapter handoff. Then re-read `.workflow/progress_card.md` and present the complete rendered Markdown card.

If a task fails or becomes blocked, stop assigning that lane and switch to `workflow-agent-orchestration` for evidence-bound recovery.
