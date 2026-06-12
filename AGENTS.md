# Skyrim SE/AE Mod 自动化汉化工程规则

## 1. 项目目标

- 本项目用于 Skyrim SE/AE Mod 汉化工程。
- Codex 是文本工程助手，不是插件编辑器。
- 项目配合 LexTranslator 和 xTranslator 使用。
- 项目目标是建立可维护、可回滚、可批量处理的汉化流程。

## 2. 工作边界

- Codex 只能处理当前项目目录。
- Codex 只能读取当前项目目录下的 `mod/` 作为 Mod 输入。
- Codex 不能访问真实游戏目录、真实 MO2/Vortex 目录。
- Codex 不能直接修改 `.esp`、`.esm`、`.esl`、`.bsa`、`.ba2`、`.pex` 等文件。
- Codex 只能编辑文本类文件。
- Codex 不能直接保存插件，不能直接修改插件二进制文件。

## 3. Windows 约束

- 使用 PowerShell。
- 禁止 Bash/WSL/Linux 命令。
- 禁止使用 `sed`、`awk`、`grep`、`rm`、`cp`、`mv`、`cat`、`touch`、`mkdir -p` 等 Unix 风格命令。
- 可以使用 PowerShell 原生命令和 Python。

## 4. mod/ 沙盒规则

- `mod/` 是项目内沙盒 Mod 副本。
- `mod/` 不是游戏实际加载目录。
- 所有导出、分析、翻译、校验都只能围绕 `mod/` 和项目内目录进行。
- 输出只能进入 `source/`、`work/`、`translated/`、`qa/`、`out/`。
- 不覆盖 `mod/` 下的原始文件，除非该文件是明确的文本导出文件，并且已经先创建备份。

## 5. 翻译规则

- 目标语言为简体中文。
- 风格为自然游戏本地化。
- 保留占位符和格式。
- 不翻译 FormID、EditorID、脚本名、变量名、路径、文件名、插件名。
- 不确定术语进入 `qa/unresolved_terms.md`。

## 6. QA 要求

- 批量翻译后必须运行校验脚本。
- 必须检查行数、JSON 格式、ID 不变、占位符不丢失、target 不为空。
- 必须记录错误。
- 校验错误默认写入 `qa/validation_errors.md`。

## 7. Git 建议

- 每处理一个 Mod 或一个 batch 提交一次。
- 不提交真实插件二进制。
- 不提交压缩包。
- 不提交真实游戏目录、真实 MO2/Vortex 目录或 AppData 配置目录内容。

## 8. 必须保护的内容

- FormID
- Plugin name
- Record type
- EditorID
- Script name
- Variable name
- File path
- File name
- JSON key
- XML tag
- HTML-like tag
- `%s`、`%d`、`%f`
- `{0}`、`{1}`、`{name}`
- `<Alias=...>`
- `<font ...>`
- `<color ...>`
- `$变量`
- `\n`
- `\r\n`

