---
name: final-mod-assembly
description: "用于状态机授权交付后，按当前 Game Profile 和规模策略组装 final_mod、provenance 和 _CHS.zip。中文触发：状态机已授权交付、组装 final_mod、汉化覆盖包、L5 聚合、打包汉化产出、复制 tool_outputs、BSA/BA2 loose override。Use after delivery is authorized for complete or translation-overlay output under out/{ModName}/汉化产出/, and aggregate validated L5 child overlays. Only copy verified workspace-local binaries unchanged or from controlled tool_outputs. Do not translate, operate GUI, repack archives, or edit binaries."
---

# Final Mod Assembly

## 目标

只负责生成 `out/<ModName>/汉化产出/final_mod/`、`out/<ModName>/汉化产出/intermediate/` 和 `out/<ModName>/汉化产出/<ModName>_CHS.zip`。`final_mod/` 保持当前 Game Profile 的 Data 根结构，方便人工检查、本地安装测试和打包交付。本 Skill 不翻译文本，不选择工具，不判断字符串质量。

交付模式由 `qa/<ModName>.scale_execution.json` 决定：L0-L2 使用 `direct-replacement-final-mod`，先复制原 Mod 再按同路径覆盖；L3/L4 使用 `translation-overlay-package`，只收录经过验证的翻译覆盖项，并声明必须配合原 Mod 使用。没有 scale execution 的旧工作区保持完整副本默认值。两种模式都必须覆盖原 Data 相对路径和原文件名，不能用不受支持的旁挂语言文件冒充交付。

L5 不在单工作区直接构建。先把 QA 通过的子项目按合同放入 `work/aggregate_inputs/<Project>/`，再由 `aggregate_translation_projects.py` 校验游戏、顺序、依赖、覆盖、provenance 和冲突后生成最终覆盖包。当前聚合合同只接受 `loose_text` provenance；插件、PEX 和字符串表的 adapter lineage 未迁移时必须阻断。

BSA/BA2 内资源完成汉化后也按同一路径规则交付：完整副本模式原样复制源归档，翻译覆盖模式只交付归档内原始相对路径的 loose override 并依赖原 Mod。BSA 默认不重打包；BA2 当前禁止重打包，且其来源必须有通过独立验证的 extraction receipt、manifest 和 entry hash。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入输出路径必须在当前工作区内。
- 不访问任何真实游戏目录或真实 MO2/Vortex 目录。
- 不直接修改插件或 PEX 二进制。
- 不修改 `mod/` 原始文件。

## 输入

- 标准来源：`work/extracted_mods/<ModName>/`。
- 兼容来源：工作区 `mod/` 沙盒目录或工作区 `.zip` 解压结果。
- 文本翻译暂存 overlay：优先使用 `translated/final_mod/<ModName>/` 或 `translated/overlay/<ModName>/`；兼容读取 `out/<ModName>/final_mod_overlay/` 作为旧暂存输入。所有 overlay 必须保持 Data 根相对路径和原文件名；这些目录只是 final_mod 组装输入，不是最终交付目录。
- BSA/BA2 内文本的译文 overlay：必须使用归档内原始相对路径和原文件名，作为 loose override 进入上述 overlay 目录；不能要求本 Skill 把它们写回归档。BA2 overlay 必须能追溯到 `ba2-archive-audit` 已验证的 receipt、manifest、entry hash 和源 BA2 hash。
- 工具输出：`out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
- 补丁输出：`out/<ModName>/dsd_patch/`。

受控 `tool_outputs` 目录是 ESP/PEX 工具写回的正式覆盖来源，包括 `out/<ModName>/tool_outputs/` 和 `translated/tool_outputs/<ModName>/`。若上游译表或 QA 证明某个插件/PEX 应被写回，但两处受控 tool_outputs 都没有对应文件，组装阶段不得把原始二进制复制进 final_mod 后宣称完整；必须让 QA/总控阶段阻断。

## 输出

- `out/<ModName>/汉化产出/final_mod/`
- `out/<ModName>/汉化产出/final_mod/meta/manifest.json`
- `out/<ModName>/汉化产出/final_mod/meta/build_report.md`
- `out/<ModName>/汉化产出/final_mod/meta/source_files.md`
- `out/<ModName>/汉化产出/final_mod/meta/provenance.jsonl`
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
- `scripts/aggregate_translation_projects.py`
- `scripts/validate_final_mod.py`
- `scripts/validate_chs_package.py`
- `scripts/audit_pex_delivery.py`
- `scripts/validate_final_text_structure.py`
- `scripts/new_final_text_review_packet.py`
- `scripts/clean_final_mod.py`

## 具体流程

1. 校验 `SourceModDir` 在当前工作区内。
2. 优先使用 `work/extracted_mods/<ModName>/` 作为来源。
3. 读取当前 scale execution。完整模式原样复制来源资产；覆盖模式不复制原 Mod 受保护资源，只选择能证明对应原路径或归档 entry 的翻译 overlay/tool_outputs。
4. 从 `translated/final_mod/<ModName>/`、`translated/overlay/<ModName>/`、兼容暂存 `out/<ModName>/final_mod_overlay/`、`out/<ModName>/tool_outputs/` 和 `translated/tool_outputs/<ModName>/` 按原相对路径覆盖工作区翻译输出；BSA/BA2 内资源的译文也走同路径 loose override。覆盖模式不得收录无法证明替换目标的新增文件。
5. 将替换原文件的 overlay 记录为 `ReplacementFilesApplied`；将新增路径记录为 `AddedOverlayFiles`。
6. 跳过 `.bak`、`.backup`、`.old`、`.tmp`、`*.esp.*` 和压缩包等历史备份或残留。
7. 生成 manifest、source_files、build_report、provenance.jsonl 和 redistribution_notes。
8. 生成 `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/`，汇总插件、PEX、xTranslator、LexTranslator 等 JSONL/XML 译表为 `source -> target` 统一翻译文本词典；按译文条目保留上下文，不因重复文本丢行，并镜像原始词典来源到 `raw_sources/`。
9. 汇总 `out/<ModName>/tool_outputs/`、`final_mod_overlay/`、`xtranslator_import/`、`dsd_patch/`、`lex_dictionary/`、`archive_audits/`、`qa/` 到 `out/<ModName>/汉化产出/intermediate/`；这里的 `final_mod_overlay/` 是被镜像的中间来源，不是最终输出。
10. L5 使用 `python scripts/aggregate_translation_projects.py --mod-name <ModName>`；其他级别从 `final_mod/` 生成 `<ModName>_CHS.zip`，包名必须带 `_CHS` 后缀。
11. 运行 `python scripts/validate_chs_package.py`，确认 `_CHS.zip` 与 `final_mod/` 的文件路径、文件数量和 SHA256 完全一致。
12. 运行 final_mod 校验、final_mod 文本结构校验、Interface runtime 审计和 final_mod 交付态文本模型校对包生成；build 与 audit 必须读取同一 GameContext encoding policy。当前两个 profile 均要求 UTF-16 LE BOM 和 `$key<TAB>value`；policy 未知或缺失必须阻断。

## 禁止事项

- 不访问真实游戏目录或真实 Mod 管理器目录。
- 不把工程目录 `source/work/qa/scripts/docs/glossary/config/skills` 混入 final_mod；工作区 `glossary/` 只是翻译参考状态，不是交付内容。
- 不把 `.zip/.rar/.7z` 残留放进 final_mod。
- 只允许在工作区内生成 `<ModName>_CHS.zip`；不安装、不复制到真实 Mod 管理器目录。
- 不自动安装到 MO2/Vortex。
- 不默认声明 final_mod 可公开再分发。
- 不重打包 BA2。BSA 也默认不重打包；如果人工测试证明 BSA loose override 不生效，应由总控/QA 标记为高风险 blocked，等待受控 BSA packer adapter。

## QA 检查

- final_mod 存在。
- Data 根结构合理。
- 无 `Data/Data` 或 `mod/mod` 嵌套。
- manifest 记录复制、覆盖和二进制原样复制。
- manifest 的 `DeliveryMode` 只能是 `direct-replacement-final-mod` 或 `translation-overlay-package`，并记录 `RequiresOriginalMod`、`IncludesOriginalFiles`、`ReplacementFilesApplied` 和 `AddedOverlayFiles`。
- 覆盖模式必须绑定当前 scale execution；L5 覆盖模式必须绑定通过的 aggregate manifest。完整模式必须包含原 Mod 文件，覆盖模式不得声称包含完整原 Mod。
- manifest 记录 `OutputLayout = mod-root/localization-output/final_mod-intermediate-package`、`IntermediateOutputDir`、`PackagedModPath` 和 `PackagedModNameSuffix = CHS`。
- manifest 记录 `ProvenancePath` 和 `ProvenanceEntryCount`；`meta/provenance.jsonl` 必须覆盖每个 final_mod 文件的直接来源、来源 SHA256、最终 SHA256、transform、tool、生成器和 QA 证据入口。
- manifest 记录 `TranslationDictionaryEntryCount`，且 `intermediate/translation_text_dictionary/manifest.json` 中 `TranslatedEntryCount` 大于 0。
- `_CHS.zip` 必须逐文件匹配 `final_mod/`；不得出现 final_mod 缺失、包内多余或 SHA256 不一致。
- 如果存在已匹配译表的 ESP/PEX，manifest/build report 必须能显示对应受控 `tool_outputs/` 覆盖已应用；PEX 还必须由 `qa/<ModName>.pex_delivery_post_build.md` 证明 final_mod 同路径文件与实际 tool_outputs 来源 SHA256 一致，否则 strict QA 应阻断。
- 如果存在来自 BSA/BA2 的已翻译文本资源，manifest/build report 必须显示同路径 loose override 已进入 `final_mod/`，且原归档只作为未修改二进制来源保留。BA2 provenance 还必须引用已验证的 extraction receipt/manifest、entry hash 和保持不变的源归档 hash；任一证据缺失都必须阻断。
- `Interface/translations/*_chinese.txt`、`*_zh*.txt` 等旁挂语言文件不得作为新增 overlay 进入 final_mod；除非 QA 有明确加载依据，否则必须改成覆盖原加载文件名。
- `final_mod/Interface/translations/*.txt` 必须通过 `qa/<ModName>.final_interface_runtime.md`：报告需记录 GameId 与 profile encoding policy；当前 Skyrim SE/AE 和 Fallout 4 均为 UTF-16 LE BOM，并要求可解码、非空、每行保持 `$key<TAB>value`。不得只用普通文本结构校验替代。
- 同路径替换后的 JSON key、XML tag/attribute name、INI section/key、CSV header、Interface key/tab/行数必须保持；PSC 源码必须保持只读原样。
- meta 中包含来源文件和再分发权限说明。

## 完成标准

- `out/<ModName>/汉化产出/final_mod/` 已生成并保持当前 Game Profile 的 Data 根结构；其内容符合 manifest 声明的完整副本或翻译覆盖模式。
- final_mod manifest 与 provenance 的 `game_id`、profile version、support level 和 adapter metadata 与工作区 marker 一致；不一致时不得打包或放行。
- `out/<ModName>/汉化产出/intermediate/` 已生成。
- `out/<ModName>/汉化产出/intermediate/translation_text_dictionary/` 已生成，并包含非空的 `translation_dictionary.jsonl`、人工预览 `translation_dictionary.md`、来源镜像 `raw_sources/` 和 `manifest.json`。
- `out/<ModName>/汉化产出/<ModName>_CHS.zip` 已生成，包名带 `_CHS` 后缀。
- `qa/<ModName>.chs_package_validation.md` 已生成并证明 `_CHS.zip` 与 `final_mod/` 完全一致。
- `meta/manifest.json`、`meta/build_report.md`、`meta/source_files.md`、`meta/provenance.jsonl` 和 `meta/redistribution_notes.md` 已生成。
- 二进制文件只从项目内来源原样复制，未被 Codex 修改。
- 翻译 overlay 和工具输出的直接替换记录已写入 build report；应写回的插件/PEX 不得缺少对应 tool_outputs 覆盖记录。
- `qa/final_mod_validation.md` 已确认 `Missing provenance rows: 0`、`Final file SHA256 mismatches: 0` 和 `Source SHA256 mismatches: 0`。
- `python scripts/validate_final_mod.py` 已运行，结果写入 `qa/final_mod_validation.md`。
- `python scripts/validate_final_text_structure.py` 已运行，结果写入 `qa/<ModName>.final_text_structure.md`。
- `python scripts/audit_final_interface_translations.py` 已运行，结果写入 `qa/<ModName>.final_interface_runtime.md`，且阻断和警告均为 0。
- `scripts/new_final_text_review_packet.py` 已运行，结果写入 `qa/<ModName>.final_text_review_packet.md` 和 `qa/<ModName>.final_text_review_items.jsonl`。
- `qa/final_mod_validation.md` 显示 `Language sidecar overlays: 0`。

## 失败处理

路径不安全、输出目录冲突、overlay 缺失、工具输出不可信或 final_mod 校验失败时停止，并写入 QA 报告。
