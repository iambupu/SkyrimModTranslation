---
name: skyrim-mod-chs-translation
description: "Use for Windows-only The Elder Scrolls V: Skyrim SE/AE Mod Simplified Chinese (CHS) localization projects when Codex needs to create or initialize a new workspace, recognize a workspace marker, operate this repository as a Codex plugin source, inspect or refresh workflow state, maintain workspace glossary dictionaries, prepare mod/ inputs, run decoder-first routing, coordinate plugin skills, build final_mod output, or explain QA/manual-test blockers."
---

# Skyrim Mod CHS Translation Plugin

Windows 环境下的《上古卷轴5：天际》SE/AE Mod 简体中文汉化插件。

## Scope

Use this as the plugin entry point for Windows-based The Elder Scrolls V: Skyrim Special Edition / Anniversary Edition Mod Simplified Chinese localization. In Chinese user-facing terms, this is a Windows-only 《上古卷轴5：天际》Mod 简体中文汉化插件. The plugin supplies rules, Skills, scripts, config templates, QA logic, glossary seeds, and workspace initialization. A workspace holds each run's `mod/`, `work/`, `qa/`, `out/`, `source/`, `translated/`, `glossary/`, and local tool config.

Keep all Mod translation work workspace-local. Never read from or write to a real game directory, MO2/Vortex directory, or AppData configuration directory. Treat `mod/` as the only input sandbox. Do not directly edit plugin, archive, script, DLL, executable, or other binary files.

## Workspace First

First locate the workspace root. Prefer the nearest ancestor containing `.skyrim-chs-workspace.json`. If absent, use the current directory only when it contains a `mod/` sandbox and workspace QA/output directories. Reusable scripts, Skills, and policy belong to the installed plugin source, not to the workspace.

Workspace initialization is coordinated by this Skill and executed by the plugin Python initializer. To create a new workspace, run:

```console
python scripts/init_workspace.py <workspace>
```

The target must be a non-existent path or an existing empty directory outside the plugin repository. The initializer refuses the plugin repository itself, any directory inside the plugin repository, existing files, and non-empty directories. `--force` is only a deprecated compatibility flag and does not permit overwriting a non-empty or existing workspace.

The initializer creates `.skyrim-chs-workspace.json`, runtime directories, `config/tools.local.json`, a user-editable `glossary/` seed directory, and initial QA/state handoff reports. It does not copy `.codex-plugin/`, `skills/`, `.codex/skills/`, `scripts/`, `adapters/`, or the full documentation tree into the workspace, because those belong only to the reusable plugin source. Existing workspaces should be operated or refreshed with workflow/state scripts, not reinitialized. `scripts/init_project.py` remains a compatibility wrapper around `scripts/init_workspace.py`.

Do not store Mod inputs or generated QA state in the plugin source. A plugin repository is the reusable source tree; a workspace is the per-Mod run directory created by the initializer.

When the current directory is a workspace, run the plugin source script path recorded in `.skyrim-chs-workspace.json` or use normalized absolute commands from workflow reports. Do not copy `scripts/` or `adapters/` into the workspace. Command examples below use plugin-source shorthand.

## Status And Handoff

For status questions or before choosing workflow actions, read the plugin `workflow-policy-and-state` Skill and then refresh missing/stale reports in this order:

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
```

Report `project_state`, `last_success_stage`, blockers, allowed next action, and the concrete next plugin-provided Python command for the current workspace. If the state is `needs_input`, ask for a sandboxed Mod archive or directory under `mod/`.

## Workflow Routing

Use the plugin downstream Skills after this entry point:

- `workflow-policy-and-state` for current stage, allowed action, and next command.
- `skyrim-mod-translation-orchestrator` for end-to-end workflow coordination.
- `translation-task-router` before processing any individual file.
- `mod-input-preparation` for scanning or extracting `mod/` inputs.
- `text-resource-translation`, `mcm-translation`, `esp-esm-esl-translation`, `pex-visible-strings-translation`, and `bsa-archive-audit` for file-type rules.
- `lextranslator-gui-automation` and `xtranslator-gui-automation` only after routing chooses GUI fallback.
- `qa-validation` after translation, tool outputs, final_mod builds, or readiness refreshes.
- `final-mod-assembly` only for assembling `out/<ModName>/汉化产出/final_mod` and `<ModName>_CHS.zip`.

## Main Commands

Prepare queued inputs:

```console
python scripts/run_translation_queue.py --mode prepare
```

Run the repeatable non-GUI workflow for one Mod:

```console
python scripts/run_non_gui_translation_workflow.py --mod-name <ModName>
```

Resume one low-risk recovery action allowed by the state machine:

```console
python scripts/resume_workflow.py --mod-name <ModName> --mode safe
```

Run strict completion gates:

```console
python scripts/run_non_gui_qa_gates.py --mod-name <ModName> --workspace-path work/extracted_mods/<ModName> --final-mod-dir out/<ModName>/汉化产出/final_mod --strict-complete
```

## Stop Conditions

Stop and report a blocked handoff for missing workspace-local input, unavailable decoder/writeback tools, GUI save failures, unverified plugin or PEX output, stale or failed model review, BSA/BA2 blockers, semantic uncertainty requiring model review, and manual game-test requirements.

Do not claim delivery complete until strict QA passes and the project reports readiness for manual game testing.
