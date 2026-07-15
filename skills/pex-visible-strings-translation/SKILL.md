---
name: pex-visible-strings-translation
description: "用于按 Game Profile 处理 Papyrus PEX 可见字符串、Export/Apply 边界和 PSC 只读提取。中文触发：PEX、Papyrus、PEX 脚本字符串、MessageBox、PEX 中的 MCM 文本、PEX 导出/写回、CMP 保护。Skyrim follows the certified adapter path; Fallout 4 Export is available and experimental Apply may create a verified workspace copy, but strict completion always blocks pending manual in-game testing. Do not edit PEX bytes, rewrite/compile PSC, or translate logic keys."
---

# PEX Visible Strings Translation Rules

## 目标

只定义 PEX 可见字符串的可翻译范围、禁止范围、译文风格和 QA 要求。本 Skill 不选择 LexTranslator/xTranslator 优先级，不描述 GUI 操作步骤，不直接修改 `.pex`。

## 全局硬约束

- 继承 `translation-task-router` 的 Windows、工作区路径、`mod/` 输入和真实游戏目录隔离合同；本节只补充 PEX 限制。
- 游戏身份和 `pex_category` 只取工作区 marker/Game Profile，不按 Mod 名猜测。
- 先解析当前 Profile 的 `pex` capability：Export 需要 `read`，Apply 需要 `write`，严格完成按实际使用的读/写操作判断。不满足时路由为 blocked，不调用 PEX 工具；不使用 Papyrus 的后续游戏只需把该 capability 设为 `unsupported`，省略 `adapter` 和 `options`，不得伪造 `pex_category=none` 或借用 Skyrim/Fallout 4 category。
- Codex 不直接修改 `.pex`，不反编译后回写，不编译 `.psc`。
- PEX 二进制输出只能由明确支持 PEX 重写的受控 CLI 工具或 LexTranslator/xTranslator 写入项目内 PEX 副本。非 GUI 路径必须先解析 `capabilities.pex`，再从 Adapter Registry 取得当前 adapter 的 Export/Apply 入口；不得把固定 Mutagen 脚本当作所有游戏的通用入口。

## 优先前置

1. 如果存在 `Interface/translations/*.txt`，优先处理这些文本，不碰 PEX；这些 Interface 文本进入交付态时必须由 `text-resource-translation`/`qa-validation` 确认为 UTF-16 LE BOM 且通过 final Interface runtime 审计。
2. 如果只有 PEX 中有玩家可见文本，先通过 Adapter Registry 解析出的 Export 入口导出 PEX 函数指令中的 `VariableType.String` 字符串；必要时对 `.psc` 做只读字符串提取供人工确认。只有 Registry 返回当前内置 `mutagen-pex` adapter 时，才使用下文的 `invoke_mutagen_pex_string_tool.py` 示例。
3. 已确认的可见字符串才能进入翻译准备文件。
4. 写回阶段必须由 `translation-task-router` 选择工具；本 Skill 不维护工具优先级。
5. 完整非 GUI 总控只在当前 profile 允许 Apply 时，才在 `build_final_mod.py` 前执行 PEX Apply + `verify_pex_output.py`。未知 category 或未实现的 PEX adapter 必须 blocked。Skyrim 沿用认证路径；Fallout 4 Export 可用，Apply 只有显式 `experimental opt-in` 后才能生成工作区实验副本并留下 Apply/反读证据，但 strict completion 固定阻断，必须人工游戏内测试，不能由任何静态证据解除。通过验证的输出放入 `out/<ModName>/tool_outputs/`；GUI/后备输出可位于 `translated/tool_outputs/<ModName>/`，两类根目录都要经过 `audit_pex_delivery.py`。

## 可翻译内容

- `Debug.Notification` 等玩家可见通知。
- `MessageBox`、确认提示、菜单提示。
- MCM 显示名、选项说明、帮助文本。
- 明确展示给玩家的状态说明。

## 模型翻译要求

- PEX 译文必须由 agent 模型翻译或复核，尤其要检查拼接片段组合后的显示效果。
- 脚本只能导出 `VariableType.String`、套用译表、写回项目内 PEX 副本和验证字节/解析状态。
- 模型校对必须确认没有翻译函数名、变量名、属性名、状态名、事件名、StorageUtil key、JsonUtil key 或比较用字符串。
- 写回前优先生成 `qa/<ModName>.model_review_packet.md`，由 agent 模型填写 `qa/<ModName>.model_review.md`。

## 禁止翻译

- 函数名、变量名、属性名、状态名、事件名。
- ModEvent 名、StorageUtil key、JsonUtil key。
- page id、option id、state id、数组索引、字典 key。
- 任何可能参与 `if`、`switch`、比较、查表或脚本逻辑判断的字符串。
- PEX 导出行中 `opcode` 为 `CMP_*` 的字符串；典型风险是 MCM `OnPageReset(Page)` 用页面标题做等值比较，误翻后会导致 MCM 右侧页面为空。
- 不确定用途的字符串。

## 输出要求

- 可见字符串中间文件写入 `work/normalized/<ModName>/pex_visible_strings.jsonl`。
- Decoder 准备/导出文件写入 `source/pex_exports/<ModName>/` 或 `work/normalized/<ModName>/`。
- LexTranslator 准备文件写入 `translated/lextranslator_ready/<ModName>/`。
- xTranslator 准备文件写入 `translated/xtranslator_ready/<ModName>/`。
- PSC 只读提取结果写入 `work/psc_strings/<ModName>/` 和 `qa/psc_string_review.md`。
- 工具写回目标只能是 `out/<ModName>/tool_outputs/Scripts/*.pex` 或 `translated/tool_outputs/<ModName>/Scripts/*.pex`。
- 无 GUI 写回使用 Adapter Registry 为当前 `pex` capability 返回的 Apply 入口。当前内置 `mutagen-pex` 映射到 `python scripts/invoke_mutagen_pex_string_tool.py --mode Apply`；输入 PEX 必须来自 `work/extracted_mods/`，译表必须来自 `translated/` 或 `work/normalized/`，输出 PEX 必须进入 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
- `scripts/run_non_gui_translation_workflow.py` 会按 PEX 文件名或 PSC `source_file` stem 自动匹配译表行，生成 `work/normalized/<ModName>/pex_apply/<Script>.translation.jsonl`，再写入 `out/<ModName>/tool_outputs/<相对路径>.pex`。
- 如果 PEX 文件存在但没有可用译表，总控会先用受控 PEX Export 生成 `work/normalized/<ModName>/pex_visible_strings/<Script>.translation.template.jsonl`；Codex 填写或复核后应另存为同目录 `<Script>.translation.jsonl`，总控会在下一次运行时自动收集。
- `scripts/prepare_pex_tool_output.py` 只用于 GUI fallback 前创建工作区内副本；Mutagen PEX `Apply` 可以直接从 `work/extracted_mods/` 写出工作区内工具输出。
- `PexStringToolPath` 不可用或 QA 失败时，才允许进入 LexTranslator/xTranslator GUI fallback。

## 当前内置 mutagen-pex 示例

以下固定命令只适用于 Adapter Registry 已把当前 Profile 的 `capabilities.pex.adapter` 解析为 `mutagen-pex` 的情况。若 Registry 返回其他 adapter id，必须使用该 adapter 注册的入口；不得沿用这些命令或跨游戏回退到 Mutagen。

导出：

```powershell
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Export --input-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --output-jsonl-path "source\pex_exports\<ModName>\<Script>.pex_strings.jsonl" --report-path "qa\<Script>.pex_export_report.md"
```

写回：

```powershell
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Apply --input-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --translation-jsonl-path "translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --output-pex-path "out\<ModName>\tool_outputs\Scripts\<Script>.pex" --report-path "qa\<Script>.mutagen_pex_write.md"
```

CLI 只接受函数指令参数里的 `VariableType.String` 作为可写回候选。写回时受控适配器会补丁 PEX 全局字符串表中的匹配源文本，并立即反读输出 PEX 确认可解析；因此同一源文本如果也出现在非指令元数据或 `CMP_*` 比较指令中，必须整条跳过，避免字符串表补丁误改对象名、函数名、变量名、属性名、状态名、user flag、source file name、debug symbol 或逻辑比较文本。

CLI 不会替换 `VariableType.Identifier`，不会反编译/重编译 PSC，也不会把输出写回 `mod/` 原始输入。Agent 只能调用该受控适配器生成工作区内 PEX 副本。

## PSC 只读辅助

- `.psc` 只用于判断字符串上下文。
- 记录文件名、行号、附近函数/事件、风险判断。
- 不自动回写源码。
- 不自动编译。
- 不翻译内部 key 或逻辑判断字符串。

## QA 要求

- 生成 `qa/pex_risk_report.md` 或 `qa/pex_tool_writeback.md`。
- 每条高风险或不确定字符串保留原文并说明原因。
- 校验占位符、标签、换行和控制符。
- 已完成 agent 模型校对，特别是 PEX 拼接片段、上下文和误翻风险。
- 写回后的 PEX 必须运行当前 adapter 合同指定的 verify 入口，报告固定写入 `qa/<ModName>.<Script>.pex_output_verification.md`，并用 Adapter Registry 返回的 Export 入口反读一次确认仍可解析。当前内置 `mutagen-pex` 才使用 `python scripts/invoke_mutagen_pex_string_tool.py --mode Export`。
- `scripts/run_non_gui_translation_workflow.py`、`scripts/run_non_gui_qa_gates.py` 和 `scripts/verify_pex_output.py` 必须跳过 protected、空 target、source 等于 target、以及 `CMP_*` 比较指令中的 PEX 译表行，避免把逻辑字符串写入 PEX。
- `verify_pex_output.py` 必须要求完整 target 字符串在输出 PEX 中出现；如果源文已消失但只找到中文片段、完整 target 未命中，必须视为阻断问题，不能降级为 warning。
- PEX 进入 final_mod 后，必须运行 `scripts/new_final_binary_review_packet.py` 反读实际交付脚本文本；`protected-logic` 或疑似逻辑 key 变化必须阻断或由模型明确解释。
- 写回后的 PEX 仍需要人工抽查和游戏内测试。
- Fallout 4 experimental Apply 即使通过工作区反读验证，也只能作为人工游戏内测试候选；strict completion 必须保持 blocked。

## 完成标准

- 已优先检查 `Interface/translations/*.txt`，避免无必要触碰 PEX。
- 如果因存在 `Interface/translations/*.txt` 而不处理 PEX 中重复 MCM 文本，必须确认对应 final Interface 文件通过 `qa/<ModName>.final_interface_runtime.md`，不能只因找到 Interface 文件就跳过运行时可加载性验证。
- 可见字符串、逻辑 key 和不确定字符串已分开记录。
- PSC 只读候选如有提取，已写入 `work/psc_strings/<ModName>/` 和 `qa/psc_string_review.md`。
- PEX decoder 导出或工具准备文件已写入项目内 `source/`、`work/`、`translated/lextranslator_ready/` 或 `translated/xtranslator_ready/`。
- 写回目标只指向项目内 PEX 副本，并已生成 `qa/pex_risk_report.md`、`qa/pex_tool_writeback.md` 或 `qa/<Script>.mutagen_pex_write*.md`。
- 如果存在可匹配 PEX 译表，`out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/` 中必须已有对应 `.pex`，`qa/<ModName>.<Script>.pex_output_verification.md` 显示完整 target 命中、无 blocking issues，且 `qa/<ModName>.pex_delivery_post_build.md` 证明 final_mod 中同路径 PEX 与该 tool_outputs 来源 SHA256 一致。
- final_mod 中的 PEX 已被反读进 `qa/<ModName>.final_binary_review_packet.md`，并由 agent 模型校对实际交付文本。

## 失败处理

无法判断是否玩家可见时不翻译；无法可靠解析 PSC 时输出原始候选并标记高风险；decoder 或 GUI 自动化失败时标记工具阶段未完成，不得由 Codex 直接改写 PEX。
