---
name: workflow-policy-and-state
description: "用于对外入口已识别状态查询或运行期动作后，读取工作流状态、progress card、Trace 摘要并判断允许动作。中文触发：入口已识别状态查询、读取 progress_card、读取 workflow_state、判断允许动作、workflow_policy、allowed action、状态机约束、接手状态。Use after entry classification and before orchestration, routing, GUI fallback, QA reruns, final_mod rebuilds, or handoff handling to read progress_card, trace_summary, workflow_policy.json/workflow_state.json and reject stage-inappropriate work."
---

# Workflow Policy And State

## Public Controller Boundary

这是 `python scripts\smt.py --format json run ...` 和公开 `resume/status/doctor/output` 背后的状态判定内部实现。顶层 Agent 不得自行组合本 Skill 记录的 canonical refresh 或任务脚本；公开结果只投影权威 state/tasks。workflow policy、`next_actions` 和 workflow task 不得指向外层 `smt.py` controller，`smt.py` 也不得加入 allowed script 集合。

## Goal

Windows 运行环境；所有可复用动作使用插件源 Python 入口。不得引入 Bash、WSL、Linux 命令或 shell 包装层。

Read the project state machine before choosing work. This Skill does not translate, route individual files, operate GUI tools, or assemble `final_mod`; it decides whether those actions are currently allowed.

## Control Model

- The active controller agent owns accurate and flexible orchestration; do not encode brittle full automation into the state machine.
- The state machine owns boundaries and evidence; it records what is allowed, what is blocked, and which reports justify the decision.
- Scripts own reproducible actions; policy should point to plugin-provided Python commands executed in the current workspace context rather than manual shell sequences.
- QA owns advancement decisions; policy cannot mark a stage complete without the required QA evidence.

## Required Inputs

Read `.workflow/progress_card.md` first when the user only asks for current progress. Otherwise select the handoff by active controller:

- Codex: read `qa/codex_handoff.json` first; read `qa/agent_handoff.json` only for an explicit cross-adapter takeover.
- opencode or Claude Code: read `qa/agent_handoff.json` first, falling back to `qa/codex_handoff.json`.

Then read, in order:

1. `qa/workflow_state.json`
2. `qa/workflow_tasks.json` when choosing schedulable work
3. `config/workflow_policy.json`
4. `qa/translation_readiness.json`
5. `qa/workflow_health.json` when present
6. `qa/workflow_agent_runs.jsonl` when continuing an agent recovery attempt

If `qa/workflow_state.json` is missing or stale, run the canonical state refresh chain against the current workspace:

```powershell
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
```

Health, strict QA, completion, and release evidence use the serialized project report chain owned by `qa-validation`; do not duplicate that longer chain in this state-selection Skill.
The strict gate runs only when workflow state or the user explicitly requests strict QA; ordinary state refresh never implies it.

If you are preparing an agent-neutral cross-adapter handoff for opencode or Claude Code, run `python scripts/write_agent_handoff.py --agent <opencode|claude-code>` explicitly after `write_codex_handoff.py`. Codex-only hot paths may keep using `write_codex_handoff.py` without adding the agent-neutral handoff export unless cross-adapter takeover is explicitly needed.

Before opencode or Claude Code trusts an existing `resume_checkpoint`, run `python scripts/write_agent_handoff.py --agent <opencode|claude-code> --check-freshness` with the current adapter name. Exit code `0` means the saved checkpoint matches current watched paths and target agent. Exit code `2` means the checkpoint is unusable: inspect the returned JSON `reasons[]` before acting. Refresh readiness, workflow state, workflow tasks, handoffs, and exported adapter context only for `snapshot_changed` or equivalent artifact/state changes. For `checkpoint_snapshot_incomplete`, evidence/read-budget limits, reparse points, or other unsafe snapshot reasons, fail closed and report a blocker instead of repeatedly refreshing. This explicit check must not be added to the default Codex hot path.

`python scripts/write_workflow_state.py` also writes the user-facing progress files:

```text
.workflow/progress_card.md
.workflow/progress_card.json
.workflow/progress_events.jsonl
.workflow/workflow_state.json
qa/workflow_timeline.md
qa/blockers.md
```

## State Machine

Canonical progress states:

```text
discovered
-> extracted
-> routed
-> candidates_extracted
-> translated
-> tool_outputs_generated
-> final_mod_built
-> packaged
-> qa_pending_strict
-> qa_passed
-> ready_for_manual_test
-> manual_tested
```

Failure states are explicit and must not be treated as progress:

```text
blocked
qa_failed
needs_input
```

## Decision Rules

- User-facing progress must come from `.workflow/progress_card.md` or `.workflow/progress_card.json`. Do not infer progress from stdout, trace records, or natural-language status updates.
- Use `workflow_state.json` as the machine-readable source of truth for current stage, last successful stage, blockers, and structured `next_actions`.
- Treat `required_agent_capability` as a declarative action requirement. State refresh does not probe the desktop; the selected handoff decides whether the active top-level adapter satisfies it. Never execute an action marked `agent_capability_satisfied=false`.
- Select the handoff by active controller: Codex uses `qa/codex_handoff.json`; opencode and Claude Code use `qa/agent_handoff.json` with Codex handoff as fallback. If a handoff conflicts with `workflow_state.json`, refresh the relevant handoff and trust `workflow_state.json`.
- Use `qa/workflow_tasks.json` only as a schedulable view derived from `workflow_state.json`; it does not decide QA pass/fail. `counts.pending` is kept only as a compatibility alias for `pending_executable`; display `pending_executable`, `pending_manual`, and `pending_total` together.
- For multi-agent orchestration, read `mod_lanes` for independent Mod-level work and `resource_lanes` for large single-Mod file/resource shards. A subagent may claim a Mod lane with `claim_workflow_task.py --mod-name <ModName> --owner <AgentId> --parallel-only`, or a resource lane with `--mod-name <ModName> --resource-lock <ResourceLock> --owner <AgentId> --parallel-only`.
- Use `workflow-subagent-orchestration` for normal lane fan-out and completion aggregation. Use `workflow-agent-orchestration` only after a lane or workflow enters `blocked`/`qa_failed` recovery.
- Treat `resource_locks` as the concurrency boundary. Different `file:<ModName>:...` or `resource:<ModName>:...` lanes can run together only when `can_run_parallel=true` and dependencies are done. `mod:<ModName>` conflicts with all file/resource lanes for that Mod. `global:workflow-state`, `gui:desktop`, strict QA, final_mod assembly, shared glossary/RAG rebuilds, and GUI automation must stay serial.
- Use `workflow_policy.json` to decide whether a requested script/action is allowed. `always_allowed_scripts`, `allowed_entrypoint_scripts`, stage `allowed_scripts`, and `allowed_leaf_scripts` together form the allowed action surface; stage `recommended_command` seeds the preferred structured action.
- Game-scoped dictionary indexing is a derived workflow aid. `scripts/build_lextranslator_dictionary_rag_index.py` may run before translation stages; reuse is allowed only when index version, `game_id`, Game Profile RAG source list, and source mtimes are current. SST/EET decoding is read-only and a malformed source must fail closed.
- `scripts/build_external_glossary_matches.py --mod-name <ModName>` may run after candidate/plugin text exists to generate a workspace-local terminology hint packet; it does not change workflow progress state by itself.
- Read `recommended_actions`, `repair_candidates`, `stop_conditions`, `retry_count`, and `last_attempt` before choosing a recovery action.
- If the request would skip required evidence, refuse the skip and give the next allowed command.
- If `final_mod/meta/provenance.jsonl` is missing, the Mod cannot be `ready_for_manual_test`; treat it as `qa_failed`/blocked evidence even if older readiness reports still say ready.
- If state is `qa_failed`, do not translate more text or rebuild blindly. First inspect the strict gate report, final_mod validation, final review quality, archive coverage when present, and model review. Prefer `workflow-agent-orchestration` for recovery planning.
- Low-risk derived-output repairs are allowed only when represented in `repair_candidates` and the command is compatible with `allowed_scripts`; log attempts to `qa/workflow_agent_runs.jsonl`.
- Progress and trace artifact paths must stay workspace-local and relative; external absolute paths or `..` escapes are not valid evidence and should be dropped by scripts or treated as stale reports.
- Semantic quality, residual English that may be a proper noun, plugin/PEX writeback, GUI save, BSA repack, BA2 extraction, and manual game test blockers require model review, user input, or a blocked handoff rather than blind retry.
- If state is `ready_for_manual_test`, do not rerun translation or GUI tools unless the user explicitly asks for rework; direct the user to manual game testing artifacts.
- `ready_for_manual_test` means project-local static QA and package evidence are clean; it does not mean real game/MO2/Vortex validation has been performed. Next action should tell the player to inspect `final_mod` / `_CHS.zip` and use `qa/manual_game_test_plan.md`.
- If state is `manual_tested`, only update package/readiness if inputs changed or the user requests a rebuild.
- `traces/latest.jsonl` and `traces/trace_summary.md` are developer diagnostics. Summarize them only when the user asks to debug a failure; they do not replace QA reports or progress cards.

## Tool Priority

Apply this priority everywhere:

```text
CLI/library adapter
> auditable export/import
> GUI fallback
> manual handoff
```

GUI fallback is Codex-only and is allowed only when policy marks it allowed and the state blocker says a decoder/CLI path is unavailable, unsupported, or failed. GUI success requires workspace-local output, logs, and QA evidence; launching or inspecting a window is not completion. opencode/Claude Code must mark GUI-only work blocked with `handoff_target=codex`.

## Output Contract

When this Skill is used, report:

- For progress-only questions, output the `.workflow/progress_card.md` summary with `[SMT 进度]`, `[SMT 阻断]`, or `[SMT 完成]`.
- For action-selection questions, report current `state`, `last_success_stage`, blocking checks, whether the requested action is allowed, recommended actions / repair candidates, stop conditions, and structured `next_actions` from `workflow_state.json`.
- After running workflow, queue, strict-gate, health, state-refresh, or recovery commands, read `.workflow/progress_card.md` again and paste the complete Markdown card directly into the chat body as user-visible progress, so it renders as a heading and table. Do not wrap it in triple backticks, a code block, a quote block, or any container that shows raw Markdown. Do not rely on the command stdout copy of the card, because Codex desktop may collapse command output.
- A run that stops after command output or a hand-written summary without the read-progress-card-and-paste step violates this Skill's output contract.

Do not invent missing evidence. If state files are stale or contradictory, run the canonical state refresh chain above before reassessing. Use the `qa-validation` serialized report chain only when workflow state or the user requires health, strict QA, completion, or release evidence. Run `python scripts/write_agent_handoff.py --agent <opencode|claude-code>` after `write_codex_handoff.py` only when an agent-neutral cross-adapter handoff for opencode or Claude Code is explicitly needed. In an initialized workspace, resolve those `scripts/` paths to the plugin source rather than creating a workspace-local copy.
