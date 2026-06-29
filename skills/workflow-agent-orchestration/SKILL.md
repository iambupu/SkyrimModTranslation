---
name: workflow-agent-orchestration
description: "用于 Codex 接手 blocked/qa_failed 后的安全恢复编排。中文触发：blocked、qa_failed、继续处理阻断、恢复流程、自动修复失败、重试、记录尝试、下一步怎么修、卡住了、恢复 QA、刷新进度卡。Reads workflow_state.json, chooses allowed repair actions, retries low-risk derived-output steps, logs qa/workflow_agent_runs.jsonl, refreshes progress card outputs, or stops safely. Do not translate directly, edit binaries, bypass QA, or replace workflow-policy-and-state."
---

# Workflow Agent Orchestration

## Goal

Give Codex a lightweight agent protocol for flexible recovery while keeping the workflow evidence-bound. This Skill does not replace scripts or the state machine; it tells Codex how to inspect state, choose an allowed next action, log the attempt, and stop when judgment or high-risk tooling is required.

## Control Model

- Codex owns accurate and flexible orchestration: interpret the current task, inspect evidence, choose the next allowed action, retry low-risk derived outputs, or stop.
- The state machine owns boundaries and evidence: current state, last successful stage, allowed scripts, recommended actions, repair candidates, stop conditions, and next command.
- Scripts own reproducible actions: plugin-provided Python entries perform extraction, rebuilds, QA gates, state refreshes, and logging against the current workspace.
- QA owns advancement decisions: a state moves forward only when the relevant gates and review evidence allow it.

## Inputs

Read these in order:

1. `qa/codex_handoff.json` when present
2. `qa/workflow_state.json`
3. `qa/workflow_tasks.json` when choosing schedulable work
4. `config/workflow_policy.json`
5. The reports named by `recommended_actions[].path`, `repair_candidates[].evidence`, or `codex_handoff.blocking_mods[].must_read_evidence`
6. `qa/translation_readiness.json`
7. `qa/workflow_health.json` when present
8. `qa/workflow_agent_runs.jsonl` when continuing a prior recovery attempt

## Parallel Subagent Orchestration

Use multiple subagents when `qa/workflow_tasks.json` contains multiple Mod lanes or file/resource lanes with independent pending tasks that have `executable=true`, `can_run_parallel=true`, satisfied `dependencies`, and non-overlapping `resource_locks`. The coordinator agent owns state refresh, lane fan-out, result aggregation, progress-card replay, and the decision to continue or stop. A subagent may own one Mod lane and process that Mod's claimed tasks serially, own one large-Mod file/resource lane, or own one bounded read-only audit scope.

For a large single Mod with many independent text files, prefer `resource_lanes` when tasks use locks such as `file:<ModName>:<RelativePathOrHash>` or `resource:<ModName>:<Name>`. Different file/resource lanes can run concurrently for parsing, candidate extraction, read-only audits, translation shard generation, and model review shards. A `mod:<ModName>` task conflicts with all file/resource lanes for the same Mod and is reserved for Mod-wide writeback, final_mod assembly, strict QA, and global refresh boundaries.

Efficiency gains apply only to the parallel segment. If there are `P` independent lanes and `--max-workers N`, the practical throughput ceiling is bounded by `min(P, N)` and reduced by model queueing, file IO, claim/complete writes, coordinator aggregation, and later serial QA/final_mod stages. Do not promise end-to-end linear speedup.

Coordinator flow:

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
python scripts/run_workflow_tasks.py --max-workers <N> --dry-run
```

Subagent claim/complete flow:

```console
python scripts/claim_workflow_task.py --mod-name <ModName> --owner <AgentId> --parallel-only
python scripts/claim_workflow_task.py --mod-name <ModName> --resource-lock <ResourceLock> --owner <AgentId> --parallel-only
python scripts/claim_workflow_task.py --task-id <TaskId> --owner <AgentId> --complete --complete-status done --exit-code 0 --output-tail "<short result>"
python scripts/claim_workflow_task.py --task-id <TaskId> --owner <AgentId> --complete --complete-status failed --exit-code 1 --output-tail "<short error>"
```

Subagents must execute only the claimed task's `command`, must keep all reads/writes inside the workspace boundary, and must not run global refresh commands independently. A Mod-lane subagent may repeatedly claim the next task for the same `--mod-name` after completing the previous one. A resource-lane subagent must keep using the same `--mod-name` and `--resource-lock` until that lane is empty or blocked. After a parallel batch, the coordinator refreshes readiness, workflow state, workflow tasks, codex handoff, progress card, timeline, and blockers once, then reads `.workflow/progress_card.md` and outputs the rendered Markdown card.

Do not parallelize GUI automation, strict QA, global state refresh, shared glossary/RAG rebuilds, old orchestration entrypoints, final_mod assembly, Mod-wide writeback, or any task with `can_run_parallel=false`, `global:workflow-state`, `gui:desktop`, overlapping `mod:<name>`, or overlapping file/resource locks. Different Mod lanes may be processed by different subagents concurrently; different file/resource lanes inside one large Mod may also run concurrently when no `mod:<ModName>` task is active.

## Agent Loop

1. Select the target Mod from `workflow_state.json`.
2. Read `state`, `last_success_stage`, `blocking_checks`, `recommended_actions`, `repair_candidates`, `stop_conditions`, `retry_count`, and `last_attempt`.
3. Inspect the named reports before running commands. For `qa_failed`, this is mandatory.
4. Pick one action that is allowed by the current state's `allowed_scripts`. This list is generated from policy-level always-allowed scripts, entrypoint scripts, the current stage scripts, and leaf scripts; prefer structured `next_actions`, `qa/workflow_tasks.json`, or a named `repair_candidate` over parsing legacy `next_command`.
5. Before and after the action, append a JSONL row with plugin script `scripts/log_workflow_agent_run.py`.
6. Refresh evidence after any change:

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
```

`scripts/write_workflow_state.py` also refreshes `.workflow/progress_card.md`, `.workflow/progress_card.json`, `.workflow/progress_events.jsonl`, `.workflow/workflow_state.json`, `qa/workflow_timeline.md`, and `qa/blockers.md`. Do not report a recovered stage or blocked state to the user until those derived progress files match the refreshed `qa/workflow_state.json`.

After any recovery attempt or health/state refresh, read `.workflow/progress_card.md` again and paste the complete Markdown card directly into the chat body so it renders as a heading and table. Do not wrap it in triple backticks, a code block, a quote block, or any container that shows raw Markdown. Do not rely on the command stdout copy of the card, a summary, or a hand-written status because Codex desktop may collapse command output. Stopping without this read-and-paste step violates the output contract.

7. Continue only if the new state or blockers changed. If the same blocker repeats after two attempts, stop and mark the next response as blocked with evidence.

## Automatic Repair Boundary

Codex may automatically repair low-risk derived artifacts:

- stale readiness, state, health, package, or status reports
- missing or stale `final_mod` metadata generated by `build_final_mod.py`
- CHS package mismatch caused by stale packaging
- missing BSA/BA2 audit manifest when the archive is workspace-local and a read-only audit is available
- missing loose override evidence when the source translated file already exists in the workspace

Codex must stop or request model/human judgment for:

- semantic quality warnings such as residual English that may be a Mod name, proper noun, or in-game term
- model review failures or stale model review after changed packets
- PEX/ESP writeback verification failures
- direct binary changes not produced by a controlled adapter
- GUI save failures or any GUI action without workspace-local output evidence
- BSA repacking, BA2 extraction/writeback, or loose override failures found only by player testing
- manual game test evidence gaps

## Logging

Use `qa/workflow_agent_runs.jsonl` as an append-only trace. Log at least:

```console
python scripts/log_workflow_agent_run.py --mod-name <ModName> --state <state> --event inspect --action read_blocker_reports --status started
python scripts/log_workflow_agent_run.py --mod-name <ModName> --state <state> --event command --action "python scripts/run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete" --status failed --evidence qa/<ModName>.non_gui_qa_gates.md
```

Each row must stay workspace-local and must not contain hidden reasoning, credentials, or real game/MO2/Vortex paths.

## Decision Rules

- If `state` is `qa_failed`, inspect the strict gate, final validation, final review quality, archive coverage, and model review reports before running any rebuild or translation command.
- If provenance is missing, run the low-risk final_mod rebuild path before any manual-test claim; a Mod with missing `final_mod/meta/provenance.jsonl` is not ready.
- If a `repair_candidate` is `risk=low` and `allowed=true`, Codex may execute it once, then refresh state.
- For a single safe automated recovery, prefer `python scripts/resume_workflow.py --mod-name <ModName> --mode safe` when it selects a low-risk task. It must still log attempts and refresh state.
- If a candidate is `risk=semantic` or `risk=high`, Codex must inspect evidence and either perform model review/term decision or stop with a clear blocker.
- If `state` is `ready_for_manual_test`, do not rerun translation or writeback unless the user explicitly requests rework.
- If the requested action is not in `allowed_scripts`, refuse that action and give the next allowed command.

## Completion

An agent recovery turn is complete only when:

- `qa/workflow_agent_runs.jsonl` records the inspected blockers and attempted action.
- `qa/translation_readiness.json`, `qa/workflow_state.json`, `qa/workflow_tasks.json`, `qa/codex_handoff.json`, `.workflow/progress_card.md`, `.workflow/progress_card.json`, `.workflow/progress_events.jsonl`, `.workflow/workflow_state.json`, `qa/workflow_timeline.md`, and `qa/blockers.md` have been refreshed.
- The Mod either moved forward, has a different blocker, or has a clear blocked reason with evidence.
