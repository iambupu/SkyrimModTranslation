---
name: skyrim-mod-chs-translation
description: "Skyrim SE/AE 稳定完整支持与 Fallout 4 Experimental Support 的 Mod 简体中文汉化插件对外入口。中文触发：翻译 mod、汉化 mod、开始/继续汉化、初始化工作区、选择游戏、--game fallout4、准备工具、检查状态、进度卡、生成 final_mod、blocked 怎么办。Use first to read the workspace marker and Game Profile, ask the user in natural language when a new workspace game is unspecified, recognize intent, answer status/setup questions, and select the downstream Skill. Never infer or default the game for a new workspace. Do not sequence the runtime pipeline; delegate that to skyrim-mod-translation-orchestrator."
---

# Bethesda Mod CHS Translation Entry

This Skill belongs to the reusable `skyrim-mod-chs-translation` plugin source. Actual Mod translation work runs in a separate initialized workspace. The workspace marker selects the game: Skyrim SE/AE has stable complete support, while Fallout 4 is `Fallout 4 Experimental Support`.

## Scope

Use this as the natural-language entry point for the Windows plugin. The plugin source supplies reusable rules, Skills, scripts, adapters, configuration, QA logic, glossary seeds, and workspace initialization. An initialized workspace holds one run's `mod/`, `work/`, `qa/`, `out/`, `source/`, `translated/`, `glossary/`, `.workflow/`, traces, marker, and local tool configuration. Do not treat the plugin source as a translation workspace.

A new workspace has no default game. The workspace marker and resolved Game Profile are authoritative; never guess the game from a Mod name, archive name, or directory name.

Keep all Mod translation work workspace-local. Never read from or write to a real game directory, MO2/Vortex directory, or AppData configuration directory. Treat `mod/` as the only input sandbox. Do not directly edit plugin, archive, script, DLL, executable, or other binary files.

## Role Contract

This Skill is the user-facing entry and overview layer. Use it first when the user speaks in natural language, when the workspace or tool setup intent is unclear, when the user asks for status/progress, or when the request needs to be mapped to a downstream Skill.

This Skill does not own runtime pipeline sequencing after the request is classified. For an actual end-to-end translation run, first read `workflow-policy-and-state` for current state and allowed actions, then hand the runtime strategy to `skyrim-mod-translation-orchestrator`. Do not make this Skill a second total controller for script ordering, recovery policy, QA promotion, or final_mod delivery.

## Workspace First

First locate the workspace root. Prefer the nearest ancestor containing `.skyrim-chs-workspace.json`. If absent, use the current directory only when it contains a `mod/` sandbox and workspace QA/output directories. Reusable scripts, Skills, and policy belong to the installed plugin source, not to the workspace.

For initialization requests, identify the target path and explicit game intent. If the target has no valid marker and the user has not selected a game, read `config/game_profiles/*.json`, 使用自然语言询问并等待用户回答。With the current profiles, ask “Skyrim SE/AE 还是 Fallout 4？” If more profiles are installed later, list every display name, game id, and support level. Never infer the game or use a CLI prompt as a substitute for the Agent conversation.

Map the confirmed choice explicitly: Skyrim SE/AE uses `--game skyrim-se`; Fallout 4 uses `--game fallout4`. Pass that choice to `workspace-tool-setup` instead of running an initializer with an omitted game.

If the target path is missing, ask for it. Once path and game intent are known, delegate initialization, tool setup mode selection, dependency preparation, setup report diagnosis, and recovery to `workspace-tool-setup`. Do not duplicate its installation contract here.

An existing valid marker is authoritative and does not require another game question. Do not reinitialize an existing workspace or create one inside the plugin repository. When operating a workspace, use the plugin source path recorded in its marker or the normalized commands written by workflow reports; never copy plugin scripts, Skills, adapters, or documentation into the workspace.

## Status And Handoff

For status questions, delegate state interpretation and any stale-report refresh to `workflow-policy-and-state`. If the user only asks where progress stands and `.workflow/progress_card.md` exists, read and present that card without rebuilding state. The entry Skill must not choose or execute the refresh chain itself.

Agent-neutral handoff export remains explicit and belongs to the state/orchestration layer. Do not add it to the entry path or the default Codex hot path.

Report `project_state`, `last_success_stage`, blockers, allowed next action, and the concrete next plugin-provided Python command for the current workspace. If the state is `needs_input`, ask for a sandboxed Mod archive or directory under `mod/`.

## Workflow Routing

Use the plugin downstream Skills after this entry point. The selection order is: classify the user request here, read state/policy when workflow action is needed, then use the runtime Skill or file-type Skill below.

- `workflow-policy-and-state` for current stage, allowed action, and next command.
- `workspace-tool-setup` for workspace initialization, auto/manual tool setup, setup report triage, and dependency-install recovery.
- `skyrim-mod-translation-orchestrator` for internal runtime workflow coordination after the request is classified as an end-to-end Mod translation run.
- `translation-task-router` before processing any individual file.
- `mod-input-preparation` for scanning or extracting `mod/` inputs.
- `text-resource-translation`, `mcm-translation`, `esp-esm-esl-translation`, `pex-visible-strings-translation`, `bsa-archive-audit`, and `ba2-archive-audit` for file-type rules.
- `lextranslator-gui-automation` and `xtranslator-gui-automation` only after routing chooses GUI fallback, and only for Codex. opencode/Claude Code must block GUI-only work with `handoff_target=codex`.
- `qa-validation` after translation, tool outputs, final_mod builds, or readiness refreshes.
- `final-mod-assembly` only for assembling `out/<ModName>/汉化产出/final_mod` and `<ModName>_CHS.zip`.

## Delegation Contract

After classifying intent, stop executing from this entry Skill and load exactly the downstream Skill that owns the action. `workflow-policy-and-state` decides the allowed next action; `skyrim-mod-translation-orchestrator` owns runtime sequence; file-type Skills own translation rules; `workflow-agent-orchestration` owns blocked recovery; `qa-validation` and `final-mod-assembly` retain their separate gates.

## Stop Conditions

Stop and report a blocked handoff for missing workspace-local input, unavailable required adapters, GUI save failures, unverified plugin, PEX, string-table or localized composite output, stale or mismatched game/profile evidence, failed model review, archive blockers, semantic uncertainty, and manual game-test requirements. Experimental Apply requires explicit opt-in and remains subject to its strict gate; BA2 materialization belongs to `ba2-archive-audit` and never implies repacking support.

Do not claim delivery complete until strict QA passes and the project reports readiness for manual game testing.
