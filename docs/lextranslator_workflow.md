# LexTranslator Workflow

- LexTranslator 适合作为主力 AI 批量翻译工具。
- 建议先导入简体中文词典。
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
- Codex 不自动点击 LexTranslator GUI。
- Codex 不直接保存插件。

