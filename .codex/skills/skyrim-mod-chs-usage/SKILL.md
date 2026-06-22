---
name: skyrim-mod-chs-usage
description: "Use for explaining how to use the Windows-only The Elder Scrolls V: Skyrim SE/AE Simplified Chinese localization Codex plugin, including creating a new workspace, locating .skyrim-chs-workspace.json, configuring tools.local.json, placing Mod input under mod/, maintaining workspace glossary dictionaries, refreshing workflow state, and reading QA handoff reports. Do not use for translating strings or editing workflow code."
---

# Skyrim Mod CHS Usage

Windows 环境下的《上古卷轴5：天际》SE/AE Mod 简体中文汉化插件使用指南。

## Scope

Use this Skill when the user asks how to use the Windows-only Skyrim SE/AE Chinese localization plugin or how the plugin and workspace interact.

The plugin repository provides reusable capabilities. Each workspace stores run-specific inputs, outputs, local tool paths, editable glossary data, and QA state. Workspace creation is guided by this Skill and performed by `scripts/init_workspace.py`; the script is the enforcement point for filesystem safety.

## Create A Workspace

From the plugin repository, create a workspace with:

```console
python scripts/init_workspace.py <workspace>
```

The target must be a non-existent path or an existing empty directory outside the plugin repository. Initialization refuses the plugin repository itself, any directory inside the plugin repository, existing files, and non-empty directories. `--force` is kept only for compatibility and does not allow overwriting a non-empty workspace.

The initializer creates:

```text
.skyrim-chs-workspace.json
config/tools.local.json
glossary/
mod/
source/
translated/
work/
qa/
out/
```

Use `--skip-initial-state` only when the user wants file creation without initial QA reports.

Do not re-run initialization over an existing workspace. Refresh an existing workspace with workflow/state scripts instead.

The workspace is not itself a plugin source. `.codex-plugin/`, `skills/`, `.codex/skills/`, `scripts/`, `adapters/`, and the full documentation tree are intentionally not copied there. `glossary/` is copied as a user-editable seed directory so `glossary/mod_terms.md` and added dictionaries travel with the workspace. Reusable scripts, controlled adapter source, Skills, and policy remain in the installed plugin source.

When operating from an initialized workspace, do not create or copy workspace-local `scripts/` or `adapters/` directories. Run the plugin source script path recorded in `.skyrim-chs-workspace.json` or use the absolute commands emitted by workflow state/handoff reports; those scripts write outputs to the workspace through the workspace marker or `SKYRIM_CHS_WORKSPACE_ROOT` while loading controlled adapter source from the plugin. Command examples that use `python scripts/...` are plugin-source shorthand, not a requirement that `scripts/` exists in the workspace.

## Use A Workspace

1. Open the workspace directory in Codex.
2. Put a sandboxed Mod archive or directory under `mod/`.
3. Configure only needed local tools in `config/tools.local.json`.
4. Refresh state using the plugin-source script path or the normalized absolute commands from `qa/workflow_state.json` / `qa/codex_handoff.json`. In plugin-source shorthand:

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
```

5. Read `qa/codex_handoff.json` first, then `qa/workflow_state.json`.

## Important Boundaries

- `mod/`, `work/`, `qa/`, `out/`, `source/`, `translated/`, and `glossary/` belong to the workspace, not to the plugin's reusable logic.
- Users may add new glossary files or subdirectories under `glossary/lextranslator_dynamic_dictionaries/` or maintain confirmed terms in `glossary/mod_terms.md`.
- Real game, MO2/Vortex, Steam, AppData, and Documents/My Games directories are out of scope.
- Missing optional tools should not block unrelated text-only workflows.
