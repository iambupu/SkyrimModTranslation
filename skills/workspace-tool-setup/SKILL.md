---
name: workspace-tool-setup
description: "用于对外入口已确认工作区初始化或工具准备意图后，执行 Skyrim SE/AE 与 Fallout 4 Experimental 工作区创建、Game Profile 确认结果、自动/手动工具准备和依赖修复。中文触发：入口已确认、工作区初始化执行、Game Profile 已确认、--game fallout4、自动准备工具、依赖失败、tools.local.json、Mutagen/dotnet/BA2 工具。Use after the user-facing entry has classified setup intent and obtained any required game choice; perform profile-aware workspace creation, safe non-GUI dependency setup, reports, and recovery. Do not infer the game from a Mod name, translate content, operate GUI, or run final QA."
---

# Workspace Tool Setup

Windows 运行环境；所有可复用动作使用插件源 Python 入口。不得引入 Bash、WSL、Linux 命令或 shell 包装层。

This Skill handles profile-aware workspace initialization and managed tool preparation. Skyrim SE/AE remains the stable complete workflow; Fallout 4 is `Fallout 4 Experimental Support`. Use the explicit `--game` choice and the workspace marker as authority. Never infer the game from a Mod name. Prefer concise Chinese explanations and expose implementation details only for diagnosis.

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

If the user gives a target path, use it. If no path is provided for a new workspace, ask for the path before running initialization. If no valid workspace marker exists and the user has not stated the game, read `config/game_profiles/*.json`; with the current installed profiles, 必须先用自然语言询问：“Skyrim SE/AE 还是 Fallout 4？” Wait for the answer and pass the corresponding explicit `--game` value. If future profiles exist, enumerate all profile display names, game ids, and support levels instead of assuming two choices. Do not start `init_workspace.py` without `--game` and do not treat its terminal prompt as Agent-user interaction. Existing valid markers remain authoritative and do not need reconfirmation.

Map tool preference:

- `auto`: 自动安装依赖, 自动准备工具, 一键初始化, 不想手动配置非 GUI 工具.
- `manual`: 手动配置工具, 我自己安装, 不要下载, 只生成清单/报告.
- `skip`: 跳过工具准备, 以后再配置.

If tool preference is not stated, use the public default `auto`. Use `manual`
or `skip` only when the user explicitly selects it; do not add a separate tool
mode question after the input and game are already known.

## Commands

Run initialization from the plugin source repository:

```powershell
python scripts/init_workspace.py <workspace> --game skyrim-se --tool-setup auto
python scripts/init_workspace.py <workspace> --game skyrim-se --tool-setup manual
python scripts/init_workspace.py <workspace> --game skyrim-se --tool-setup skip
python scripts/init_workspace.py <workspace> --game fallout4 --tool-setup auto
```

Run tool preparation from an existing workspace, using the plugin source script:

```powershell
python <plugin-root>\scripts\setup_workspace_tools.py --mode auto
python <plugin-root>\scripts\setup_workspace_tools.py --mode manual
```

When the current directory is a workspace, the plugin source path is recorded in `.skyrim-chs-workspace.json` as `plugin_root`.

## Auto Mode Contract

`auto` may install or prepare only safe non-GUI managed pieces:

- a machine-shared immutable Python runtime from the committed hash-pinned export derived from `uv.lock`; prefer uv copy mode and fall back to stdlib venv plus pip using the same exact hashes;
- a machine-shared pinned .NET 8 SDK verified by exact package SHA-256;
- machine-shared pinned BSAFileExtractor and Champollion source snapshots verified by archive SHA-256;
- machine-shared .NET adapter outputs keyed by source/project digest, SDK identity, build configuration, target framework, RID and architecture;
- an atomic `.workflow/managed-tools.json` binding for the current workspace while preserving user external paths in `config/tools.local.json`.

`auto` must not silently install or launch GUI/system tools:

- LexTranslator;
- xTranslator;
- ESP-ESM Translator/EET4;
- SSEEdit/xEdit;
- B.A.E.;
- 7-Zip;
- any tool that requires user license, UI confirmation, or system-wide installer changes.

For GUI/system tools, explain that the user installs them manually and fills `config/tools.local.json`.

When auto mode succeeds, controller-supervised post-binding workflow children use the Python entry recorded in `.workflow/managed-tools.json` while holding its runtime lease. Setup, doctor, maintenance, and the outer controller continue to use bootstrap Python. BSA extraction must stay routed through `scripts/invoke_bsa_file_extractor_safe.py`; never treat the third-party payload file itself as the configured safe wrapper.

Legacy workspace-local tool paths are migration candidates only when the exact known path and project manifest prove ownership. Migration is copy-only and leaves the legacy bytes untouched. Unknown or incompletely proven legacy-looking content must block automatic replacement; never overwrite, delete, or silently bypass it. When ownership is proven but the copied payload cannot prove the current deterministic key and complete inventory, retain the legacy bytes and use normal shared provisioning or report its independent blocker.

A proven legacy copy is never a diagnostic or runtime fallback. Until `auto` has copied it into a verified shared entry and committed a valid binding, detectors report the auto-managed tool unavailable and controlled wrappers must not execute the legacy payload. If a binding file exists but is unsafe, stale, or points to an uninstalled entry, fail closed and run `auto`; do not silently downgrade to bootstrap Python or the legacy workspace venv.

Released schema-v2 workspace markers may predate `workspace_id`. Read-only diagnostics must not rewrite them: they may use only a safely read, structurally valid SMT session UUID whose game matches the marker. `auto` performs the compatibility upgrade under the dedicated workspace identity process lock, atomically persists the matching session UUID, or assigns a new UUID only when no session exists. An invalid marker UUID or any marker/session game or UUID conflict is a blocker and must remain unchanged.

Managed plugin, PEX, localized-delivery, and string-table adapters must execute with the leased managed .NET SDK recorded by their binding/build identity. Binding and runtime resolution cross-check the adapter key's `sdk_entry_id` against the bound `dotnet-sdk` entry. Runtime leases first hold the store lifecycle guard and then acquire adapter before SDK, matching maintenance lock order. An external `DotNetSdkPath` keeps precedence only when the selected adapter is also an explicit external tool; it must not replace the SDK underneath a managed adapter.

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
- Missing LexTranslator/xTranslator paths are warnings unless the current Mod specifically needs GUI fallback. `EspEsmTranslatorPath` is optional; EET RAG decoding does not require EET4.
- `needs_input` in `qa/workflow_state.json` is normal for an empty workspace; ask the user to put a Mod archive or directory under `mod/`.

If a command fails, do not rerun blindly. Read the reports first and identify the failing category:

- Python package install: network/package index/Python environment issue.
- .NET SDK preparation: pinned archive download, SHA-256 verification, shared-store publication, or extraction issue.
- GitHub source download: network/GitHub access issue.
- Mutagen adapter build: NuGet restore or C# compile issue.
- Decoder detection: path resolution or config issue.
- GUI config validation: unsafe/missing external tool path issue.

## Recovery Rules

Prefer narrow recovery:

- If reports were not written because setup crashed, fix the crash first, then rerun setup.
- If shared publication was interrupted, rerun `setup_workspace_tools.py --mode auto`; it reuses complete verified entries, and under locks it may quarantine only the damaged final-key entry it is rebuilding. It preserves every workspace-local legacy directory and leaves unrelated `staging/` or `trash/` remnants for the explicit maintenance workflow.
- If only GUI paths are missing, do not rerun auto mode; tell the user which paths to fill.
- If non-GUI decoder reports are ready but one optional adapter build fails, explain whether that adapter is required for the user's current Mod type before blocking the whole workflow.
- If the workspace was already initialized successfully, do not run `init_workspace.py` over it again. Use `setup_workspace_tools.py` or state refresh scripts.

Keep reports and bindings inside the workspace; managed payloads live only in the versioned Windows Local AppData store. Do not copy plugin `scripts/`, `skills/`, `.codex/skills/`, `.codex-plugin/`, or `adapters/` into the workspace. Never invoke cache cleanup from this Skill; explicit cleanup belongs only to `managed-tool-cache-maintenance`.
