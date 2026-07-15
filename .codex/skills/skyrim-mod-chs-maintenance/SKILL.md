---
name: skyrim-mod-chs-maintenance
description: "用于维护这个 Bethesda Mod 汉化 Codex 插件仓库。中文触发：修改 README、更新开发者指南、优化 Skill 触发、维护插件、修初始化脚本、修依赖安装、修工作流脚本、进度卡、Trace、验证插件、跑 smoke test、检查插件/工作区边界。Covers the Skyrim SE/AE stable and Fallout 4 Experimental Game Profile implementation, root skills/, meta Skills, workflow scripts, health checks, smoke tests, and plugin/workspace boundaries. Do not use for translating Mod content."
---

# Skyrim Mod CHS Maintenance

Windows 环境下的 Bethesda Mod 简体中文汉化插件维护指南。

## Scope

Use this Skill for repository maintenance of the Windows-only Bethesda Mod Chinese localization plugin. Skyrim SE/AE is stable; Fallout 4 remains Experimental. This Skill is not part of the Mod translation runtime.

Root `skills/` is the plugin runtime Skill directory. `.codex/skills/` contains only repository meta Skills for install, usage, and maintenance guidance.

## Maintenance Rules

- Keep `.codex-plugin/plugin.json` valid.
- Keep `.claude-plugin/marketplace.json` and `.claude-plugin/plugin.json` valid for Claude Code marketplace support.
- Keep the Claude Code marketplace non-GUI only. It must not expose `lextranslator-gui-automation`, `xtranslator-gui-automation`, GUI, Computer Use, pywinauto, UI Automation, or desktop automation capability.
- Keep `agents/` as lightweight adapter metadata for Codex, opencode, and Claude Code. Do not put runtime workflow logic there.
- Keep root `skills/` as a real directory, not a symlink.
- Do not duplicate runtime Skills under `.codex/skills/`.
- Do not duplicate runtime Skills under `agents/`; opencode and Claude Code must consume the shared root `skills/` registry/context export.
- Keep tracked `.codex/skills/` limited to:
  - `skyrim-mod-chs-install`
  - `skyrim-mod-chs-usage`
  - `skyrim-mod-chs-maintenance`
- Local tool-generated OpenSpec files, such as `*openspec-*` Skills and `*opsx*` commands, may exist in a developer checkout only when they are ignored by Git and excluded from repository validation.
- Keep workspace runtime/output directories (`mod/`, `work/`, `qa/`, `out/`, `source/`, `translated/`) and progress/trace directories (`.workflow/`, `traces/`) out of the plugin logic boundary. `glossary/` may be copied into initialized workspaces as user-editable seed data.
- Keep `config/tools.local.json` local and uncommitted.
- Keep workspace initialization split between Skill guidance and `scripts/init_workspace.py` enforcement.
- `scripts/init_workspace.py` must require a non-existent path or an existing empty directory outside the plugin repository. It must not initialize the plugin repository, any directory inside it, an existing file, or a non-empty directory.
- Keep initialization tool setup explicit in both scripts and Skills: `--tool-setup auto` prepares safe non-GUI tools, `manual` writes reports/checklists only, and `skip` defers setup. Do not let GUI/system tools install silently. Auto mode must install Python packages into workspace `tools/python-venv/`; it may prefer `uv venv` and `uv pip install` when uv is available, but must fall back to standard `python`, `venv`, and `pip`. Auto mode must use pinned .NET SDK version plus install-script hash verification, use pinned and SHA256-verified GitHub archives, write `.skyrim-chs-tool.json` manifests for auto-managed tool directories, and configure BSA extraction through `scripts/invoke_bsa_file_extractor_safe.py` rather than the third-party extractor directly.
- Do not copy `.codex-plugin/`, `skills/`, `.codex/skills/`, `scripts/`, `adapters/`, or the full documentation tree into initialized workspaces. Only the plugin source repository should carry reusable plugin code, controlled adapter source, and Skills. Copying `glossary/` is allowed because workspace terms and user-added dictionaries are run-specific state.
- When updating docs or Skills, keep command examples clear that workflow scripts live in the plugin source and are executed against the workspace; do not imply that initialized workspaces contain their own `scripts/`.
- Keep glossary docs aligned: plugin `glossary/` is only a default seed, while workspace `glossary/` is editable state and may contain user-added dictionary files or subdirectories.
- Keep Codex plugin performance stable. New opencode/Claude Code adapter capability/backend checks, context export, Skill registry, or `write_agent_handoff.py` must stay explicit or CI-only and must not be inserted into existing Codex translation hot paths.

## Validation

After structural changes, run:

```powershell
$env:PYTHONUTF8 = "1"
python "$env:USERPROFILE\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py" .
python scripts\validate_claude_plugin_marketplace.py
$smoke = Join-Path $env:TEMP ("skyrim-mod-chs-maintenance-" + [guid]::NewGuid().ToString("N"))
python scripts\init_workspace.py $smoke --game skyrim-se --tool-setup manual
```

Use the same `$smoke` value for inspection. The generated path is unique and initially nonexistent, so repeated maintenance runs do not collide with an earlier workspace. Then inspect:

```text
$smoke\.skyrim-chs-workspace.json
$smoke\qa\tool_setup.md
$smoke\qa\translation_readiness.json
$smoke\qa\workflow_state.json
$smoke\qa\workflow_tasks.json
$smoke\qa\codex_handoff.json
$smoke\qa\workflow_health.json
$smoke\.workflow\progress_card.md
$smoke\.workflow\progress_card.json
$smoke\.workflow\progress_events.jsonl
$smoke\.workflow\workflow_state.json
$smoke\qa\workflow_timeline.md
$smoke\qa\blockers.md
```

The empty workspace should report `needs_input`. It should not report Skill directory blockers.
If a workflow or queue entry ran, also inspect `traces\trace_summary.md`; initialization alone may only create the trace directory.
For any workflow, queue, strict-gate, health, state-refresh, or recovery command, verify the execution contract: the agent must re-read `.workflow\progress_card.md` after the command and output the full Markdown card directly in the chat body so it renders as a heading and table. Do not wrap it in triple backticks, a code block, a quote block, or any container that shows raw Markdown. A stdout-only progress card or a hand-written summary is a maintenance failure, even when the files were generated correctly.

Also smoke-test the initializer refusal paths: an existing non-empty directory, the plugin repository itself, and a directory inside the plugin repository must all fail.

## Editing Guidance

Use focused changes. Prefer updating existing scripts and Skills over adding new entrypoints. When changing workflow state behavior, refresh readiness, workflow state, workflow tasks, and codex handoff before drawing conclusions. `scripts/write_workflow_state.py` must also refresh `.workflow/progress_card.md`, `.workflow/progress_card.json`, `.workflow/progress_events.jsonl`, `.workflow/workflow_state.json`, `qa/workflow_timeline.md`, and `qa/blockers.md`; verify those files when progress behavior changes.
