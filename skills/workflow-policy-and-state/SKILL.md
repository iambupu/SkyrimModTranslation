---
name: workflow-policy-and-state
description: "用于读取工作流状态、进度卡、Trace 摘要和判断下一步允许动作。中文触发：现在状态、现在进度到哪了、进度卡、progress_card、Trace、trace_summary、下一步、怎么继续、能不能继续、工作流状态、workflow_state、workflow_policy、allowed action、推荐命令、状态机、接手状态。Use before orchestration, routing, GUI fallback, QA reruns, final_mod rebuilds, or handoff/status/progress/trace questions to read progress_card, trace_summary, workflow_policy.json/workflow_state.json, and reject stage-inappropriate work."
---

# Workflow Policy And State

## Goal

Read the project state machine before choosing work. This Skill does not translate, route individual files, operate GUI tools, or assemble `final_mod`; it decides whether those actions are currently allowed.

## Control Model

- Codex owns accurate and flexible orchestration; do not encode brittle full automation into the state machine.
- The state machine owns boundaries and evidence; it records what is allowed, what is blocked, and which reports justify the decision.
- Scripts own reproducible actions; policy should point to plugin-provided Python commands executed in the current workspace context rather than manual shell sequences.
- QA owns advancement decisions; policy cannot mark a stage complete without the required QA evidence.

## Required Inputs

Read these first:

1. `.workflow/progress_card.md` when the user only asks for current progress
2. `qa/codex_handoff.json` when present
3. `qa/workflow_state.json`
4. `qa/workflow_tasks.json` when choosing schedulable work
5. `config/workflow_policy.json`
6. `qa/translation_readiness.json`
7. `qa/workflow_health.json` when present
8. `qa/workflow_agent_runs.jsonl` when continuing an agent recovery attempt

If `qa/workflow_state.json` is missing or stale, run the plugin-provided scripts against the current workspace:

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/test_workflow_health.py --run-strict-gate
```

Then refresh the compact handoff:

```console
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
python scripts/audit_project_completion.py
python scripts/new_manual_game_test_plan.py
python scripts/new_manual_game_test_results_template.py
python scripts/audit_translation_goal_compliance.py
```

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
- Use `workflow_state.json` as the machine-readable source of truth for current stage, last successful stage, blockers, `next_actions`, and legacy `next_command`.
- Use `qa/codex_handoff.json` as the short first-read handoff only; if it conflicts with `workflow_state.json`, refresh it and trust `workflow_state.json`.
- Use `qa/workflow_tasks.json` only as a schedulable view derived from `workflow_state.json`; it does not decide QA pass/fail. `counts.pending` is kept only as a compatibility alias for `pending_executable`; display `pending_executable`, `pending_manual`, and `pending_total` together.
- Use `workflow_policy.json` to decide whether a requested script/action is allowed. `always_allowed_scripts`, `allowed_entrypoint_scripts`, stage `allowed_scripts`, and `allowed_leaf_scripts` together form the allowed action surface; stage `next_command` still controls the preferred path.
- Dynamic LexTranslator-style dictionary indexing is a derived workflow aid. `scripts/build_lextranslator_dictionary_rag_index.py` may run before translation stages; it should compare the workspace `glossary/lextranslator_dynamic_dictionaries/` tree against `work/glossary_rag/lextranslator_dynamic.sqlite` and skip rebuild when the index is current.
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

GUI fallback is allowed only when policy marks it allowed and the state blocker says a decoder/CLI path is unavailable, unsupported, or failed. GUI success requires workspace-local output, logs, and QA evidence; launching or inspecting a window is not completion.

## Output Contract

When this Skill is used, report:

- For progress-only questions, output the `.workflow/progress_card.md` summary with `[SMT 进度]`, `[SMT 阻断]`, or `[SMT 完成]`.
- For action-selection questions, report current `state`, `last_success_stage`, blocking checks, whether the requested action is allowed, recommended actions / repair candidates, stop conditions, and structured `next_actions` from `workflow_state.json`.
- After running workflow, queue, strict-gate, health, state-refresh, or recovery commands, read `.workflow/progress_card.md` again and paste the complete Markdown card directly into the chat body as user-visible progress, so it renders as a heading and table. Do not wrap it in triple backticks, a code block, a quote block, or any container that shows raw Markdown. Do not rely on the command stdout copy of the card, because Codex desktop may collapse command output.
- A run that stops after command output or a hand-written summary without the read-progress-card-and-paste step violates this Skill's output contract.

Do not invent missing evidence. If state files are stale or contradictory, refresh readiness first with `python scripts/audit_translation_readiness.py`, then run `python scripts/write_workflow_state.py`, `python scripts/test_workflow_health.py --run-strict-gate`, `python scripts/write_workflow_tasks.py`, `python scripts/write_codex_handoff.py`, `python scripts/audit_project_completion.py`, `python scripts/new_manual_game_test_plan.py`, `python scripts/new_manual_game_test_results_template.py`, and `python scripts/audit_translation_goal_compliance.py` before reassessing. In an initialized workspace, resolve those `scripts/` paths to the plugin source rather than creating a workspace-local copy.
