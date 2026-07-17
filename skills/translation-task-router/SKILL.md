---
name: translation-task-router
description: "用于任何 Bethesda Mod 文件处理前的 Game Profile 分流和风险判断。中文触发：这个文件怎么处理、该用哪个工具、判断文件类型、路由文件、风险等级、能不能直接翻译、BA2、ESP、PEX、MCM。Use before processing files to select the profile-aware Skill, risk, adapter, and output. Routes .ba2 to ba2-archive-audit, Skyrim/Fallout 4 STRINGS and Fallout 4 localized plugins to blocked, and Fallout 4 PEX Apply to experimental opt-in. Do not translate, operate GUI, validate QA, or assemble final_mod."
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
| `MCM/**/*.json`、`MCM/**/*.ini` | 中 | `mcm-translation` | Agent Structured MCM Extractor | Codex-only LexTranslator fallback | 自动处理已确认的玩家可见 value |
| `MCM/**/*.txt` | 低到中 | `mcm-translation` | Agent Text Pipeline | 无 | 自动处理可见文本并保留结构 |
| `MCM/**/*.toml` | 人工 | `mcm-translation` | Structured TOML manual review | 无 | 当前不自动翻译或写回 |
| `.esp/.esm/.esl` | 高 | `esp-esm-esl-translation` | 当前 profile 声明的 plugin adapter | Codex-only GUI fallback | 否；adapter 未实现或 capability 不允许时 blocked；FO4 localized plugin 当前 blocked |
| `.strings/.dlstrings/.ilstrings` | 阻断 | `manual-review` | 仅资源清点 | 无 | 当前 Skyrim 与 Fallout 4 都不满足 read；不得回退到 GUI、其他游戏 adapter 或当普通文本处理 |
| `.pex` | 高或阻断 | `pex-visible-strings-translation` | `PexStringToolPath` Export；Apply 按 profile capability | Codex-only GUI fallback | `pex` capability 不满足 read 时 blocked；FO4 Apply 仅 experimental opt-in 且 strict completion 固定阻断 |
| `.psc` | 高 | `pex-visible-strings-translation` | Agent 只读提取 | 无 | 只读提取，不回写 |
| `Meshes/**/*.xml`、`Textures/**/*.xml`、`FaceGenData/**/*.xml` | 受保护 | `manual-review` | 原样复制 | final_mod 结构校验 | 否，不能自动翻译 |
| `.json/.jsonl/.xml/.csv/.txt/.md` | 低到中 | `text-resource-translation` | Agent Text Pipeline | 无 | 是，保留结构 |
| `.zip` | 中 | `mod-input-preparation` | 工作区内只读解压 | 无 | 只解压到工作区内工作副本 |
| `.bsa` | 中 | `bsa-archive-audit` | `bethesda-structs` 只读归档审计 | `BsaFileExtractorPath` 安全 wrapper | 不直接翻译；汉化内容默认同路径 loose override；未配置审计库时阻断 |
| `.ba2` | 中 | `ba2-archive-audit` | 通用只读 inventory；受控 BA2 wrapper materialization | 独立 receipt/manifest/hash 验证 | 不直接翻译；只允许安全解包和同路径 loose override，不重打包 |
| `Interface/*.swf`、`Interface/*.gfx` | 受保护 | `manual-review` | 只读 inventory / 人工检查 / 原样复制 | hash/provenance 校验 | 否，不修改；优先查找 `Interface/translations/*.txt` |
| `F4SE/**/*.dll` | 受保护 | `manual-review` | 只读 inventory / 原样复制 | hash/provenance 校验 | 否，不修改 |
| `F4SE/**/*.{ini,json,toml}` | 人工 | `manual-review` | INI/TOML 整行注释只读提取；配置值人工确认 | 无 | 不自动翻译 key/value；只允许注释进入候选包 |
| `.rar` | 中 | `mod-input-preparation` | 提取计划 | 后续明确配置的 RAR adapter | 未配置时默认不解包 |
| `.7z` | 中 | `mod-input-preparation` | Python `py7zr` | `Archive7zPath` | 只解压到工作区内工作副本 |

`capabilities.string_tables` 不满足 read 时，STRINGS 家族只能清点并立即 blocked；GUI 可用性不能提升该 capability。

## Fallout 4 Data 资源边界

- `Materials/*.bgsm`、`Materials/*.bgem`，以及 `Meshes/`、`Textures/`、`Sound/`、`Music/`、`Video/`、`Vis/`、`Seq/` 下的资源默认受保护。需要进入 `final_mod` 时，只能从 `mod/` 原样复制，不得从宽泛的 Tool Adapter 输出替换。
- 受保护资源必须记录 original-copy provenance。source SHA256 与 final SHA256 必须相同。扩展名未知也不能绕过目录级保护。
- 路径命中任一 protected container 时，最终按 protected 处理；否则命中 F4SE 时按 f4se 处理。后出现的 `MCM/Scripts` 不能覆盖这两项。
- MCM 是 container，不是单一 JSON 文件类型。JSON/INI 使用 Agent Structured MCM Extractor，LexTranslator 只作为 Codex 后备；TXT 使用 Agent Text Pipeline；TOML 当前只允许 manual review。
- F4SE DLL 只做 inventory 或原样复制。F4SE 下的 JSON value 以及 INI/TOML key/value 只生成结构化人工确认记录；INI/TOML 整行注释可只读提取到候选包。
- Skyrim 与 Fallout 4 的 `.esl` 或带 light trait 的插件只允许 inventory 和受支持的只读导出，不生成 Apply 产物。两个游戏的 STRINGS 家族保持 blocked；Fallout 4 localized 插件同样 blocked。

## 推荐工具

- `scripts/route_translation_task.py`

## 输出

- 控制台路由结果。
- `qa/routing_report.md`。

## QA 检查

- 每个输入路径都在当前工作区内。
- 高风险文件默认不能由 agent 直接处理。
- `.esp/.esm/.esl`、string table 和 `.pex` 按当前 Game Profile capability 选择 adapter；未声明或未实现的能力必须 fail closed。Skyrim/FO4 STRINGS 与 FO4 localized plugin 必须 blocked，PEX Apply 不得越过 experimental opt-in 和 strict gate。
- `.ba2` 的 inventory 和 materialization 路由都固定为 `ba2-archive-audit`。该 Skill 可以复用共享只读归档解析脚本，但不得把 BA2 请求转交给 `bsa-archive-audit`。
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
