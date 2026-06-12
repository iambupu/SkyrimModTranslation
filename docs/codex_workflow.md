# Codex Workflow

## Codex 可以做

- 分析 `mod/` 沙盒目录中的文件结构。
- 整理 LexTranslator/xTranslator 导出的文本。
- 生成批次。
- 根据术语表翻译 target 字段。
- 生成 QA 报告。
- 检查占位符错误。
- 汇总未确定术语。
- 生成可导入的文本文件。
- 生成 DSD Patch 文本结构。
- 维护文档和脚本。

## Codex 不能做

- 不能直接修改插件。
- 不能运行 GUI 自动点击。
- 不能覆盖 MO2 原始 Mod。
- 不能访问真实游戏目录。
- 不能猜测未知文件格式。
- 不能把文件写出当前项目目录。

## 标准任务模板

读取 `source/xxx.jsonl`，按 `docs/translation_rules.md` 和 `glossary/` 下的术语表翻译 `target` 字段，输出到 `translated/xxx.zh-CN.jsonl`，然后运行 `scripts/validate-translation.ps1` 校验。

