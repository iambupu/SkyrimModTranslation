---
name: skyrim-mod-chs-install
description: "用于安装/重新安装/注册/刷新这个 Skyrim 汉化 Codex 插件。中文触发：帮我安装这个插件、安装 Codex 插件、重新安装插件、刷新本地 marketplace、检查插件安装、插件没装好。Covers marketplace registration, plugin validation, reinstall/cache refresh, and checking that Codex points at the current installed plugin source. Do not use for Mod translation, routing, QA, or final_mod assembly."
---

# Skyrim Mod CHS Install

Windows 环境下的《上古卷轴5：天际》SE/AE Mod 简体中文汉化插件安装与注册指南。

## Scope

Use this Skill only for installing, registering, or validating this repository as the `skyrim-mod-chs-translation` Codex plugin source for Windows-based The Elder Scrolls V: Skyrim SE/AE Simplified Chinese Mod localization.

Do not process Mod files, run translation stages, operate GUI tools, or edit binary files from this Skill. For translation work, use the root `skills/` plugin Skills.

## Natural Language Installation

Users do not need to understand Codex marketplace layout. Treat requests such as "帮我安装这个 Skyrim 汉化 Codex 插件", "安装这个 Codex 插件", "重新安装插件", or "刷新本地 marketplace 入口" as plugin installation intent.

From the repository root, run:

```console
python scripts/install_codex_plugin.py
```

This script validates the repository, copies the plugin source into the default personal Codex marketplace plugin directory, writes the personal marketplace entry, and then tries `codex plugin add skyrim-mod-chs-translation@personal` when Codex CLI is available. If Codex CLI is unavailable, the script still prepares the marketplace entry and reports the manual follow-up.

If the user only wants the marketplace entry prepared without invoking Codex CLI, run:

```console
python scripts/install_codex_plugin.py --skip-codex-add
```

When Codex executes the installer, explain that it writes outside the repository into the user's Codex plugin area. After installation, report the installed plugin source path, marketplace path, and whether `codex plugin add` succeeded or needs manual completion in the Codex UI.

## Required Checks

Before changing registration, inspect:

```console
python -c "from pathlib import Path; print(Path('.codex-plugin/plugin.json').is_file()); print(Path('skills').is_dir()); print(Path('scripts/install_codex_plugin.py').is_file())"
```

Then validate the repository as a plugin:

```console
python "$env:USERPROFILE\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py" .
```

If the validator environment lacks `yaml`, install PyYAML into a temporary dependency directory and run with `PYTHONPATH`; do not vendor the dependency into this repository.

The plugin manifest description must identify this as a Windows environment plugin for The Elder Scrolls V: Skyrim SE/AE Simplified Chinese Mod localization. Keep the user-facing introduction in `.codex-plugin/plugin.json` aligned with that wording; marketplace files only register the source path and should not become the primary description source.

## Registration Model

This repository root is the development source. The installer creates or refreshes the user's personal installed copy and marketplace entry. The default personal marketplace is:

```text
%USERPROFILE%\.agents\plugins\marketplace.json
```

The installed plugin source is normally placed under the personal marketplace plugin directory and referenced from the marketplace as `./plugins/skyrim-mod-chs-translation`. If an older copy under another plugin/cache location exists, do not treat it as canonical unless the user explicitly asks to restore that model.

## After Changes

Run plugin validation and summarize:

- plugin name from `.codex-plugin/plugin.json`
- marketplace source path
- whether `skills/` is a real directory
- whether `.codex/skills/` contains only repository meta Skills
