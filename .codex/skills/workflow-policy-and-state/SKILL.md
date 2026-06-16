---
name: workflow-policy-and-state
description: Use before Skyrim translation orchestration, routing, GUI fallback, QA reruns, final_mod rebuilds, or handoff/status questions to read workflow_policy.json and workflow_state.json, decide the current allowed action, reject stage-inappropriate work, and give the next project-local Python command.
---

# Workflow Policy And State

## Goal

Read the project state machine before choosing work. This Skill does not translate, route individual files, operate GUI tools, or assemble `final_mod`; it decides whether those actions are currently allowed.

## Control Model

- Codex owns accurate and flexible orchestration; do not encode brittle full automation into the state machine.
- The state machine owns boundaries and evidence; it records what is allowed, what is blocked, and which reports justify the decision.
- Scripts own reproducible actions; policy should point to project-local Python commands rather than manual shell sequences.
- QA owns advancement decisions; policy cannot mark a stage complete without the required QA evidence.

## Required Inputs

Read these first:

1. `config/workflow_policy.json`
2. `qa/workflow_state.json`
3. `qa/translation_readiness.json`
4. `qa/workflow_health.json` when present
5. `qa/workflow_agent_runs.jsonl` when continuing an agent recovery attempt

If `qa/workflow_state.json` is missing or stale, run:

```console
python scripts/write_workflow_state.py
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

- Use `workflow_state.json` as the machine-readable source of truth for current stage, last successful stage, blockers, and next command.
- Use `workflow_policy.json` to decide whether a requested script/action is allowed. `always_allowed_scripts`, `allowed_entrypoint_scripts`, stage `allowed_scripts`, and `allowed_leaf_scripts` together form the allowed action surface; stage `next_command` still controls the preferred path.
- Read `recommended_actions`, `repair_candidates`, `stop_conditions`, `retry_count`, and `last_attempt` before choosing a recovery action.
- If the request would skip required evidence, refuse the skip and give the next allowed command.
- If `final_mod/meta/provenance.jsonl` is missing, the Mod cannot be `ready_for_manual_test`; treat it as `qa_failed`/blocked evidence even if older readiness reports still say ready.
- If state is `qa_failed`, do not translate more text or rebuild blindly. First inspect the strict gate report, final_mod validation, final review quality, archive coverage when present, and model review. Prefer `workflow-agent-orchestration` for recovery planning.
- Low-risk derived-output repairs are allowed only when represented in `repair_candidates` and the command is compatible with `allowed_scripts`; log attempts to `qa/workflow_agent_runs.jsonl`.
- Semantic quality, residual English that may be a proper noun, plugin/PEX writeback, GUI save, BSA repack, BA2 extraction, and manual game test blockers require model review, user input, or a blocked handoff rather than blind retry.
- If state is `ready_for_manual_test`, do not rerun translation or GUI tools unless the user explicitly asks for rework; direct the user to manual game testing artifacts.
- If state is `manual_tested`, only update package/readiness if inputs changed or the user requests a rebuild.

## Tool Priority

Apply this priority everywhere:

```text
CLI/library adapter
> auditable export/import
> GUI fallback
> manual handoff
```

GUI fallback is allowed only when policy marks it allowed and the state blocker says a decoder/CLI path is unavailable, unsupported, or failed. GUI success requires project-local output, logs, and QA evidence; launching or inspecting a window is not completion.

## Output Contract

When this Skill is used, report:

- Current `state`
- `last_success_stage`
- Blocking checks, if any
- Whether the requested action is allowed
- Recommended actions / repair candidates when the state is blocked or `qa_failed`
- Stop conditions that prevent automatic retry
- The next command from `workflow_state.json`

Do not invent missing evidence. If state files are stale or contradictory, refresh state with `python scripts/write_workflow_state.py`, then reassess.
