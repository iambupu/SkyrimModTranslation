---
name: translation-task-router
description: Use before any Skyrim Mod file processing to route paths by extension and role into the correct Skill, risk level, tool priority, and output directory. Do not use to translate strings, operate GUI tools, validate QA, or assemble final_mod.
---

# Translation Task Router

## 目标

作为权威路由层，为项目内文件确定：文件类型、风险等级、推荐 Skill、工具优先级、输出目录和 Codex 是否允许直接处理。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入路径和输出路径必须在当前项目内。
- Mod 原始输入只允许来自当前项目 `mod/` 沙盒；`source/`、`work/`、`translated/`、`out/` 只处理项目内派生产物。
- 不访问真实 Skyrim 游戏目录或真实 MO2/Vortex 目录。
- 不直接修改插件或 PEX 二进制。

## 职责边界

- 本 Skill 负责选择下游 Skill 和工具优先级。
- 本 Skill 不决定具体字符串是否应该翻译；该判断由文件类型 Skill 完成。
- 本 Skill 不描述 GUI 操作步骤；GUI Skill 只按本 Skill 的路由结果执行工具操作。
- Decoder/CLI 优先级高于 GUI 工具。LexTranslator/xTranslator 仅在 decoder 不可用、格式不支持或写回阶段必须使用 GUI 时作为后备。

## 路由规则

| 文件类型或路径 | 风险 | 文件类型 Skill | 主工具 | 后备/验证工具 | Codex 直接处理 |
|---|---|---|---|---|---|
| `Interface/translations/*.txt` | 低 | `text-resource-translation` | Codex Text Pipeline | LexTranslator | 是，写项目内译文副本 |
| MCM 文本 | 中 | `mcm-translation` | LexTranslator 或 Codex Text Pipeline | xTranslator | 只处理可见文本 |
| `.esp/.esm/.esl` | 高 | `esp-esm-esl-translation` | Decoder CLI/library pipeline | LexTranslator/xTranslator GUI fallback | 否，只能处理 decoder/工具导出文本 |
| `.pex` | 高 | `pex-visible-strings-translation` | configured `PexStringToolPath` decoder/rewriter, currently `scripts/invoke_mutagen_pex_string_tool.py` | LexTranslator/xTranslator PapyrusPex GUI fallback | 否，只能处理 decoder/工具导出的可见字符串 |
| `.psc` | 高 | `pex-visible-strings-translation` | Codex 只读提取 | 无 | 只读提取，不回写 |
| `Meshes/**/*.xml`、`Textures/**/*.xml`、`FaceGenData/**/*.xml` | 受保护 | `manual-review` | 原样复制 | final_mod 结构校验 | 否，不能自动翻译 |
| `.json/.jsonl/.xml/.csv/.txt/.md` | 低到中 | `text-resource-translation` | Codex Text Pipeline | 无 | 是，保留结构 |
| `.zip` | 中 | `mod-input-preparation` | 项目内只读解压 | 无 | 只解压到项目内工作副本 |
| `.bsa/.ba2/.rar/.7z` | 中 | `mod-input-preparation` | 配置的 CLI 解包器 | 提取计划 | 未配置时默认不解包 |

## 推荐工具

- `scripts/route_translation_task.py`

## 输出

- 控制台路由结果。
- `qa/routing_report.md`。

## QA 检查

- 每个输入路径都在当前项目内。
- 高风险文件默认不能由 Codex 直接处理。
- `.esp/.esm/.esl` 和 `.pex` 的主路径为 decoder/CLI pipeline；PEX 首选 `PexStringToolPath` 的 `Export`/`Apply`；LexTranslator/xTranslator 是 GUI fallback。
- 未知扩展名或路径不安全时路由为 `manual-review`。
- `Meshes/`、`Textures/`、`FaceGenData/` 下的 XML 是资源元数据，不是普通可见文本；路由必须阻止自动翻译，final_mod 中默认要求字节级不变。

## 完成标准

- 已为每个候选文件输出 Skill、风险等级、主工具、后备工具和是否允许 Codex 直接处理。
- `qa/routing_report.md` 已更新。
- 没有未知或高风险文件被静默放行。
- 下游执行只依据路由结果启动。

## 失败处理

未知扩展名、高风险路径、路径不在项目内或工具选择不明确时，路由为 `manual-review`，不得继续自动翻译。
