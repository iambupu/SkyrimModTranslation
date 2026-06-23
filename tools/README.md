# Tool Adapter

本目录记录外部工具适配层约定。优先使用 decoder/CLI 工具生成项目内文本中间文件；LexTranslator 与 xTranslator GUI 只作为必要时的 fallback。GUI 启动逻辑统一由 Python 入口负责，例如 `scripts/invoke_lextranslator.py`、`scripts/invoke_lextranslator_gui.py` 和 `scripts/invoke_xtranslator.py`。

`tools/README.md` 需要提交 Git；`tools/` 下的真实工具、SDK、下载包、解压目录和构建输出不提交。

## 配置方式

1. 将 `config/tools.example.json` 复制为 `config/tools.local.json`。
2. 在 `config/tools.local.json` 中填写本机工具路径。
3. 运行 `python scripts/detect_decoder_tools.py` 检查可用 decoder/CLI。

`config/tools.local.json` 是本机配置，不提交 Git。

## 必需或推荐工具

| 工具 | 用途 | 安装方式 |
|---|---|---|
| Python 依赖 | 7Z 解包、BSA/BA2 只读审计、文本处理、QA 脚本 | 可以让 Codex 运行 `python -m pip install -r requirements.txt`；也可以用户自行安装；当前包含 `py7zr` 和 `bethesda-structs` |
| .NET SDK | 构建/运行 Mutagen 插件和 PEX 适配器 | 可以让 Codex 安装到 `tools/dotnet-sdk/`；也可以用户自行安装后把 `DecoderTools.DotNetSdkPath` 指到 `dotnet.exe` |
| Mutagen 适配器源码 | ESP/ESM/ESL 文本导出、写回和验证；PEX 可见字符串导出/写回 | 项目内受控适配器源码位于根目录 `adapters/`，`tools/` 只保存本机工具缓存和下载物 |
| LexTranslator | GUI fallback，插件/PEX/字符串工具后备处理 | 通常由用户自行下载安装并在 `LexTranslatorPath` 填写路径；Codex 只操作项目内输入输出 |
| xTranslator | GUI fallback，精修、查漏、复杂导入或 PapyrusPex 后备 | 通常由用户自行下载安装并在 `XTranslatorPath` 填写路径；Codex 只操作项目内输入输出 |
| SSEEdit/xEdit 或安全 dump 包装器 | 插件文本辅助导出、交叉验证 | 用户可自行安装；也可以让 Codex 配置项目内 wrapper，例如 `scripts/invoke_ssedump_safe.py` |
| Champollion 或 PEX 工具 | PEX/PSC 只读分析或后备解码 | 可由用户自行安装，也可让 Codex 在项目内配置；默认优先 Mutagen PEX 适配器 |
| bethesda-structs | BSA/BA2 只读归档目录读取、候选分类和 manifest 证据 | Python 包依赖；不写归档、不解包、不重打包 |
| BSAFileExtractor | BSA 内容物化到项目内 `work/archive_extracts/` | 可放在 `tools/BSAFileExtractor/`；只能通过 `scripts/invoke_bsa_file_extractor_safe.py` 调用 |
| BA2 解包器 | BA2 资源提取计划或后续 adapter | 未配置单独 adapter 时只生成提取计划/阻断报告；不由 `bsa-archive-audit` Skill 承担 |
| 7-Zip CLI | `.7z` 解包后备 | 首选 Python `py7zr`；没有 `py7zr` 时可配置 `DecoderTools.Archive7zPath` |

## 可以让 Codex 做的事

- 安装 Python 包到当前 Python 环境。
- 在项目内准备 `.NET SDK`、Mutagen 适配器、工具 wrapper、检测报告和 QA 报告。
- 在项目内配置 `BSAFileExtractor`，并把 `DecoderTools.BsaFileExtractorPath` 指向 `scripts/invoke_bsa_file_extractor_safe.py`。
- 从用户提供的项目内压缩包解出工具到 `tools/`。
- 修改 `config/tools.local.json` 中的项目内工具路径。
- 运行 `python scripts/detect_decoder_tools.py`、decoder/CLI、QA 和 final_mod 组装脚本。

## 用户通常需要自己做的事

- 从工具作者页面获取 LexTranslator、xTranslator、SSEEdit/xEdit、BA2 解包器等 GUI 或第三方工具。
- 处理需要网页登录、Nexus 下载权限、许可确认或安装器交互的步骤。
- 确认工具许可允许本地使用。
- 在真实游戏、MO2/Vortex 或 Steam 环境中进行最终人工测试。

## 安全边界

- 工具可以位于项目外，但传给工具的输入路径必须位于当前项目目录内。
- 所有输出必须写入当前项目目录内。
- 对需要解码的 ESP/PEX/BSA/BA2，先配置 `config/tools.local.json` 的 `DecoderTools` 并运行 `python scripts/detect_decoder_tools.py`。
- BSA 默认先用 `bethesda-structs` 做只读 manifest；只有确实需要物化归档内容时，才通过 `scripts/invoke_bsa_file_extractor_safe.py` 解到 `work/archive_extracts/<ModName>/<ArchiveName>/`。
- BSA 内已汉化资源默认以归档内原始相对路径生成 loose override，由 `final_mod/` 中同路径文件覆盖归档内资源；原 `.bsa` 原样复制，不重打包。
- BSA 重打包不是默认工具路径。只有人工测试证明 loose override 不加载或导致 Mod 问题，并且后续配置了受控 BSA packer adapter、manifest、hash 校验和 QA 证据时，才允许进入高风险重打包流程。
- GUI fallback 只能写项目内 `tool_outputs`，不得直接写真实游戏目录或真实 MO2/Vortex 目录。
- Codex 不直接修改 `.esp/.esm/.esl/.pex/.bsa/.ba2`，只能调用受控 decoder/CLI 或 GUI 工具生成项目内副本。
- 如果 decoder/GUI 操作无法自动化，脚本只输出报告和人工操作清单，不假装完成。
