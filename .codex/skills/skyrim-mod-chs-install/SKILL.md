---
name: skyrim-mod-chs-install
description: "Use for repository-local installation and registration guidance for the Windows-only The Elder Scrolls V: Skyrim SE/AE Simplified Chinese localization Codex plugin, including marketplace registration, plugin validation, reinstall/cache refresh, and checking that Codex points at this repository rather than an old plugin copy. Do not use for Mod translation, routing, QA, or final_mod assembly."
---

# Skyrim Mod CHS Install

Windows 环境下的《上古卷轴5：天际》SE/AE Mod 简体中文汉化插件安装与注册指南。

## Scope

Use this Skill only for installing, registering, or validating this repository as the `skyrim-mod-chs-translation` Codex plugin source for Windows-based The Elder Scrolls V: Skyrim SE/AE Simplified Chinese Mod localization.

Do not process Mod files, run translation stages, operate GUI tools, or edit binary files from this Skill. For translation work, use the root `skills/` plugin Skills.

## Required Checks

Before changing registration, inspect:

```console
python - <<'PY'
from pathlib import Path
print(Path(".codex-plugin/plugin.json").is_file())
print(Path(".agents/plugins/marketplace.json").is_file())
print(Path("skills").is_dir())
PY
```

Then validate the repository as a plugin:

```console
python /Users/liuxiaodong07/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
```

If the validator environment lacks `yaml`, install PyYAML into a temporary dependency directory and run with `PYTHONPATH`; do not vendor the dependency into this repository.

The plugin manifest description must identify this as a Windows environment plugin for The Elder Scrolls V: Skyrim SE/AE Simplified Chinese Mod localization. Keep the user-facing introduction in `.codex-plugin/plugin.json` aligned with that wording; marketplace files only register the source path and should not become the primary description source.

## Registration Model

The canonical plugin source is this repository root. The repository-local marketplace is:

```text
.agents/plugins/marketplace.json
```

The default personal marketplace may also point at this repository:

```text
~/.agents/plugins/marketplace.json
```

If an older copy under `~/plugins/skyrim-mod-chs-translation` exists, do not treat it as canonical unless the user explicitly asks to restore that model.

## After Changes

Run plugin validation and summarize:

- plugin name from `.codex-plugin/plugin.json`
- marketplace source path
- whether `skills/` is a real directory
- whether `.codex/skills/` contains only repository meta Skills
