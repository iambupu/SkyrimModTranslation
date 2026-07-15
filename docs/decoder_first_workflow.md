# Decoder-First Workflow

## 目标

Decoder-first 是所有主控 agent 共享的非 GUI 执行原则：先解析当前 Game Profile 的资源 capability，再从 Adapter Registry 取得受控入口；只有没有可用 decoder、格式不受支持，或必须通过桌面工具写回工作区内副本时，Codex 才进入 GUI 后备。

本页只维护架构、顺序和文档分工，不维护各工具的完整参数、二进制写回步骤、归档协议或最终交付门禁。

## 核心顺序

1. 从 `.skyrim-chs-workspace.json` 读取游戏身份并解析当前 Game Profile；不得根据 Mod 名、目录名或扩展名猜游戏。
2. 运行 `scripts/detect_decoder_tools.py`，确认工具配置、Python 库和受控 adapter 是否可用。
3. 使用 `mod-input-preparation` 扫描工作区 `mod/`，把 ZIP/7Z 或目录输入物化到 `work/extracted_mods/<ModName>/`。
4. 所有资源先经过 `translation-task-router`，由 capability 决定 inventory、read、write 或 strict-complete 是否允许。
5. decoder 或受控 adapter 只生成工作区内文本中间件、工具输出和 QA 证据；Agent 不直接修改二进制。
6. 译文经过机械校验和 Agent 模型校对后，才能进入受控写回、`final_mod` 组装和严格 QA。
7. `final_mod` 保持当前 Game Profile 的 Data 根结构，最终状态只由 workflow state、严格 QA 和 provenance 证据决定。

## 工具优先级

| 资源 | 首选路径 | 后备或阻断 |
|---|---|---|
| ESP/ESM/ESL | capability 选择的 CLI/库 adapter | Codex 可按路由进入 LexTranslator/xTranslator GUI；非 Codex 主控记录 `handoff_target=codex` |
| PEX | PEX capability 选择的导出/写回 adapter | LexTranslator/xTranslator PapyrusPex 仅作 Codex GUI 后备 |
| Interface、MCM、JSON/XML/CSV/TXT | Agent 文本管线 | 结构或编码不受支持时阻断并保留报告 |
| BSA | 只读 inventory；必要时使用受控 BSA wrapper materialization | 默认不重打包，使用同路径 loose override |
| BA2 | 独立 BA2 inventory/materialization 协议 | receipt、manifest、hash 或 capability 不满足时阻断；不重打包 |
| ZIP/7Z | Python 标准库或 `py7zr` | 7Z 可回退 `DecoderTools.Archive7zPath`；RAR 默认只给提取建议 |

`support_level` 只用于说明，不授权具体操作。执行和严格 QA 始终以资源 capability、Adapter Registry、AdapterResult 和当前证据为准。

## 配置入口

本机路径只写入未跟踪的 `config/tools.local.json`。字段和占位示例以 `config/tools.example.json` 为准；配置校验、GUI 启动开关和 adapter 清单统一见 [Tool Adapter](./tool_adapter.md)。本页不复制完整 JSON 配置，避免字段变更后出现第二份过期示例。

## 状态与恢复

常规运行由 workflow state 给出结构化 `next_actions`。主控只执行当前阶段授权的 Python 入口；执行后按项目刷新链更新 readiness、state、tasks、handoff、progress card 和 trace。

用户进度只读取 `.workflow/progress_card.md`。`traces/latest.jsonl` 和 `traces/trace_summary.md` 只用于开发者排查，不能替代 QA 或阶段完成证据。失败恢复使用 `workflow-agent-orchestration`，正常并发 lane 使用 `workflow-subagent-orchestration`。

## 二进制边界

- Agent 不直接修改 ESP/ESM/ESL、PEX、BSA 或 BA2。
- 受控 adapter 只能读取工作区允许的输入，并把二进制副本写到 `translated/tool_outputs/<ModName>/` 或 `out/<ModName>/tool_outputs/`。
- 每次受控操作必须记录 adapter identity、operation、输入 hash、输出 hash、报告和标准 AdapterResult。
- GUI 只属于 Codex；opencode 和 Claude Code 遇到 GUI-only 动作必须停止并交回 Codex。

ESP/ESM/ESL 的字段范围、译表格式和写回规则见 [ESP/ESM/ESL Translation Skill](../skills/esp-esm-esl-translation/SKILL.md) 与 [Tool Adapter](./tool_adapter.md)。PEX 的操作步骤只在 [PEX Visible Strings Writeback](./pex_visible_strings_writeback.md) 维护。

## 归档边界

BSA inventory/materialization 由 `bsa-archive-audit` 编排。BSA wrapper 的调用方必须传入 `--adapter-result-path`，让 extraction-backed manifest、files JSONL、QA 报告和 AdapterResult 在同一次受控提取中生成。单独运行 `new_archive_audit_manifest.py` 不能单独建立 strict completion 所需的 AdapterResult lineage。

BA2 inventory/materialization 都由 `ba2-archive-audit` 编排。BA2 使用独立 receipt、manifest、预发布 payload snapshot、源归档 hash 和验证链；不得转交 BSA Skill，也不能用当前提取目录自证成功。

BSA 和 BA2 都保持源归档不变。已翻译资源按归档内原始相对路径生成 loose override，再由 `final_mod` 直接替换路径承载。详细协议和命令只在对应归档 Skill、[Tool Adapter](./tool_adapter.md) 和 [Final Mod Output](./final_mod_output.md) 维护。

## QA 与交付

译文校对、模型报告时效、final text/binary review packet 和 strict-complete 合同统一见 [Translation Proofreading Workflow](./translation_proofreading_workflow.md)。`final_mod`、provenance、translation text dictionary 和 `_CHS.zip` 的目录及验证命令统一见 [Final Mod Output](./final_mod_output.md)。

decoder-first 成功只表示已获得可审计的工作区产物，不表示汉化完成。缺少候选覆盖、模型校对、二进制反读、归档 loose override、provenance 或严格 QA 证据时，workflow state 仍必须保持 blocked、qa_failed 或 qa_pending_strict。

## 文档分工

| 内容 | 权威文档 |
|---|---|
| capability、Adapter Registry、工具配置和 GUI fallback 条件 | [Tool Adapter](./tool_adapter.md) |
| PEX 导出、翻译、受控写回与验证 | [PEX Visible Strings Writeback](./pex_visible_strings_writeback.md) |
| 校对、模型审查和严格门禁 | [Translation Proofreading Workflow](./translation_proofreading_workflow.md) |
| final_mod、provenance、词典和 CHS 包 | [Final Mod Output](./final_mod_output.md) |
| Fallout 4 能力限制 | [Fallout 4 Experimental Support](./fallout4_experimental_support.md) |
| BSA/BA2 的具体编排 | `bsa-archive-audit`、`ba2-archive-audit` Skills |
