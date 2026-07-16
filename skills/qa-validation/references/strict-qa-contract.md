# Strict QA Contract

Read this reference for strict completion, final delivery, binary writeback evidence, release review, or manual game-test validation.

## Contents

- Required evidence areas
- Translation and model review
- Plugin and PEX evidence
- Archive and final Mod evidence
- Serialized project report chain
- Manual game testing
- Release decision

## Required Evidence Areas

Strict QA must cover:

1. Translation structure and placeholder preservation
2. Interface translation key/encoding validity
3. Protected ID, path, filename, FormID, EditorID, and script-key integrity
4. Agent model review freshness and coverage
5. ESP/ESM/ESL controlled writeback evidence
6. PEX protected-row filtering and delivery evidence
7. BSA/BA2 audit and loose-override coverage
8. final_mod structure and direct-replacement behavior
9. Per-file provenance and hashes
10. `_CHS.zip` consistency
11. Project completion and translation-goal compliance
12. Player-run manual testing evidence when supplied

## Translation And Model Review

- Validate JSON/JSONL/XML/CSV syntax, row count, stable IDs/keys, placeholders, escaped newlines, and non-empty targets.
- Reject source-equals-target entries unless explicitly protected or intentionally unchanged.
- Run terminology and quality checks for English residue, empty translations, modern web slang, and inconsistent proper nouns.
- Before translation, generate `qa/<ModName>.translation_context_packet.md` from current candidates and have the agent model complete `qa/<ModName>.translation_context.json`. The context must match the workspace Game Profile, Mod name, and current source-items hash.
- Use the context summary for translation and final text/binary review. Give targeted semantic review to short UI labels, related help text, action/object/control relationships, and source strings with conflicting targets.
- Generate/update the model review packet after the latest translation input changes.
- Strict QA must reject a missing, incomplete, cross-game, or stale translation context and must bind the context content hash in the model-review contract.
- `qa/<ModName>.model_review.md` must identify `Reviewer: Agent model`, name the current final review packets, and cover every changed final_mod text/binary item.
- Mechanical scripts may extract, normalize, compare, and report; they cannot replace agent translation or semantic review.

## Plugin And PEX Evidence

- ESP/ESM/ESL outputs must be produced by a controlled CLI/adapter or Codex-only GUI fallback into workspace `tool_outputs`.
- Validate plugin masters, FormID, EditorID, record identity, and expected translated fields before assembly.
- PEX writeback candidates must exclude protected rows, empty targets, source-equals-target rows, logic keys, and every `CMP_*` comparison string.
- Run pre-build and post-build PEX delivery audits. The final PEX must be readable and contain complete expected targets, not only matching fragments.
- If required tool output is missing, strict QA must block instead of accepting the original untranslated binary.

## Archive And Final Mod Evidence

- BSA/BA2 inputs require current archive audit manifests when policy requires them.
- BSA-translated resources default to same-path loose overrides; do not treat an audit manifest as translated output.
- final_mod must preserve the current Game Profile's Data-root layout and contain original assets plus verified direct replacements.
- Language sidecar overlays must be zero unless the game is proven to load that exact file as the authoritative replacement.
- `meta/provenance.jsonl` must cover every final_mod file and record source path, source SHA256, final SHA256, transform, tool/generator, status, and QA evidence.
- Untouched binaries copied from `mod/` must remain byte-identical.
- Controlled tool outputs must have current adapter/tool logs and source/output hashes.

Run the consolidated gate:

```powershell
python scripts/run_non_gui_qa_gates.py --mod-name <ModName> --strict-complete
```

Relevant consolidated/final validators include:

```powershell
python scripts/audit_non_gui_coverage.py --mod-name <ModName>
python scripts/audit_archive_coverage.py --mod-name <ModName>
python scripts/audit_pex_delivery.py --mod-name <ModName>
python scripts/audit_final_review_quality.py --mod-name <ModName>
python scripts/validate_final_mod.py --final-mod-dir out/<ModName>/汉化产出/final_mod
python scripts/validate_chs_package.py --mod-name <ModName>
```

Use exact CLI options emitted by workflow state when they differ; workflow policy remains authoritative.

## Serialized Project Report Chain

Do not run dependent report writers in parallel. When manually refreshing the complete chain, use this order:

```powershell
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/test_workflow_health.py --run-strict-gate
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
python scripts/audit_project_completion.py
python scripts/new_manual_game_test_plan.py
python scripts/new_manual_game_test_results_template.py
python scripts/audit_translation_goal_compliance.py
```

Only for an explicit cross-adapter handoff, run `write_agent_handoff.py --agent <opencode|claude-code>` after `write_codex_handoff.py` and before exporting adapter context.

When `run_non_gui_translation_workflow.py` fails, its internal failure exit uses the shorter recovery chain: write the stage failure report, refresh readiness, workflow state, tasks, and Codex handoff, then generate and print the current `[SMT 阻断]` card before returning a nonzero exit code. It continues this refresh after secondary report-writer failures so the first root cause is preserved.

The refreshed chain must keep these authoritative/derived views consistent:

- `qa/translation_readiness.json`
- `qa/workflow_state.json`
- `.workflow/workflow_state.json`
- `.workflow/progress_card.md` and `.json`
- `.workflow/progress_events.jsonl`
- `qa/workflow_timeline.md`
- `qa/blockers.md`
- `qa/workflow_tasks.json`
- `qa/codex_handoff.json`
- `qa/workflow_health.json`
- `qa/project_completion_audit.json`
- `qa/manual_game_test_plan.json`
- `qa/manual_game_test_results.template.json`
- `qa/translation_goal_compliance.json`

Old state, progress cards, tasks, handoff, plan/template, package validation, or model review must not release current outputs.

## Manual Game Testing

Project automation only prepares and validates the evidence contract. It must not operate the real game, MO2, or Vortex or claim player testing was completed.

Generate the plan and result template after current package/readiness evidence exists:

```powershell
python scripts/new_manual_game_test_plan.py
python scripts/new_manual_game_test_results_template.py
```

The player fills `qa/manual_game_test_results.json` and stores evidence under `qa/manual_game_test_artifacts/<ModName>/`. Validate it with:

```powershell
python scripts/validate_manual_game_test_results.py
```

Reject plan-scope mismatches, duplicate Mods, stale package/manifest hashes, missing load-order details, vague evidence such as only `ok`/`passed`, missing timestamps, missing artifacts, or artifact hash changes after validation.

## Release Decision

Project-local QA passes only when all policy-required reports are current, strict gate blockers are zero, final_mod and package hashes agree, provenance covers every delivered file, and model review covers current outputs. The resulting state may be `ready_for_manual_test`; external runtime testing remains a separate player responsibility.
