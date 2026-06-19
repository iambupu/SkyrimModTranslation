---
name: pex-visible-strings-translation
description: Use for Skyrim Papyrus PEX visible-string rules, Mutagen PEX Export/Apply outputs, automatic pre-final_mod PEX writeback checks, PSC read-only string extraction, MCM notifications, MessageBox, and risk QA. Do not use to direct-edit PEX bytes, rewrite PSC, compile scripts, or translate logic keys.
---

# PEX Visible Strings Translation Rules

## 目标

只定义 PEX 可见字符串的可翻译范围、禁止范围、译文风格和 QA 要求。本 Skill 不选择 LexTranslator/xTranslator 优先级，不描述 GUI 操作步骤，不直接修改 `.pex`。

## 全局硬约束

- Windows 10；可复用流程入口统一为 Python 脚本；不得新增 shell 包装层；禁止 Bash/WSL/Linux 命令。
- 输入输出路径必须在当前项目内。
- Mod 原始输入只允许来自当前项目 `mod/` 沙盒或项目内工作副本。
- 不访问真实 Skyrim 游戏目录或真实 MO2/Vortex 目录。
- Codex 不直接修改 `.pex`，不反编译后回写，不编译 `.psc`。
- PEX 二进制输出只能由明确支持 PEX 重写的受控 CLI 工具或 LexTranslator/xTranslator 写入项目内 PEX 副本；当前首选 Python 入口是 `scripts/invoke_mutagen_pex_string_tool.py`。

## 优先前置

1. 如果存在 `Interface/translations/*.txt`，优先处理这些文本，不碰 PEX；这些 Interface 文本进入交付态时必须由 `text-resource-translation`/`qa-validation` 确认为 UTF-16 LE BOM 且通过 final Interface runtime 审计。
2. 如果只有 PEX 中有玩家可见文本，先用 `python scripts/invoke_mutagen_pex_string_tool.py --mode Export` 导出 PEX 函数指令中的 `VariableType.String` 字符串；必要时对 `.psc` 做只读字符串提取供人工确认。
3. 已确认的可见字符串才能进入翻译准备文件。
4. 写回阶段必须由 `translation-task-router` 选择工具；本 Skill 不维护工具优先级。
5. 完整非 GUI 总控默认在 `build_final_mod.py` 前执行 PEX Apply + `verify_pex_output.py`，把确认可写回的 PEX 输出放入 `out/<ModName>/tool_outputs/` 后再组装 final_mod；GUI/后备写回允许使用 `translated/tool_outputs/<ModName>/`，两种受控 tool_outputs 根目录都必须在 `build_final_mod.py` 前后通过 `audit_pex_delivery.py` 校验。

## 可翻译内容

- `Debug.Notification` 等玩家可见通知。
- `MessageBox`、确认提示、菜单提示。
- MCM 显示名、选项说明、帮助文本。
- 明确展示给玩家的状态说明。

## 模型翻译要求

- PEX 译文必须由 Codex 模型翻译或复核，尤其要检查拼接片段组合后的显示效果。
- 脚本只能导出 `VariableType.String`、套用译表、写回项目内 PEX 副本和验证字节/解析状态。
- 模型校对必须确认没有翻译函数名、变量名、属性名、状态名、事件名、StorageUtil key、JsonUtil key 或比较用字符串。
- 写回前优先生成 `qa/<ModName>.model_review_packet.md`，由 Codex 模型填写 `qa/<ModName>.model_review.md`。

## 禁止翻译

- 函数名、变量名、属性名、状态名、事件名。
- ModEvent 名、StorageUtil key、JsonUtil key。
- page id、option id、state id、数组索引、字典 key。
- 任何可能参与 `if`、`switch`、比较、查表或脚本逻辑判断的字符串。
- 不确定用途的字符串。

## 输出要求

- 可见字符串中间文件写入 `work/normalized/<ModName>/pex_visible_strings.jsonl`。
- Decoder 准备/导出文件写入 `source/pex_exports/<ModName>/` 或 `work/normalized/<ModName>/`。
- LexTranslator 准备文件写入 `translated/lextranslator_ready/<ModName>/`。
- xTranslator 准备文件写入 `translated/xtranslator_ready/<ModName>/`。
- PSC 只读提取结果写入 `work/psc_strings/<ModName>/` 和 `qa/psc_string_review.md`。
- 工具写回目标只能是 `out/<ModName>/tool_outputs/Scripts/*.pex` 或 `translated/tool_outputs/<ModName>/Scripts/*.pex`。
- 无 GUI 写回使用 `python scripts/invoke_mutagen_pex_string_tool.py --mode Apply`，输入 PEX 必须来自 `work/extracted_mods/`，译表必须来自 `translated/` 或 `work/normalized/`，输出 PEX 必须进入 `out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/`。
- `scripts/run_non_gui_translation_workflow.py` 会按 PEX 文件名或 PSC `source_file` stem 自动匹配译表行，生成 `work/normalized/<ModName>/pex_apply/<Script>.translation.jsonl`，再写入 `out/<ModName>/tool_outputs/<相对路径>.pex`。
- `scripts/prepare_pex_tool_output.py` 只用于 GUI fallback 前创建项目内副本；Mutagen PEX `Apply` 可以直接从 `work/extracted_mods/` 写出项目内工具输出。
- `PexStringToolPath` 不可用或 QA 失败时，才允许进入 LexTranslator/xTranslator GUI fallback。

## Mutagen PEX CLI 流程

导出：

```console
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Export --input-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --output-jsonl-path "source\pex_exports\<ModName>\<Script>.pex_strings.jsonl" --report-path "qa\<Script>.pex_export_report.md"
```

写回：

```console
python .\scripts\invoke_mutagen_pex_string_tool.py --mode Apply --input-pex-path "work\extracted_mods\<ModName>\Scripts\<Script>.pex" --translation-jsonl-path "translated\lextranslator_ready\<ModName>\<Script>_strings.jsonl" --output-pex-path "out\<ModName>\tool_outputs\Scripts\<Script>.pex" --report-path "qa\<Script>.mutagen_pex_write.md"
```

CLI 只会替换函数指令参数里的 `VariableType.String`。它不会替换 `VariableType.Identifier`，也不会修改对象名、函数名、变量名、属性名、状态名、user flag、source file name 或 debug symbol。

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
- 已完成 Codex 模型校对，特别是 PEX 拼接片段、上下文和误翻风险。
- 写回后的 PEX 必须运行 `python scripts/verify_pex_output.py`，报告固定写入 `qa/<ModName>.<Script>.pex_output_verification.md`，并用 `python scripts/invoke_mutagen_pex_string_tool.py --mode Export` 反读一次确认仍可解析。
- `verify_pex_output.py` 必须要求完整 target 字符串在输出 PEX 中出现；如果源文已消失但只找到中文片段、完整 target 未命中，必须视为阻断问题，不能降级为 warning。
- PEX 进入 final_mod 后，必须运行 `scripts/new_final_binary_review_packet.py` 反读实际交付脚本文本；`protected-logic` 或疑似逻辑 key 变化必须阻断或由模型明确解释。
- 写回后的 PEX 仍需要人工抽查和游戏内测试。

## 完成标准

- 已优先检查 `Interface/translations/*.txt`，避免无必要触碰 PEX。
- 如果因存在 `Interface/translations/*.txt` 而不处理 PEX 中重复 MCM 文本，必须确认对应 final Interface 文件通过 `qa/<ModName>.final_interface_runtime.md`，不能只因找到 Interface 文件就跳过运行时可加载性验证。
- 可见字符串、逻辑 key 和不确定字符串已分开记录。
- PSC 只读候选如有提取，已写入 `work/psc_strings/<ModName>/` 和 `qa/psc_string_review.md`。
- PEX decoder 导出或工具准备文件已写入项目内 `source/`、`work/`、`translated/lextranslator_ready/` 或 `translated/xtranslator_ready/`。
- 写回目标只指向项目内 PEX 副本，并已生成 `qa/pex_risk_report.md`、`qa/pex_tool_writeback.md` 或 `qa/<Script>.mutagen_pex_write*.md`。
- 如果存在可匹配 PEX 译表，`out/<ModName>/tool_outputs/` 或 `translated/tool_outputs/<ModName>/` 中必须已有对应 `.pex`，`qa/<ModName>.<Script>.pex_output_verification.md` 显示完整 target 命中、无 blocking issues，且 `qa/<ModName>.pex_delivery_post_build.md` 证明 final_mod 中同路径 PEX 与该 tool_outputs 来源 SHA256 一致。
- final_mod 中的 PEX 已被反读进 `qa/<ModName>.final_binary_review_packet.md`，并由 Codex 模型校对实际交付文本。

## 失败处理

无法判断是否玩家可见时不翻译；无法可靠解析 PSC 时输出原始候选并标记高风险；decoder 或 GUI 自动化失败时标记工具阶段未完成，不得由 Codex 直接改写 PEX。
