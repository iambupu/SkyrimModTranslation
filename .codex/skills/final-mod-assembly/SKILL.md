---
name: final-mod-assembly
description: Use when assembling direct-replacement Skyrim Mod output under out/{ModName}/汉化产出/final_mod, consuming verified tool_outputs, mirroring intermediate outputs, and creating out/{ModName}/汉化产出/{ModName}_CHS.zip from project-local source and translated overlays. Do not use for translation, GUI automation, or editing binaries.
---

# Final Mod Assembly

## 目标

只负责生成 `out/<ModName>/汉化产出/final_mod/`、`out/<ModName>/汉化产出/intermediate/` 和 `out/<ModName>/汉化产出/<ModName>_CHS.zip`。`final_mod/` 保持 Skyrim Mod Data 根结构，方便人工检查、本地安装测试和打包交付。本 Skill 不翻译文本，不选择工具，不判断字符串质量。

最终交付默认是直接替换：先复制原 Mod 文件，再用项目内翻译 overlay 或 tool_outputs 按相同相对路径覆盖 `final_mod` 中的对应原文件。旁挂语言补丁文件只作为中间件，不能替代同路径同名覆盖。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入输出路径必须在当前项目内。
- 不访问真实 Skyrim 游戏目录或真实 MO2/Vortex 目录。
- 不直接修改插件或 PEX 二进制。
- 不修改 `mod/` 原始文件。

## 输入

- 标准来源：`work/extracted_mods/<ModName>/`。
- 兼容来源：项目内 `mod/` 沙盒目录或项目内 `.zip` 解压结果。
- 文本翻译暂存 overlay：优先使用 `translated/final_mod/<ModName>/` 或 `translated/overlay/<ModName>/`；兼容读取 `out/<ModName>/final_mod_overlay/` 作为旧暂存输入。所有 overlay 必须保持 Data 根相对路径和原文件名；这些目录只是 final_mod 组装输入，不是最终交付目录。
- 工具输出：`out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
- 补丁输出：`out/<ModName>/dsd_patch/`。

`out/<ModName>/tool_outputs/` 是受控 ESP/PEX 工具写回的正式覆盖来源。若上游译表或 QA 证明某个插件/PEX 应被写回，但对应 tool_outputs 文件不存在，组装阶段不得把原始二进制复制进 final_mod 后宣称完整；必须让 QA/总控阶段阻断。

## 输出

- `out/<ModName>/汉化产出/final_mod/`
- `out/<ModName>/汉化产出/final_mod/meta/manifest.json`
- `out/<ModName>/汉化产出/final_mod/meta/build_report.md`
- `out/<ModName>/汉化产出/final_mod/meta/source_files.md`
- `out/<ModName>/汉化产出/final_mod/meta/redistribution_notes.md`
- `out/<ModName>/汉化产出/intermediate/`
- `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/translation_dictionary.jsonl`
- `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/translation_dictionary.md`
- `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/manifest.json`
- `out/<ModName>/汉化产出/<ModName>_CHS.zip`
- `out/<ModName>/汉化产出/package_report.md`
- `qa/final_mod_validation.md`

## 推荐工具

- `scripts/build_final_mod.py`
- `scripts/validate_final_mod.py`
- `scripts/validate_chs_package.py`
- `scripts/validate_final_text_structure.py`
- `scripts/new_final_text_review_packet.py`
- `scripts/clean_final_mod.py`

## 具体流程

1. 校验 `SourceModDir` 在项目内。
2. 优先使用 `work/extracted_mods/<ModName>/` 作为来源。
3. 原样复制来源目录中的资产和二进制到 `out/<ModName>/汉化产出/final_mod/`。
4. 从 `translated/final_mod/<ModName>/`、`translated/overlay/<ModName>/`、兼容暂存 `out/<ModName>/final_mod_overlay/`、`out/<ModName>/tool_outputs/` 和 `translated/tool_outputs/<ModName>/` 按原相对路径覆盖项目内翻译输出；`final_mod_overlay/` 只作为旧暂存 overlay 输入，`tool_outputs/` 中的同路径插件/PEX 输出优先覆盖原始二进制副本。
5. 将替换原文件的 overlay 记录为 `ReplacementFilesApplied`；将新增路径记录为 `AddedOverlayFiles`。
6. 跳过 `.bak`、`.backup`、`.old`、`.tmp`、`*.esp.*` 和压缩包等历史备份或残留。
7. 生成 manifest、source_files、build_report 和 redistribution_notes。
8. 生成 `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/`，汇总插件、PEX、xTranslator、LexTranslator 等 JSONL/XML 译表为 `source -> target` 统一翻译文本词典；按译文条目保留上下文，不因重复文本丢行，并镜像原始词典来源到 `raw_sources/`。
9. 汇总 `out/<ModName>/tool_outputs/`、`final_mod_overlay/`、`xtranslator_import/`、`dsd_patch/`、`lex_dictionary/`、`archive_audits/`、`qa/` 到 `out/<ModName>/汉化产出/intermediate/`；这里的 `final_mod_overlay/` 是被镜像的中间来源，不是最终输出。
10. 从 `final_mod/` 生成 `out/<ModName>/汉化产出/<ModName>_CHS.zip`，包名必须带 `_CHS` 后缀。
11. 运行 `python scripts/validate_chs_package.py`，确认 `_CHS.zip` 与 `final_mod/` 的文件路径、文件数量和 SHA256 完全一致。
12. 运行 final_mod 校验、final_mod 文本结构校验和 final_mod 交付态文本模型校对包生成。

## 禁止事项

- 不访问真实游戏目录或真实 Mod 管理器目录。
- 不把工程目录 `source/work/qa/scripts/docs/glossary/config/skills` 混入 final_mod。
- 不把 `.zip/.rar/.7z` 残留放进 final_mod。
- 只允许在项目内生成 `<ModName>_CHS.zip`；不安装、不复制到真实 Mod 管理器目录。
- 不自动安装到 MO2/Vortex。
- 不默认声明 final_mod 可公开再分发。

## QA 检查

- final_mod 存在。
- Data 根结构合理。
- 无 `Data/Data` 或 `mod/mod` 嵌套。
- manifest 记录复制、覆盖和二进制原样复制。
- manifest 记录 `DeliveryMode = direct-replacement-final-mod`、`ReplacementFilesApplied` 和 `AddedOverlayFiles`。
- manifest 记录 `OutputLayout = mod-root/localization-output/final_mod-intermediate-package`、`IntermediateOutputDir`、`PackagedModPath` 和 `PackagedModNameSuffix = CHS`。
- manifest 记录 `TranslationDictionaryEntryCount`，且 `intermediate/translation_text_dictionary/manifest.json` 中 `TranslatedEntryCount` 大于 0。
- `_CHS.zip` 必须逐文件匹配 `final_mod/`；不得出现 final_mod 缺失、包内多余或 SHA256 不一致。
- 如果存在已匹配译表的 ESP/PEX，manifest/build report 必须能显示对应 `tool_outputs/` 覆盖已应用；否则 strict QA 应阻断。
- `Interface/translations/*_chinese.txt`、`*_zh*.txt` 等旁挂语言文件不得作为新增 overlay 进入 final_mod；除非 QA 有明确加载依据，否则必须改成覆盖原加载文件名。
- 同路径替换后的 JSON key、XML tag/attribute name、INI section/key、CSV header、Interface key/tab/行数必须保持；PSC 源码必须保持只读原样。
- meta 中包含来源文件和再分发权限说明。

## 完成标准

- `out/<ModName>/汉化产出/final_mod/` 已生成并保持 Skyrim Data 根结构。
- `out/<ModName>/汉化产出/intermediate/` 已生成。
- `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/` 已生成，并包含非空的 `translation_dictionary.jsonl`、人工预览 `translation_dictionary.md`、来源镜像 `raw_sources/` 和 `manifest.json`。
- `out/<ModName>/汉化产出/<ModName>_CHS.zip` 已生成，包名带 `_CHS` 后缀。
- `qa/<ModName>.chs_package_validation.md` 已生成并证明 `_CHS.zip` 与 `final_mod/` 完全一致。
- `meta/manifest.json`、`meta/build_report.md`、`meta/source_files.md` 和 `meta/redistribution_notes.md` 已生成。
- 二进制文件只从项目内来源原样复制，未被 Codex 修改。
- 翻译 overlay 和工具输出的直接替换记录已写入 build report；应写回的插件/PEX 不得缺少对应 tool_outputs 覆盖记录。
- `python scripts/validate_final_mod.py` 已运行，结果写入 `qa/final_mod_validation.md`。
- `python scripts/validate_final_text_structure.py` 已运行，结果写入 `qa/<ModName>.final_text_structure.md`。
- `scripts/new_final_text_review_packet.py` 已运行，结果写入 `qa/<ModName>.final_text_review_packet.md` 和 `qa/<ModName>.final_text_review_items.jsonl`。
- `qa/final_mod_validation.md` 显示 `Language sidecar overlays: 0`。

## 失败处理

路径不安全、输出目录冲突、overlay 缺失、工具输出不可信或 final_mod 校验失败时停止，并写入 QA 报告。
