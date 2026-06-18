# LexTranslator Workflow

- LexTranslator 适合作为 GUI fallback 批量翻译工具。
- 当前项目已改为 decoder-first：ESP/ESM/ESL 和 PEX 先尝试 CLI/库 decoder；只有 decoder 不可用、格式不支持或必须用 GUI 写回项目内副本时，才使用 LexTranslator。
- 简体中文参考词典优先放入 `glossary/lextranslator_dynamic_dictionaries/`，由项目本地 RAG 索引动态加载；详见 `docs/lextranslator_dictionary_rag.md`。
- 建议先用小 Mod 测试。
- 批量翻译前先确认源语言和目标语言。
- 翻译后导出文本或保存前先抽样检查。
- 不要直接覆盖唯一原文件。
- 每个 Mod 保留：
  1. 原始导出
  2. 翻译中间文件
  3. 最终导入文件
  4. QA 记录
- 如果导出格式不确定，把样例放到 `source/lextranslator_exports/`，再让 Codex 适配脚本。
- Codex 可以通过 Computer Use 自动操作 LexTranslator GUI fallback，但所有输入和输出路径必须位于当前项目内。
- Codex 不绕过 LexTranslator 直接保存插件；插件输出必须由 LexTranslator 生成到 `translated/tool_outputs/<ModName>/` 或 `out/<ModName>/tool_outputs/`。
- 如果 LexTranslator 从 `.pex` 中导出可翻译字符串，Codex 只处理玩家可见文本导出，不直接修改 `.pex` 或 `.psc`。
- 存在 `Interface/translations/*.txt` 时，优先翻译独立翻译文件，不碰 `.pex`。

## 配置来源

- LexTranslator 可执行文件路径只从 `config/tools.local.json` 读取。
- `config/tools.local.json` 是本地配置，不提交到远程。
- GUI fallback 前先运行工具配置校验；路径缺失或不可访问时，工具阶段标记为 blocked。
- 继续工具阶段前先读 `docs/decoder_first_workflow.md`，不要默认进入 GUI。

## 动态词典索引

- LexTranslator 风格词表目录是 `glossary/lextranslator_dynamic_dictionaries/`。
- 索引文件是 `work/glossary_rag/lextranslator_dynamic.sqlite`。
- `scripts/build_lextranslator_dictionary_rag_index.py` 会比较动态词典目录和索引文件的修改时间；词典未变化时复用索引，词典更新时才重建。
- 翻译当前 Mod 前运行 `scripts/build_external_glossary_matches.py --mod-name "<ModName>"`，生成 `qa/<ModName>.external_glossary_matches.md`。
- 动态词典命中项用于辅助翻译，不代表 LexTranslator GUI fallback 已经执行，也不允许跳过后续 QA。

## GUI 自动化入口

LexTranslator 只在路由明确进入 GUI fallback 后使用。进入 GUI fallback 后，Computer Use 优先；项目内 pywinauto/UIA 脚本是 GUI 降级入口，只在 Computer Use 当前会话不可用、无法识别窗口或操作失败时使用：

```console
python .\scripts\invoke_lextranslator_gui.py --input-path ".\out\<ModName>\tool_outputs\<PluginName>.esp" --mode inspect
python .\scripts\invoke_lextranslator_gui.py --input-path ".\out\<ModName>\tool_outputs\<PluginName>.esp" --mode open
```

- `inspect`：降级模式下启动或连接 LexTranslator，只读取主窗口控件，不加载文件。
- `open`：降级模式下通过 UI Automation 触发 `Load File`，只打开项目内输入，不执行翻译、应用或保存。
- 当前脚本不会自动保存插件；保存/导出模式必须在后续补充 QA 门禁后单独实现。
- 如果 Computer Use 失败，必须先记录失败原因；如果 Python 环境缺少 `pywinauto`，降级脚本会写入 `qa/lextranslator_gui_report.md` 并标记 blocked。
- 对 `.esp/.esm/.esl/.pex` 等二进制输入，脚本默认拒绝直接从 `mod/` 打开；先复制到 `out/<ModName>/tool_outputs/` 或其他项目内工具输出目录。

## 失败处理

- 只打开窗口、只加载文件或只完成检查不算翻译完成。
- GUI 保存路径不可确认时，立即停止并标记 blocked。
- 人工临时保存只能作为本地记录，不能算作自动化完成；除非后续由受控工具适配器复现并写入项目内 `tool_outputs`。
