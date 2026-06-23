---
name: skyrim-mod-chs-maintenance
description: "用于维护这个 Skyrim 汉化 Codex 插件仓库。中文触发：修改 README、更新开发者指南、优化 Skill 触发、维护插件、修初始化脚本、修依赖安装、修工作流脚本、验证插件、跑 smoke test、检查插件/工作区边界。Covers root skills/, .codex/skills meta Skills, workflow scripts, health checks, smoke tests, and plugin/workspace boundaries. Do not use for translating Mod content."
---

# Skyrim Mod CHS Maintenance

Windows 环境下的《上古卷轴5：天际》SE/AE Mod 简体中文汉化插件维护指南。

## Scope

Use this Skill for repository maintenance of the Windows-only Skyrim SE/AE Chinese localization plugin. It is not part of the Mod translation runtime.

Root `skills/` is the plugin runtime Skill directory. `.codex/skills/` contains only repository meta Skills for install, usage, and maintenance guidance.

## Maintenance Rules

- Keep `.codex-plugin/plugin.json` valid.
- Keep root `skills/` as a real directory, not a symlink.
- Do not duplicate runtime Skills under `.codex/skills/`.
- Keep `.codex/skills/` limited to:
  - `skyrim-mod-chs-install`
  - `skyrim-mod-chs-usage`
  - `skyrim-mod-chs-maintenance`
- Keep runtime directories (`mod/`, `work/`, `qa/`, `out/`, `source/`, `translated/`) out of the plugin logic boundary. `glossary/` may be copied into initialized workspaces as user-editable seed data.
- Keep `config/tools.local.json` local and uncommitted.
- Keep workspace initialization split between Skill guidance and `scripts/init_workspace.py` enforcement.
- `scripts/init_workspace.py` must require a non-existent path or an existing empty directory outside the plugin repository. It must not initialize the plugin repository, any directory inside it, an existing file, or a non-empty directory.
- Keep initialization tool setup explicit in both scripts and Skills: `--tool-setup auto` prepares safe non-GUI tools, `manual` writes reports/checklists only, and `skip` defers setup. Do not let GUI/system tools install silently. Auto mode must install Python packages into workspace `tools/python-venv/`, use pinned .NET SDK version plus install-script hash verification, use pinned and SHA256-verified GitHub archives, write `.skyrim-chs-tool.json` manifests for auto-managed tool directories, and configure BSA extraction through `scripts/invoke_bsa_file_extractor_safe.py` rather than the third-party extractor directly.
- Do not copy `.codex-plugin/`, `skills/`, `.codex/skills/`, `scripts/`, `adapters/`, or the full documentation tree into initialized workspaces. Only the plugin source repository should carry reusable plugin code, controlled adapter source, and Skills. Copying `glossary/` is allowed because workspace terms and user-added dictionaries are run-specific state.
- When updating docs or Skills, keep command examples clear that workflow scripts live in the plugin source and are executed against the workspace; do not imply that initialized workspaces contain their own `scripts/`.
- Keep glossary docs aligned: plugin `glossary/` is only a default seed, while workspace `glossary/` is editable state and may contain user-added dictionary files or subdirectories.

## Validation

After structural changes, run:

```console
python "$env:USERPROFILE\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py" .
python scripts\init_workspace.py D:\SkyrimCHS\maintenance-smoke --tool-setup manual
```

Then inspect:

```text
D:\SkyrimCHS\maintenance-smoke\.skyrim-chs-workspace.json
D:\SkyrimCHS\maintenance-smoke\qa\tool_setup.md
D:\SkyrimCHS\maintenance-smoke\qa\codex_handoff.json
D:\SkyrimCHS\maintenance-smoke\qa\workflow_health.json
```

The empty workspace should report `needs_input`. It should not report Skill directory blockers.

Also smoke-test the initializer refusal paths: an existing non-empty directory, the plugin repository itself, and a directory inside the plugin repository must all fail.

## Editing Guidance

Use focused changes. Prefer updating existing scripts and Skills over adding new entrypoints. When changing workflow state behavior, refresh readiness, workflow state, workflow tasks, and codex handoff before drawing conclusions.
