---
name: workspace-tool-setup
description: "用于 Skyrim SE/AE 默认入口与 Fallout 4 Experimental 工作区初始化、Game Profile 选择、自动/手动工具准备和依赖修复。中文触发：初始化工作区、--game fallout4、自动准备工具、依赖失败、tools.local.json、Mutagen/dotnet/BA2 工具。Use for profile-aware workspace creation, safe non-GUI dependency setup, reports, and recovery. Do not infer the game from a Mod name, translate content, operate GUI, or run final QA."
---

# Workspace Tool Setup

This Skill handles profile-aware workspace initialization and local tool preparation. Skyrim SE/AE remains the default complete workflow; Fallout 4 is `Fallout 4 Experimental Support`. Use the explicit `--game` choice and the workspace marker as authority. Never infer the game from a Mod name. Prefer concise Chinese explanations and expose implementation details only for diagnosis.

## Scope

Use this Skill when the user wants to:

- create or initialize a new workspace;
- choose automatic, manual, or skipped tool preparation;
- automatically install safe non-GUI dependencies;
- diagnose `qa/tool_setup.md`, `qa/decoder_tools_report.md`, or `qa/tools_config_validation.md`;
- recover from failed Python dependency, .NET SDK, GitHub source, Mutagen adapter, decoder, or tool path setup.

Do not translate Mod content, operate LexTranslator/xTranslator GUI, assemble `final_mod`, or decide strict QA completion from this Skill.

## Natural Language Routing

Treat these Chinese phrases as workspace/tool setup intent:

- 初始化工作区, 创建工作区, 新建汉化项目, 一键初始化;
- 自动安装依赖, 自动准备工具, 自动装工具, 非 GUI 工具自动装;
- 依赖装失败, 工具安装失败, dotnet 失败, Mutagen 构建失败, BSA 工具失败;
- 检查工具, 检查 tools.local.json, 配置 LexTranslator, 配置 xTranslator.

If the user gives a target path, use it. If no path is provided for a new workspace, ask for the path before running initialization.

Map tool preference:

- `auto`: 自动安装依赖, 自动准备工具, 一键初始化, 不想手动配置非 GUI 工具.
- `manual`: 手动配置工具, 我自己安装, 不要下载, 只生成清单/报告.
- `skip`: 跳过工具准备, 以后再配置.

If the target path is known but tool preference is unclear, ask one short follow-up. In non-interactive contexts, use `manual` so the workflow does not hang.

## Commands

Run initialization from the plugin source repository:

```console
python scripts/init_workspace.py <workspace> --tool-setup auto
python scripts/init_workspace.py <workspace> --tool-setup manual
python scripts/init_workspace.py <workspace> --tool-setup skip
python scripts/init_workspace.py <workspace> --game fallout4 --tool-setup auto
```

Run tool preparation from an existing workspace, using the plugin source script:

```console
python <plugin-root>\scripts\setup_workspace_tools.py --mode auto
python <plugin-root>\scripts\setup_workspace_tools.py --mode manual
```

When the current directory is a workspace, the plugin source path is recorded in `.skyrim-chs-workspace.json` as `plugin_root`.

## Auto Mode Contract

`auto` may install or prepare only safe non-GUI project-local pieces:

- Python requirements from the plugin `requirements.txt` into workspace `tools/python-venv/`; prefer `uv venv` and `uv pip install` when uv is available, and fall back to standard `venv` plus `pip` when uv is missing or fails;
- pinned project-local .NET 8 SDK under workspace `tools/dotnet-sdk/`; reuse an existing project-local SDK only when `dotnet --version` matches the pinned SDK version and its manifest is current or safely migratable from an older verified installer manifest, otherwise install from the plugin's vendored `scripts/vendor/dotnet-install.ps1` only after the installer script hash is verified;
- pinned BSAFileExtractor source under workspace `tools/BSAFileExtractor/`, verified by SHA256;
- pinned Champollion source under workspace `tools/Champollion/`, verified by SHA256;
- non-GUI Mutagen adapter builds from plugin source;
- workspace `config/tools.local.json` decoder paths.

`auto` must not silently install or launch GUI/system tools:

- LexTranslator;
- xTranslator;
- SSEEdit/xEdit;
- B.A.E.;
- 7-Zip;
- any tool that requires user license, UI confirmation, or system-wide installer changes.

For GUI/system tools, explain that the user installs them manually and fills `config/tools.local.json`.

When auto mode succeeds, prefer the workspace Python at `tools/python-venv/Scripts/python.exe` for follow-up plugin Python commands so `py7zr` and `bethesda-structs` are visible. BSA extraction must stay routed through `scripts/invoke_bsa_file_extractor_safe.py`; never treat the third-party `tools/BSAFileExtractor/BSAFileExtractor.py` file itself as the configured safe wrapper.

Auto-managed tool directories must contain `.skyrim-chs-tool.json`. If a previous `tools/BSAFileExtractor`, `tools/Champollion`, or `tools/dotnet-sdk` directory lacks the expected manifest or pinned version/hash, rerun auto setup so the script downloads and verifies a replacement before swapping it in instead of silently trusting the old directory.

## Report Triage

After setup, inspect:

```text
qa/tool_setup.md
qa/decoder_tools_report.md
qa/tools_config_validation.md
```

Interpret results this way:

- `Blocking errors: 0` in `qa/tool_setup.md` means setup completed.
- `No blocking errors` in decoder/config reports means missing optional GUI paths are not fatal.
- Missing LexTranslator/xTranslator paths are warnings unless the current Mod specifically needs GUI fallback.
- `needs_input` in `qa/workflow_state.json` is normal for an empty workspace; ask the user to put a Mod archive or directory under `mod/`.

If a command fails, do not rerun blindly. Read the reports first and identify the failing category:

- Python package install: network/package index/Python environment issue.
- .NET SDK install: PowerShell, SDK package network download, vendored dotnet install script, or SDK extraction issue.
- GitHub source download: network/GitHub access issue.
- Mutagen adapter build: NuGet restore or C# compile issue.
- Decoder detection: path resolution or config issue.
- GUI config validation: unsafe/missing external tool path issue.

## Recovery Rules

Prefer narrow recovery:

- If reports were not written because setup crashed, fix the crash first, then rerun setup.
- If downloads or vendored installer preparation partially completed, rerun `setup_workspace_tools.py --mode auto`; it should reuse existing manifest-verified `tools/` entries, replace unverified old entries only after the new copy is verified, and preserve the old directory if the replacement preparation fails.
- If only GUI paths are missing, do not rerun auto mode; tell the user which paths to fill.
- If non-GUI decoder reports are ready but one optional adapter build fails, explain whether that adapter is required for the user's current Mod type before blocking the whole workflow.
- If the workspace was already initialized successfully, do not run `init_workspace.py` over it again. Use `setup_workspace_tools.py` or state refresh scripts.

Keep all installed tools and reports inside the workspace. Do not copy plugin `scripts/`, `skills/`, `.codex/skills/`, `.codex-plugin/`, or `adapters/` into the workspace.
