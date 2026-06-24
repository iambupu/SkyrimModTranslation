---
name: skyrim-mod-chs-translation
description: "Skyrim SE/AE Mod 简体中文汉化插件对外入口和总说明。中文触发：翻译 mod、汉化 mod、开始汉化、继续汉化、初始化工作区、创建工作区、自动安装依赖、依赖装失败、检查状态、现在进度到哪了、进度卡、继续处理、检查工具、准备 mod、生成 final_mod、能不能测试、blocked 怎么办。Use first for user natural-language request recognition, workspace/tool setup intent, progress/status questions, plugin overview, and handoff to the correct downstream Skill. Do not use as the runtime pipeline orchestrator after the task is classified; delegate state-machine progression and script sequencing to skyrim-mod-translation-orchestrator."
---

# Skyrim Mod CHS Translation Plugin

Windows 环境下的《上古卷轴5：天际》SE/AE Mod 简体中文汉化插件。

## Scope

Use this as the plugin entry point for Windows-based The Elder Scrolls V: Skyrim Special Edition / Anniversary Edition Mod Simplified Chinese localization. In Chinese user-facing terms, this is a Windows-only 《上古卷轴5：天际》Mod 简体中文汉化插件. The plugin supplies rules, Skills, scripts, config templates, QA logic, glossary seeds, and workspace initialization. A workspace holds each run's `mod/`, `work/`, `qa/`, `out/`, `source/`, `translated/`, `glossary/`, and local tool config.

Keep all Mod translation work workspace-local. Never read from or write to a real game directory, MO2/Vortex directory, or AppData configuration directory. Treat `mod/` as the only input sandbox. Do not directly edit plugin, archive, script, DLL, executable, or other binary files.

## Role Contract

This Skill is the user-facing entry and overview layer. Use it first when the user speaks in natural language, when the workspace or tool setup intent is unclear, when the user asks for status/progress, or when the request needs to be mapped to a downstream Skill.

This Skill does not own runtime pipeline sequencing after the request is classified. For an actual end-to-end translation run, first read `workflow-policy-and-state` for current state and allowed actions, then hand the runtime strategy to `skyrim-mod-translation-orchestrator`. Do not make this Skill a second total controller for script ordering, recovery policy, QA promotion, or final_mod delivery.

## Workspace First

First locate the workspace root. Prefer the nearest ancestor containing `.skyrim-chs-workspace.json`. If absent, use the current directory only when it contains a `mod/` sandbox and workspace QA/output directories. Reusable scripts, Skills, and policy belong to the installed plugin source, not to the workspace.

Workspace initialization is coordinated by this Skill and executed by the plugin Python initializer. When creating a workspace for a user, choose an explicit tool setup mode instead of leaving tool handling implicit:

```console
python scripts/init_workspace.py <workspace> --tool-setup auto
python scripts/init_workspace.py <workspace> --tool-setup manual
python scripts/init_workspace.py <workspace> --tool-setup skip
```

Users may ask for workspace initialization in natural language instead of naming the script. Treat requests such as "帮我初始化一个新的汉化工作区", "在 D:\SkyrimCHS\MyMod 创建工作区", or "一键准备一个天际 Mod 汉化项目" as workspace initialization intent. Extract the target path or workspace name from the request. If no target path is provided, ask for the path before running the initializer; do not invent a workspace under the plugin repository.

Also extract the tool setup preference from natural language:

- Use `--tool-setup auto` for wording such as 自动安装工具, 自动准备工具, 一键初始化, or 不想手动配置非 GUI 工具.
- Use `--tool-setup manual` for wording such as 手动配置工具, 我自己安装工具, 不要下载工具, or 只生成配置/清单.
- Use `--tool-setup skip` only for wording such as 跳过工具准备 or 以后再配置工具.
- If the user gives a path but no clear tool preference, prefer asking one short follow-up. In non-interactive contexts, use `manual` rather than leaving the initializer waiting.

Run the initializer from the plugin source repository. After it succeeds, tell the user to open the new workspace directory in Codex for actual Mod translation work.

Use `--tool-setup auto` when the user wants non-GUI tool preparation handled by the plugin. Auto mode installs Python packages into workspace `tools/python-venv/`, prepares a pinned project-local .NET 8 SDK from the plugin's vendored `scripts/vendor/dotnet-install.ps1` after installer hash verification, downloads pinned and SHA256-verified GitHub non-GUI tools such as BSAFileExtractor and Champollion source, updates `config/tools.local.json`, and builds available Mutagen adapters with source/DLL hash manifests. It must not silently install GUI/system tools such as LexTranslator, xTranslator, SSEEdit/xEdit, B.A.E., or 7-Zip; those remain user-installed and path-configured. BSA extraction remains configured through `scripts/invoke_bsa_file_extractor_safe.py`, not through direct third-party extractor invocation. Existing auto-managed tool directories without `.skyrim-chs-tool.json` are not trusted and should be replaced by auto setup.

Use `--tool-setup manual` when the user wants manual tool installation. Use `--tool-setup skip` only when the user explicitly wants no tool setup now. In non-interactive contexts, `ask` resolves to `manual`.

The target must be a non-existent path or an existing empty directory outside the plugin repository. The initializer refuses the plugin repository itself, any directory inside the plugin repository, existing files, and non-empty directories. `--force` is only a deprecated compatibility flag and does not permit overwriting a non-empty or existing workspace.

The initializer creates `.skyrim-chs-workspace.json`, runtime directories, `.workflow/`, `traces/`, `config/tools.local.json`, a user-editable `glossary/` seed directory, and initial QA/state handoff reports. It does not copy `.codex-plugin/`, `skills/`, `.codex/skills/`, `scripts/`, `adapters/`, or the full documentation tree into the workspace, because those belong only to the reusable plugin source. Existing workspaces should be operated or refreshed with workflow/state scripts, not reinitialized. `scripts/init_project.py` remains a compatibility wrapper around `scripts/init_workspace.py`.

Do not store Mod inputs or generated QA state in the plugin source. A plugin repository is the reusable source tree; a workspace is the per-Mod run directory created by the initializer.

When the current directory is a workspace, run the plugin source script path recorded in `.skyrim-chs-workspace.json` or use normalized absolute commands from workflow reports. If workspace `tools/python-venv/` exists, use that workspace Python for plugin script commands so auto-installed packages are visible. Do not copy `scripts/` or `adapters/` into the workspace. Command examples below use plugin-source shorthand.

## Status And Handoff

For status questions or before choosing workflow actions, read the plugin `workflow-policy-and-state` Skill and then refresh missing/stale reports in this order:

```console
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

`scripts/write_workflow_state.py` also emits `.workflow/progress_card.md`, `.workflow/progress_card.json`, `.workflow/progress_events.jsonl`, `.workflow/workflow_state.json`, `qa/workflow_timeline.md`, and `qa/blockers.md`. If the user only asks where progress stands, read `.workflow/progress_card.md` and summarize that card instead of rebuilding state.

After any workflow, queue, strict-gate, health, state-refresh, or recovery command, read `.workflow/progress_card.md` again and paste the complete Markdown card verbatim into the chat. Do not rely on the command stdout copy of the card, a summary, or a hand-written status because Codex desktop can collapse command output. Stopping without this read-and-paste step violates the output contract.

Report `project_state`, `last_success_stage`, blockers, allowed next action, and the concrete next plugin-provided Python command for the current workspace. If the state is `needs_input`, ask for a sandboxed Mod archive or directory under `mod/`.

## Workflow Routing

Use the plugin downstream Skills after this entry point. The selection order is: classify the user request here, read state/policy when workflow action is needed, then use the runtime Skill or file-type Skill below.

- `workflow-policy-and-state` for current stage, allowed action, and next command.
- `workspace-tool-setup` for workspace initialization, auto/manual tool setup, setup report triage, and dependency-install recovery.
- `skyrim-mod-translation-orchestrator` for internal runtime workflow coordination after the request is classified as an end-to-end Mod translation run.
- `translation-task-router` before processing any individual file.
- `mod-input-preparation` for scanning or extracting `mod/` inputs.
- `text-resource-translation`, `mcm-translation`, `esp-esm-esl-translation`, `pex-visible-strings-translation`, and `bsa-archive-audit` for file-type rules.
- `lextranslator-gui-automation` and `xtranslator-gui-automation` only after routing chooses GUI fallback.
- `qa-validation` after translation, tool outputs, final_mod builds, or readiness refreshes.
- `final-mod-assembly` only for assembling `out/<ModName>/汉化产出/final_mod` and `<ModName>_CHS.zip`.

## Main Commands

These commands are user-facing handoff shortcuts. Runtime ordering, retries, QA promotion, and final_mod delivery decisions belong to `workflow-policy-and-state` plus `skyrim-mod-translation-orchestrator`.

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
