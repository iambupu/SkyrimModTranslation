---
name: skyrim-mod-chs-usage
description: "用于说明这个 Skyrim 汉化插件怎么用。中文触发：怎么使用、如何开始、初始化工作区、创建工作区、自动安装依赖、自动准备工具、依赖装失败、手动配置工具、mod 放哪里、怎么配置 tools.local.json、怎么看状态、怎么看进度、进度卡、怎么看 QA 报告、怎么继续。Covers workspace creation, tool setup auto/manual/skip, .skyrim-chs-workspace.json, mod/, glossary, workflow state, progress_card, and handoff reports. Do not use for translating strings or editing workflow code."
---

# Skyrim Mod CHS Usage

Windows 环境下的《上古卷轴5：天际》SE/AE Mod 简体中文汉化插件使用指南。

## Scope

Use this Skill when the user asks how to use the Windows-only Skyrim SE/AE Chinese localization plugin or how the plugin and workspace interact.

The plugin repository provides reusable capabilities. Each workspace stores run-specific inputs, outputs, local tool paths, editable glossary data, and QA state. Workspace creation is guided by this Skill and performed by `scripts/init_workspace.py`; the script is the enforcement point for filesystem safety.

## Create A Workspace

Users do not need to know the command first. They can ask Codex in natural language, for example:

```text
帮我在 D:\SkyrimCHS\MyMod 初始化一个新的天际 Mod 汉化工作区，并自动准备非 GUI 工具
```

```text
初始化一个新的工作区，路径是 D:\SkyrimCHS\ManualTools，工具我手动配置
```

```text
帮我创建一个工作区，但先跳过工具准备
```

Map these requests to the initializer by extracting the requested path and translating the tool preference to `--tool-setup auto`, `--tool-setup manual`, or `--tool-setup skip`. If the user does not provide a path, ask for the target path before running the initializer.

From the plugin repository, create a workspace by explicitly choosing the tool setup mode:

```console
python scripts/init_workspace.py <workspace> --tool-setup auto
python scripts/init_workspace.py <workspace> --tool-setup manual
python scripts/init_workspace.py <workspace> --tool-setup skip
```

Use `--tool-setup auto` when the user wants Codex to prepare safe non-GUI tools. Auto mode installs Python requirements into workspace `tools/python-venv/`, prepares a pinned project-local .NET 8 SDK from the plugin's vendored `scripts/vendor/dotnet-install.ps1` after installer hash verification, downloads pinned and SHA256-verified GitHub non-GUI tools such as BSAFileExtractor and Champollion source, updates `config/tools.local.json`, and attempts to build available Mutagen adapters with source/DLL hash manifests. It still does not silently install GUI or system-level tools such as LexTranslator, xTranslator, SSEEdit/xEdit, B.A.E., or 7-Zip. BSA extraction must remain configured through `scripts/invoke_bsa_file_extractor_safe.py`. Existing auto-managed tool directories without `.skyrim-chs-tool.json` should be replaced by auto setup.

Use `--tool-setup manual` when the user wants to install tools themselves. Manual mode writes reports and checklists without downloading tools. Use `--tool-setup skip` only when the user explicitly wants to defer tool setup.

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

Tool setup writes `qa/tool_setup.md`, `qa/decoder_tools_report.md`, and `qa/tools_config_validation.md` when it runs. Use `--skip-initial-state` only when the user wants file creation without initial QA reports.

Do not re-run initialization over an existing workspace. Refresh an existing workspace with workflow/state scripts instead.

The workspace is not itself a plugin source. `.codex-plugin/`, `skills/`, `.codex/skills/`, `scripts/`, `adapters/`, and the full documentation tree are intentionally not copied there. `glossary/` is copied as a user-editable seed directory so `glossary/mod_terms.md` and added dictionaries travel with the workspace. Reusable scripts, controlled adapter source, Skills, and policy remain in the installed plugin source.

When operating from an initialized workspace, do not create or copy workspace-local `scripts/` or `adapters/` directories. Run the plugin source script path recorded in `.skyrim-chs-workspace.json` or use the absolute commands emitted by workflow state/handoff reports; those scripts write outputs to the workspace through the workspace marker or `SKYRIM_CHS_WORKSPACE_ROOT` while loading controlled adapter source from the plugin. Command examples that use `python scripts/...` are plugin-source shorthand, not a requirement that `scripts/` exists in the workspace.

## Use A Workspace

1. Open the workspace directory in Codex.
2. Put a sandboxed Mod archive or directory under `mod/`.
3. If initialization used `manual` or optional GUI tools are needed, configure only needed local tools in `config/tools.local.json`.
4. Refresh state using the plugin-source script path or the normalized absolute commands from `qa/workflow_state.json` / `qa/codex_handoff.json`. In plugin-source shorthand:

```console
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
```

`scripts/write_workflow_state.py` also derives `.workflow/progress_card.md`, `.workflow/progress_card.json`, `.workflow/progress_events.jsonl`, `.workflow/workflow_state.json`, `qa/workflow_timeline.md`, and `qa/blockers.md`. If the user only asks for current progress, read `.workflow/progress_card.md` first and do not rescan the workspace.

After running workflow, queue, strict-gate, health, state-refresh, or recovery commands, read `.workflow/progress_card.md` again and paste the card into the chat. Command stdout can be collapsed in Codex desktop, so a card printed only inside command output is not user-visible progress.

5. For action decisions, read `qa/codex_handoff.json` first, then `qa/workflow_state.json`.

## Important Boundaries

- `mod/`, `work/`, `qa/`, `out/`, `source/`, `translated/`, `.workflow/`, `traces/`, and `glossary/` belong to the workspace, not to the plugin's reusable logic.
- Users may add new glossary files or subdirectories under `glossary/lextranslator_dynamic_dictionaries/` or maintain confirmed terms in `glossary/mod_terms.md`.
- Real game, MO2/Vortex, Steam, AppData, and Documents/My Games directories are out of scope.
- Missing optional tools should not block unrelated text-only workflows.
