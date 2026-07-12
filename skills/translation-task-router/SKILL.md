---
name: translation-task-router
description: "用于任何 Bethesda Mod 文件处理前的 Game Profile 分流和风险判断。中文触发：这个文件怎么处理、该用哪个工具、判断文件类型、路由文件、风险等级、能不能直接翻译、BA2、ESP、PEX、MCM。Use before processing files to select the profile-aware Skill, risk, adapter, and output. Routes .ba2 to ba2-archive-audit, Fallout 4 localized plugins/STRINGS to blocked, and Fallout 4 PEX Apply to experimental opt-in. Do not translate, operate GUI, validate QA, or assemble final_mod."
---

# Translation Task Router

## 目标

作为权威路由层，为工作区内文件确定：文件类型、风险等级、推荐 Skill、工具优先级、输出目录和 agent 是否允许直接处理。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入路径和输出路径必须在当前工作区内。
- Mod 原始输入只允许来自当前工作区 `mod/` 沙盒；`source/`、`work/`、`translated/`、`out/` 只处理工作区内派生产物。
- 游戏身份只读工作区 marker/Game Profile；不按 Mod 名或文件名猜游戏。
- 不访问任何真实游戏目录或真实 MO2/Vortex 目录。
- 不直接修改插件或 PEX 二进制。

## 职责边界

- 本 Skill 负责选择下游 Skill 和工具优先级。
- 本 Skill 不决定具体字符串是否应该翻译；该判断由文件类型 Skill 完成。
- 本 Skill 不描述 GUI 操作步骤；GUI Skill 只按本 Skill 的路由结果由 Codex 执行。opencode/Claude Code 遇到 GUI fallback 路由必须 blocked，并记录 `handoff_target=codex`。
- Decoder/CLI 优先级高于 GUI 工具。LexTranslator/xTranslator 仅在 decoder 不可用、格式不支持或写回阶段必须使用 GUI 时作为 Codex-only 后备。

## 路由规则

| 文件类型或路径 | 风险 | 文件类型 Skill | 主工具 | 后备/验证工具 | Agent 直接处理 |
|---|---|---|---|---|---|
| `Interface/translations/*.txt` | 低 | `text-resource-translation` | Agent Text Pipeline | profile 指定验证器；Codex-only GUI fallback | 是，写工作区译文副本；交付编码和结构由当前 profile 决定 |
| MCM 文本 | 中 | `mcm-translation` | Agent Text Pipeline / profile 支持的 decoder | Codex-only GUI fallback | 只处理 profile 允许的玩家可见文本 |
| `.esp/.esm/.esl` | 高 | `esp-esm-esl-translation` | 当前 profile 的 Mutagen adapter | Codex-only GUI fallback | 否；FO4 localized plugin/STRINGS 检测后 blocked，非 localized 仅处理白名单字段 |
| `.pex` | 高 | `pex-visible-strings-translation` | `PexStringToolPath` Export；Apply 按 profile 能力 | Codex-only GUI fallback | 否；FO4 Export 可用，Apply 仅 experimental opt-in 且必须通过 strict gate |
| `.psc` | 高 | `pex-visible-strings-translation` | Agent 只读提取 | 无 | 只读提取，不回写 |
| `Meshes/**/*.xml`、`Textures/**/*.xml`、`FaceGenData/**/*.xml` | 受保护 | `manual-review` | 原样复制 | final_mod 结构校验 | 否，不能自动翻译 |
| `.json/.jsonl/.xml/.csv/.txt/.md` | 低到中 | `text-resource-translation` | Agent Text Pipeline | 无 | 是，保留结构 |
| `.zip` | 中 | `mod-input-preparation` | 工作区内只读解压 | 无 | 只解压到工作区内工作副本 |
| `.bsa` | 中 | `bsa-archive-audit` | `bethesda-structs` 只读归档审计 | `BsaFileExtractorPath` 安全 wrapper | 不直接翻译；汉化内容默认同路径 loose override；未配置审计库时阻断 |
| `.ba2` | 中 | `ba2-archive-audit` | 通用只读 inventory；受控 BA2 wrapper materialization | 独立 receipt/manifest/hash 验证 | 不直接翻译；只允许安全解包和同路径 loose override，不重打包 |
| `.swf/.dll/.exe` | 受保护 | `manual-review` | 只读 inventory / 原样复制 | hash/provenance 校验 | 否，不修改 |
| `.rar` | 中 | `mod-input-preparation` | 提取计划 | 后续明确配置的 RAR adapter | 未配置时默认不解包 |
| `.7z` | 中 | `mod-input-preparation` | Python `py7zr` | `Archive7zPath` | 只解压到工作区内工作副本 |

## 推荐工具

- `scripts/route_translation_task.py`

## 输出

- 控制台路由结果。
- `qa/routing_report.md`。

## QA 检查

- 每个输入路径都在当前工作区内。
- 高风险文件默认不能由 agent 直接处理。
- `.esp/.esm/.esl` 和 `.pex` 按当前 Game Profile 选择 adapter；FO4 localized plugin/STRINGS 必须 blocked，PEX Apply 不得越过 experimental opt-in 和 strict gate。
- `.ba2` 主路由固定为 `ba2-archive-audit`。`bsa-archive-audit` 可提供通用 readonly inventory，但不得 materialize BA2。
- 未知扩展名或路径不安全时路由为 `manual-review`。
- `Meshes/`、`Textures/`、`FaceGenData/` 下的 XML 是资源元数据，不是普通可见文本；路由必须阻止自动翻译，final_mod 中默认要求字节级不变。
- `Interface/translations/*.txt` 的交付态不是普通 UTF-8 文本；必须由 `text-resource-translation` 和 `qa-validation` 按当前 GameContext policy 校验。当前 Skyrim SE/AE 与 Fallout 4 profile 都要求 `utf-16-le-bom`；policy 未知或缺失时阻断。

## 完成标准

- 已为每个候选文件输出 Skill、风险等级、主工具、后备工具和是否允许 agent 直接处理。
- `qa/routing_report.md` 已更新。
- 没有未知或高风险文件被静默放行。
- 下游执行只依据路由结果启动。

## 失败处理

未知扩展名、高风险路径、路径不在项目内或工具选择不明确时，路由为 `manual-review`，不得继续自动翻译。
