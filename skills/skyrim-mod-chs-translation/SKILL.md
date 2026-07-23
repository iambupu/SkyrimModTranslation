---
name: skyrim-mod-chs-translation
description: 'Skyrim SE/AE 稳定支持与 Fallout 4 Experimental Support 的唯一公开自然语言入口。中文触发：翻译 mod、汉化 mod、开始/继续汉化、初始化工作区、选择游戏/Game Profile、--game fallout4、准备工具、检查状态、进度卡、生成 final_mod、blocked 怎么办。顶层 Agent 首先调用 python scripts\smt.py --format json 的 run/status/resume/doctor/output 对应命令，读取 outcome 与 next_action 后再选择 Agent-owned 文件类型或 GUI Skill；不得自行组合底层脚本、推断或默认新工作区游戏。'
---

# Bethesda Mod CHS Translation Entry

This Skill belongs to the reusable `skyrim-mod-chs-translation` plugin source. Actual Mod translation work runs in a separate initialized workspace. The workspace marker selects the game: Skyrim SE/AE has stable complete support, while Fallout 4 is `Fallout 4 Experimental Support`.

## Scope

Use this as the natural-language entry point for the Windows plugin. The plugin source supplies reusable rules, Skills, scripts, adapters, configuration, QA logic, glossary seeds, and workspace initialization. An initialized workspace holds one run's `mod/`, `work/`, `qa/`, `out/`, `source/`, `translated/`, `glossary/`, `.workflow/`, traces, marker, and local tool configuration. Do not treat the plugin source as a translation workspace.

A new workspace has no default game. The workspace marker and resolved Game Profile are authoritative; never guess the game from a Mod name, archive name, or directory name.

Keep all Mod translation work workspace-local. Never read from or write to a real game directory, MO2/Vortex directory, or AppData configuration directory. Treat `mod/` as the only input sandbox. Do not directly edit plugin, archive, script, DLL, executable, or other binary files.

## Role Contract

This Skill is the user-facing entry and overview layer. Use it first when the user speaks in natural language, when the workspace or tool setup intent is unclear, when the user asks for status/progress, or when the request needs to be mapped to a downstream Skill.

This Skill does not own runtime pipeline sequencing after the request is classified. The public CLI projects the existing workflow state and invokes the internal runtime strategy; do not make this Skill a second controller for script ordering, recovery policy, QA promotion, or final_mod delivery.

## Public CLI Contract

顶层 Agent 收到“翻译这个 Mod”后，首次只调用唯一公开入口并固定请求 JSON：

```powershell
python scripts\smt.py --format json run <Mod路径> --game <game-id>
```

后续只调用：

```powershell
python scripts\smt.py --format json resume
python scripts\smt.py --format json status
python scripts\smt.py --format json doctor
python scripts\smt.py --format json output
```

顶层 Agent 必须读取 JSON 的 `outcome`、`workspace`、`mod_name`、`game_id`、`workflow_state`、`next_action.kind`、`next_action.summary`、`next_action.artifacts` 和 `diagnostics`。`next_action.artifacts` 指定的工作区内路径才是候选、校对包或其他动作输入；没有同名顶层字段。若 outcome 是 `needs_agent_translation`，处理指定路径后调用 `resume`；若为 `needs_gui`，只有 Codex 可以执行获授权 GUI 动作，然后调用 `resume`；若为 `needs_user_input`，取得明确输入后调用 `resume`。`status` 只读最近状态快照，`doctor` 只做诊断，`output` 只读公开产物路径。

不得自行组合初始化、queue、canonical refresh、任务领取、恢复、QA、状态生成或 final_mod 底层脚本。不得把内部退出码直接解释为用户结果，也不得让 workflow task 指向 `smt.py` 外层 controller。

## Workspace First

For a new input, pass its directory, ZIP, or 7Z path to public `run`. The CLI resolves explicit workspace, matching current workspace, identity mapping, or a newly allocated workspace in that order. Reusable scripts, Skills, and policy belong to the installed plugin source, not to the workspace.

For initialization requests, identify the target path and explicit game intent. If the target has no valid marker and the user has not selected a game, read `config/game_profiles/*.json`, 使用自然语言询问并等待用户回答。With the current profiles, ask “Skyrim SE/AE 还是 Fallout 4？” If more profiles are installed later, list every display name, game id, and support level. Never infer the game or use a CLI prompt as a substitute for the Agent conversation.

Map the confirmed choice explicitly: Skyrim SE/AE uses `--game skyrim-se`; Fallout 4 uses `--game fallout4`. Pass that choice to public `run`; never invoke an initializer with an omitted game.

If the input path is missing, ask for it. Once input and game intent are known, use public `run`; its default tool setup is `auto`, with `manual` and `skip` available only when the user selects them. Do not duplicate the internal workspace/tool installation contract here.

An existing valid marker is authoritative and does not require another game question. Do not reinitialize an existing workspace or create one inside the plugin repository. Let the public CLI validate marker/session identity; never copy plugin scripts, Skills, adapters, or documentation into the workspace.

## Status And Handoff

For status questions, call public `python scripts\smt.py --format json status`. It reads the latest progress snapshot without rebuilding state; the entry Skill must not choose or execute the refresh chain itself.

Agent-neutral handoff export remains an internal explicit operation owned by the state/orchestration layer. Do not add it to the public entry path or the default Codex hot path.

Report the public `outcome`, workflow state snapshot, blockers and `next_action`. If the result is `needs_user_input`, ask only for the input named by the result.

## Workflow Routing

Use downstream Skills only when the public JSON `next_action` names language, GUI or another Agent-owned action. Internal CLI orchestration continues to use the state/policy and runtime Skills below.

- `workflow-policy-and-state` for internal current-stage and allowed-action interpretation.
- `workspace-tool-setup` for CLI-internal workspace initialization, tool setup and dependency recovery.
- `skyrim-mod-translation-orchestrator` for internal runtime workflow coordination after the request is classified as an end-to-end Mod translation run.
- `translation-task-router` before processing any individual file.
- `mod-input-preparation` for scanning or extracting `mod/` inputs.
- `text-resource-translation`, `mcm-translation`, `esp-esm-esl-translation`, `pex-visible-strings-translation`, `bsa-archive-audit`, and `ba2-archive-audit` for file-type rules.
- `lextranslator-gui-automation` and `xtranslator-gui-automation` only after routing chooses GUI fallback, and only for Codex. opencode/Claude Code must block GUI-only work with `handoff_target=codex`.
- `qa-validation` after translation, tool outputs, final_mod builds, or readiness refreshes.
- `final-mod-assembly` only for assembling `out/<ModName>/汉化产出/final_mod` and `<ModName>_CHS.zip`.

## Delegation Contract

After classifying intent, call the public CLI. Load a downstream Skill only for the concrete Agent-owned `next_action`; then return through public `resume`. Internally, `workflow-policy-and-state` decides allowed actions, `skyrim-mod-translation-orchestrator` owns runtime sequence, file-type Skills own translation rules, `workflow-agent-orchestration` owns blocked recovery, and QA/final assembly keep their gates.

## Stop Conditions

Stop and report a blocked handoff for missing workspace-local input, unavailable required adapters, GUI save failures, unverified plugin, PEX, string-table or localized composite output, stale or mismatched game/profile evidence, failed model review, archive blockers, semantic uncertainty, and manual game-test requirements. Experimental Apply requires explicit opt-in and remains subject to its strict gate; BA2 materialization belongs to `ba2-archive-audit` and never implies repacking support.

Do not claim delivery complete until strict QA passes and the project reports readiness for manual game testing.
