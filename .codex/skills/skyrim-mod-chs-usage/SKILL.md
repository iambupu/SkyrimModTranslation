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

Users do not need to know the command first. They can ask Codex in natural language and provide the Mod path and game, for example:

```text
翻译 D:\Mods\MyMod.zip，这是 Skyrim SE/AE Mod
```

```text
翻译 D:\Mods\MyMod.7z，这是 Fallout 4 Mod，工具只做手动检测
```

```text
翻译 D:\Mods\MyMod，工作区固定放到 D:\SkyrimCHS\MyMod
```

The top-level Agent uses only the public controller for the first run:

```powershell
python scripts\smt.py --format json run <Mod路径> --game <game-id>
```

It maps tool preference to `--tool-setup auto`, `manual`, or `skip`. If the request does not identify the game and there is no existing marker, the runtime agent must read `config/game_profiles/*.json`, use natural language（自然语言）to ask “Skyrim SE/AE 还是 Fallout 4”, and wait for the answer before passing `--game`. Do not infer the game from the Mod name or use a CLI prompt instead of the Agent conversation.

Map Skyrim SE/AE to `--game skyrim-se` and Fallout 4 to `--game fallout4`. New inputs default to a newly allocated single-Mod workspace under the user's `Documents\SkyrimModTranslationWorkspaces`; `--workspace` selects a specific new or matching existing workspace. New workspaces have no default game.

Use `--tool-setup auto` when the user wants Codex to prepare safe non-GUI tools. Auto mode publishes or reuses immutable machine-shared Python, pinned .NET SDK, hash-verified BSAFileExtractor/Champollion source, and source-keyed adapter generations under the versioned Windows Local AppData managed store. The workspace receives only an atomic `.workflow/managed-tools.json` binding and reports; managed cache paths are not written to `config/tools.local.json`. It still does not silently install GUI or system-level tools such as LexTranslator, xTranslator, SSEEdit/xEdit, B.A.E., or 7-Zip. BSA extraction must remain configured through `scripts/invoke_bsa_file_extractor_safe.py`.

When `uv` is available, auto mode may use uv copy mode to build the shared Python runtime. This is optional; standard `venv` and `pip` remain supported, and both paths must consume the committed exact hash-pinned runtime export.

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

Tool setup writes `qa/tool_setup.md`, `qa/decoder_tools_report.md`, and `qa/tools_config_validation.md` when it runs. These are implementation evidence; ordinary users and top-level Agents consume the public command result instead of using internal initialization flags.

Do not re-run initialization over an existing workspace. Let public `run` validate an existing workspace and session identity; use public `resume` to continue it.

The workspace is not itself a plugin source. `.codex-plugin/`, `skills/`, `.codex/skills/`, `scripts/`, `adapters/`, and the full documentation tree are intentionally not copied there. `glossary/` is copied as a user-editable seed directory so `glossary/mod_terms.md` and added dictionaries travel with the workspace. Reusable scripts, controlled adapter source, Skills, and policy remain in the installed plugin source.

When operating from an initialized workspace, do not create or copy workspace-local `scripts/` or `adapters/` directories. The public controller resolves the installed plugin source recorded by the workspace marker and invokes internal scripts under workflow policy; the top-level Agent does not execute commands emitted by internal state/handoff reports.

## Use A Workspace

After the first `run`, the top-level Agent continues through the same public controller only:

```powershell
python scripts\smt.py --format json status
python scripts\smt.py --format json resume
python scripts\smt.py --format json doctor
python scripts\smt.py --format json output
```

Read `outcome`, `workspace`, `mod_name`, `game_id`, `workflow_state`, `next_action`, `progress_card`, and `diagnostics` from the single JSON result. Only `next_action.artifacts` names files for Agent translation, review, or an authorized GUI action; complete that action and return through `resume`. Do not manually combine initializer, queue, refresh, recovery, QA, or final assembly scripts, and do not treat a nonzero exit code alone as a workflow failure.

`status` is a read-only snapshot and the top-level Agent renders its returned `progress_card`; it does not read `.workflow/progress_card.*` directly. `doctor` is read-only and never installs or cleans tools. `output` reports the exact current Mod paths and distinguishes readiness for manual game testing from completed manual validation.

普通状态刷新不得附加 `--run-strict-gate`。严格 QA 只由状态机授权的内部
阶段显式运行；查询状态不能隐式改变门禁或工作流状态。

Shared cache inspection, old-generation cleanup, and full managed-tool uninstall are not public translation subcommands. They are available only through `managed-tool-cache-maintenance` after an explicit user request and must follow inspect -> plan -> exact confirmation -> apply -> inspect.

## Important Boundaries

- `mod/`, `work/`, `qa/`, `out/`, `source/`, `translated/`, `.workflow/`, `traces/`, and `glossary/` belong to the workspace, not to the plugin's reusable logic.
- Users may add new glossary files or subdirectories under `glossary/lextranslator_dynamic_dictionaries/` or maintain confirmed terms in `glossary/mod_terms.md`.
- Real game, MO2/Vortex, Steam, game/manager AppData configuration, and Documents/My Games directories are out of scope. The only AppData exception is the project-controlled versioned managed-tool store; it contains tools and control metadata, never Mod inputs, translations, or game configuration.
- Missing optional tools should not block unrelated text-only workflows.
