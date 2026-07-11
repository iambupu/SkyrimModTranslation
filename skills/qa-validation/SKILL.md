---
name: qa-validation
description: "用于汉化后的 QA 校验和放行判断。中文触发：QA、校验、检查漏译、严格门禁、strict、占位符、保护 ID、残留英文、结构检查、hash、provenance、ready、能不能测试、验证 final_mod、PEX 覆盖。Use after translation batches, GUI tool_outputs, PEX writeback, package rebuilds, readiness refreshes, or final_mod assembly. Do not use for translation, GUI control, workflow recovery, or task scheduling."
---

# QA Validation

## Goal

Validate translated text, controlled tool outputs, final_mod contents, and release evidence. QA decides whether the workflow may advance; it does not translate, schedule tasks, operate GUI tools, or refresh workflow state on its own.

## Read Strategy

Use this file for routine batch QA and gate selection.

Read [references/strict-qa-contract.md](references/strict-qa-contract.md) completely when any of these apply:

- running `--strict-complete`
- validating final_mod or `_CHS.zip`
- checking ESP/PEX writeback evidence
- preparing release or manual game testing
- diagnosing a failed strict gate

## Hard Boundaries

- Work only inside the initialized workspace.
- Read Mod input only from workspace `mod/`.
- Never access real Skyrim, MO2, or Vortex directories.
- Never directly edit `.esp`, `.esm`, `.esl`, `.bsa`, `.ba2`, `.pex`, `.dll`, or `.exe` files.
- Do not mark a stage complete when a required report is missing, stale, or failed.
- Keep player-run game testing separate from project-local automated QA.

## Routine Batch QA

1. Confirm the input and output paths are inside the workspace.
2. Validate JSON, JSONL, XML, CSV, or Interface translation structure.
3. Check row counts, stable IDs/keys, placeholders, protected tokens, line breaks, and non-empty targets.
4. Check for untranslated English, source-equals-target rows, modern web slang, and terminology drift.
5. For PEX exports, reject protected rows, logic keys, and `CMP_*` comparison strings from writeback candidates.
6. Refresh the model review whenever a translated input changes. New reports must say `Reviewer: Agent model`.
7. Write QA findings under `qa/`; do not modify source Mod files to make a check pass.

Useful focused entrypoints:

```console
python scripts/validate_translation.py
python scripts/scan_placeholders.py
python scripts/validate_interface_translation.py
python scripts/proofread_translation.py
python scripts/validate_final_text_structure.py
python scripts/verify_plugin_output.py
python scripts/verify_pex_output.py
```

Use the entrypoint appropriate to the routed file type; do not run every leaf script by default.

## Strict Gate

Use the consolidated strict gate after translation and controlled writeback evidence are current:

```console
python scripts/run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete
```

The strict gate must cover final text/binary review packets, model review freshness, PEX delivery where applicable, archive coverage, final_mod validation, provenance, package consistency, and translation goal compliance. Inspect the named failure report before rebuilding or retrying anything.

## Final Output Checks

Require all of the following before project-local completion:

- `out/<ModName>/汉化产出/final_mod/` has the correct Data-root layout.
- The final output contains direct replacements, not unsupported language sidecars.
- Every final_mod file has current provenance and matching source/final SHA256 evidence.
- Controlled ESP/PEX outputs come from workspace `tool_outputs`; untouched binaries are byte-identical copies from `mod/`.
- `<ModName>_CHS.zip` matches final_mod file-for-file.
- Final text and binary review packets are current and covered by agent model review.
- Strict QA reports zero blockers and zero unresolved warnings required by policy.

## State Boundary

QA scripts write validation evidence. The controller/orchestrator owns the serialized report refresh chain:

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/test_workflow_health.py --run-strict-gate
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
```

Run `write_agent_handoff.py` only when explicitly preparing an opencode or Claude Code handoff. After workflow/state/health commands, the controller must re-read `.workflow/progress_card.md` and present its complete rendered Markdown card.

## Completion Rule

Project-local QA may finish at `ready_for_manual_test`. That means the package and static evidence are ready for the player; it does not mean Skyrim/MO2/Vortex testing happened. Validate player-supplied results only through the manual-test contract in the strict QA reference.
