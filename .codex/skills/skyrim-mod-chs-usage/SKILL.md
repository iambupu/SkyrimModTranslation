---
name: skyrim-mod-chs-usage
description: "用于回答这个 Bethesda Mod 汉化插件怎么使用。中文触发：怎么使用、如何开始、怎么初始化工作区、如何创建工作区、如何准备工具、mod 放哪里、怎么配置 tools.local.json、怎么看状态、怎么看进度、怎么看进度卡、怎么看 QA 报告。Covers explanatory usage guidance for Skyrim SE/AE and Fallout 4 Experimental workspaces. Do not use when the user asks to actually initialize, prepare tools, translate, resume a workflow, or refresh status; route those actions to the runtime entry and downstream Skills."
---

# Skyrim Mod CHS Usage

Windows 环境下的 Bethesda Mod 简体中文汉化插件使用指南。

## Scope

Use this Skill only when the user asks for instructions or an explanation of how the plugin and workspace interact. It covers Skyrim SE/AE stable support and Fallout 4 Experimental support.

When the user asks Codex to perform initialization, tool setup, translation, status refresh, or recovery, hand the request to `skyrim-mod-chs-translation` and its downstream runtime Skill instead of executing it as repository usage guidance.

The plugin repository provides reusable capabilities. Each workspace stores run-specific inputs, outputs, local tool paths, editable glossary data, and QA state. This Skill explains workspace creation; `workspace-tool-setup` guides the actual action and `scripts/init_workspace.py` enforces filesystem safety.

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

Explain that the runtime entry maps these requests to `--tool-setup auto`, `manual`, or `skip`. If the request does not identify the game and there is no existing marker, the runtime agent must read `config/game_profiles/*.json`, use natural language（自然语言）to ask “Skyrim SE/AE 还是 Fallout 4”, and wait for the answer before passing `--game`. Do not infer the game from the Mod name or use the CLI prompt instead of the Agent conversation.

Map Skyrim SE/AE to `--game skyrim-se` and Fallout 4 to `--game fallout4`. For exact CLI syntax and the complete `auto` / `manual` / `skip` matrix, read the `workspace-tool-setup` Skill's **Commands** section. This explanatory Skill does not duplicate the executable command table.

Use `--tool-setup auto` when the user wants Codex to prepare safe non-GUI tools. Auto mode installs Python requirements into workspace `tools/python-venv/`; it reuses a verified workspace .NET SDK or an explicitly configured plugin-source SDK only when the version exactly matches the pin, and otherwise prepares the pinned workspace SDK from the plugin's vendored `scripts/vendor/dotnet-install.ps1` after installer hash verification. It also downloads pinned and SHA256-verified GitHub non-GUI tools such as BSAFileExtractor and Champollion source, updates `config/tools.local.json`, and attempts to build available Mutagen adapters with source/DLL hash manifests. It still does not silently install GUI or system-level tools such as LexTranslator, xTranslator, SSEEdit/xEdit, B.A.E., or 7-Zip. BSA extraction must remain configured through `scripts/invoke_bsa_file_extractor_safe.py`. Existing auto-managed tool directories without `.skyrim-chs-tool.json` should be replaced by auto setup.

When `uv` is available, auto mode may use `uv venv` and `uv pip install` for the workspace `tools/python-venv/` environment. This is an optional ease-of-use path; standard `python`, `venv`, and `pip` remain supported.

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

```powershell
python scripts/audit_translation_readiness.py
python scripts/write_workflow_state.py
python scripts/test_workflow_health.py
python scripts/write_workflow_tasks.py
python scripts/write_codex_handoff.py
python scripts/audit_project_completion.py
python scripts/new_manual_game_test_plan.py
python scripts/new_manual_game_test_results_template.py
python scripts/audit_translation_goal_compliance.py
```

普通状态刷新不得附加 `--run-strict-gate`。只有 `qa/workflow_state.json` 推荐严格 QA，或用户明确要求严格 QA 时，才运行严格门禁。

`scripts/write_workflow_state.py` also derives `.workflow/progress_card.md`, `.workflow/progress_card.json`, `.workflow/progress_events.jsonl`, `.workflow/workflow_state.json`, `qa/workflow_timeline.md`, and `qa/blockers.md`. If the user only asks for current progress, read `.workflow/progress_card.md` first and do not rescan the workspace.

After running workflow, queue, strict-gate, health, state-refresh, or recovery commands, read `.workflow/progress_card.md` again and paste the complete Markdown card into the chat. Command stdout can be collapsed in Codex desktop, so a card printed only inside command output or replaced by a summary is not user-visible progress.

5. For action decisions, read `qa/codex_handoff.json` first, then `qa/workflow_state.json`.

## Important Boundaries

- `mod/`, `work/`, `qa/`, `out/`, `source/`, `translated/`, `.workflow/`, `traces/`, and `glossary/` belong to the workspace, not to the plugin's reusable logic.
- Users may add new glossary files or subdirectories under `glossary/lextranslator_dynamic_dictionaries/` or maintain confirmed terms in `glossary/mod_terms.md`.
- Real game, MO2/Vortex, Steam, AppData, and Documents/My Games directories are out of scope.
- Missing optional tools should not block unrelated text-only workflows.
