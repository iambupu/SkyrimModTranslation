# Tool Adapter

本目录中的工具配置和调用只面向 Windows 工作区。命令通过 PowerShell 和插件源 Python 安全包装入口执行，不直接调用 Bash、WSL 或 Linux 工具链。

本目录记录外部工具适配层约定。优先使用 decoder/CLI 工具生成项目内文本中间文件；LexTranslator 与 xTranslator GUI 只作为必要时的 fallback。GUI 启动逻辑统一由 Python 入口负责，例如 `scripts/invoke_lextranslator.py`、`scripts/invoke_lextranslator_gui.py` 和 `scripts/invoke_xtranslator.py`。

`tools/README.md` 需要提交 Git；仓库 `tools/` 不保存真实工具、SDK、下载包、解压目录或构建输出。自动准备的非 GUI 工具发布到 Windows Local AppData 下的版本化共享托管存储，工作区只保存绑定。

## 配置方式

1. 普通用户通过 `python scripts/smt.py run ... --tool-setup auto` 发布或复用共享托管工具。
2. 只有用户自行安装的外部工具路径才写入工作区 `config/tools.local.json`。
3. CLI 内部运行 `detect_decoder_tools.py` 检查共享绑定和外部 decoder/CLI。

`config/tools.local.json` 是本机外部工具配置，不提交 Git，也不记录共享缓存绝对路径。

## 必需或推荐工具

| 工具 | 用途 | 安装方式 |
|---|---|---|
| Python 依赖 | 7Z 解包、BSA/BA2 只读审计、文本处理、QA 脚本 | `auto` 从已提交的精确 hash-pinned runtime 导出构建或复用共享不可变 Python runtime |
| .NET SDK | 构建/运行 Mutagen 插件和 PEX 适配器 | `auto` 下载并校验固定包 SHA-256 后发布到共享存储；用户也可自行安装并配置外部 `dotnet.exe` |
| Mutagen 适配器源码 | ESP/ESM/ESL 文本导出、写回和验证；PEX 可见字符串导出/写回 | 受控源码位于根目录 `adapters/`；构建输出按源码、SDK、目标框架、RID 和架构身份发布到共享存储 |
| LexTranslator | GUI fallback，插件/PEX/字符串工具后备处理 | 通常由用户自行下载安装并在 `LexTranslatorPath` 填写路径；Codex 只操作项目内输入输出 |
| xTranslator | GUI fallback，精修、查漏、复杂导入或 PapyrusPex 后备 | 通常由用户自行下载安装并在 `XTranslatorPath` 填写路径；Codex 只操作项目内输入输出 |
| ESP-ESM Translator | 可选 GUI 工具；原生 EET 工程/数据库检查 | 用户自行下载安装并在 `EspEsmTranslatorPath` 填写路径；RAG 通过项目只读解析器读取 EET，不依赖 GUI |
| SSEEdit/xEdit 或安全 dump 包装器 | 插件文本辅助导出、交叉验证 | 用户可自行安装；也可以让 Codex 配置项目内 wrapper，例如 `scripts/invoke_ssedump_safe.py` |
| Champollion 或 PEX 工具 | PEX/PSC 只读分析或后备解码 | Champollion 固定源码快照可由 `auto` 校验并发布到共享存储；也可配置用户外部工具；默认优先 Mutagen PEX 适配器 |
| bethesda-structs | BSA/BA2 只读归档目录读取、候选分类和 manifest 证据 | Python 包依赖；不写归档、不解包、不重打包 |
| BSAFileExtractor | BSA 内容物化到项目内 `work/archive_extracts/` | 固定源码快照可由 `auto` 校验并发布到共享存储；只能通过 `scripts/invoke_bsa_file_extractor_safe.py` 调用 |
| BA2 解包器 | BA2 资源提取计划或后续 adapter | 未配置单独 adapter 时只生成提取计划/阻断报告；不由 `bsa-archive-audit` Skill 承担 |
| 7-Zip CLI | `.7z` 解包后备 | 首选 Python `py7zr`；没有 `py7zr` 时可配置 `DecoderTools.Archive7zPath` |

## 可以让 Codex 做的事

- 通过公开 `smt.py run` 的 `auto` 模式发布或复用共享 Python、.NET SDK、固定 decoder 源码和 adapter 构建输出。
- 在工作区写入 `.workflow/managed-tools.json` 绑定、检测报告和 QA 报告。
- 保持 `BsaFileExtractorPath` 指向项目受控的 `scripts/invoke_bsa_file_extractor_safe.py` wrapper。
- 校验用户明确填写在 `config/tools.local.json` 中的外部工具路径。
- 由公开控制器按状态机授权调用 decoder/CLI、QA 和 final_mod 组装脚本。

## 用户通常需要自己做的事

- 从工具作者页面获取 LexTranslator、xTranslator、ESP-ESM Translator、SSEEdit/xEdit、BA2 解包器等 GUI 或第三方工具。
- 处理需要网页登录、Nexus 下载权限、许可确认或安装器交互的步骤。
- 确认工具许可允许本地使用。
- 在真实游戏、MO2/Vortex 或 Steam 环境中进行最终人工测试。

## 安全边界

- 工具可以位于项目外，但传给工具的输入路径必须位于当前项目目录内。
- 所有输出必须写入当前项目目录内。
- 共享托管工具只允许由项目受控脚本访问版本化 Local AppData payload/control 根；不得手动删除目录或编辑 catalog。清理和卸载只能通过 `managed-tool-cache-maintenance` 的 inspect → plan → 明确确认 → apply → inspect 流程。
- 对需要解码的 ESP/PEX/BSA/BA2，先配置 `config/tools.local.json` 的 `DecoderTools` 并运行 `python scripts/detect_decoder_tools.py`。
- BSA 默认先用 `bethesda-structs` 做只读 manifest；只有确实需要物化归档内容时，才通过 `scripts/invoke_bsa_file_extractor_safe.py` 解到 `work/archive_extracts/<ModName>/<ArchiveName>/`。
- BSA 内已汉化资源默认以归档内原始相对路径生成 loose override，由 `final_mod/` 中同路径文件覆盖归档内资源；原 `.bsa` 原样复制，不重打包。
- BSA 重打包不是默认工具路径。只有人工测试证明 loose override 不加载或导致 Mod 问题，并且后续配置了受控 BSA packer adapter、manifest、hash 校验和 QA 证据时，才允许进入高风险重打包流程。
- GUI fallback 只能写项目内 `tool_outputs`，不得直接写真实游戏目录或真实 MO2/Vortex 目录。
- Codex 不直接修改 `.esp/.esm/.esl/.pex/.bsa/.ba2`，只能调用受控 decoder/CLI 或 GUI 工具生成项目内副本。
- 如果 decoder/GUI 操作无法自动化，脚本只输出报告和人工操作清单，不假装完成。
