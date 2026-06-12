你现在要初始化一个“Skyrim SE/AE Mod 自动化汉化工程”。

项目目标：
建立一个可维护、可回滚、可批量处理的上古卷轴5 Mod 汉化项目。项目已经安装并计划配合使用 LexTranslator 和 xTranslator。Codex 只负责文本工程、术语表、翻译中间文件、校验脚本、格式转换脚本和文档，不直接修改任何 Skyrim 游戏插件或真实游戏目录。

当前项目根目录：
- 以当前工作目录作为项目根目录。
- 当前项目根目录下必须有一个专用 mod/ 目录。
- mod/ 是唯一允许 Codex 读取和分析的 Mod 输入目录。
- mod/ 必须被视为从 MO2/Vortex/游戏目录复制出来的沙盒副本。
- Codex 不能访问真实游戏目录、真实 MO2/Vortex 目录、Steam 游戏安装目录或 AppData/Documents 下的游戏配置目录。
- Codex 的所有输出只能写入当前项目目录下的 source/、work/、translated/、qa/、out/、docs/、scripts/、glossary/。

我的工具：
- 已安装 LexTranslator。
- 已安装 xTranslator。
- 目标游戏：Skyrim Special Edition / Anniversary Edition。
- 目标语言：简体中文。
- 系统：Windows 10。
- 默认 Shell：PowerShell。

严格环境约束：
- 所有命令必须使用 PowerShell。
- 禁止使用 Bash、WSL、Linux 命令。
- 禁止使用 sed、awk、grep、rm、cp、mv、cat、touch、mkdir -p 等 Unix 风格命令。
- 可以使用 PowerShell 原生命令和 Python。
- 不要自动操作 LexTranslator 或 xTranslator GUI。
- 最终导入、回写、保存插件、复制到 MO2/Vortex 的动作由我手动执行。

绝对禁止：
1. 禁止直接修改真实 Skyrim 游戏目录。
2. 禁止直接修改真实 MO2/Vortex mods 目录。
3. 禁止访问 SteamLibrary/steamapps/common/Skyrim Special Edition。
4. 禁止访问 Skyrim Special Edition/Data。
5. 禁止访问 AppData 下的 MO2/Vortex/Skyrim 配置目录。
6. 禁止访问 Documents/My Games/Skyrim Special Edition，除非我明确要求读取日志。
7. 禁止直接修改 .esp/.esm/.esl/.bsa/.ba2/.pex/.dll/.exe 文件。
8. 禁止把任何文件写回真实游戏目录或真实 Mod 管理器目录。
9. 禁止根据 config/tools.example.json 里的示例路径自动访问真实目录。
10. 禁止覆盖 mod/ 下的原始文件，除非该文件是明确的文本导出文件，并且已经先创建备份。

允许处理的文件类型：
- .json
- .jsonl
- .xml
- .csv
- .txt
- .md
- .ps1
- .py

禁止编辑的文件类型：
- .esp
- .esm
- .esl
- .bsa
- .ba2
- .pex
- .dll
- .exe
- .7z
- .zip
- .rar

工具分工：
- LexTranslator：用于 AI 批量翻译、词典、MCM/PEX/ESP/ESM 文本处理。
- xTranslator：用于精修、查漏、对照、回写、处理复杂插件文本。
- Codex：用于项目结构、术语表、文本分批、翻译辅助、校验脚本、占位符检查、格式整理、QA 报告。
- Codex 不能直接保存插件，不能直接修改插件二进制文件。

核心翻译规则：
- 翻译方向：English → 简体中文。
- 风格：自然游戏本地化，不要机翻腔，不要现代网络用语，不要网文腔。
- UI/MCM 文本要短、准、清楚。
- 任务目标要清晰直接。
- 对话要符合角色身份和奇幻世界语感。
- 书籍文本可以更文学，但不能改变剧情含义。
- 物品名要短。
- 法术名要有奇幻感。
- 不翻译 FormID、EditorID、脚本名、变量名、路径、文件名、插件名。
- 保留所有占位符、换行符、颜色标签、HTML/XML 标签、MCM 控制符。
- 不确定的专有名词写入 qa/unresolved_terms.md，不要擅自硬翻。
- 每次批量翻译后必须运行校验脚本。

必须保护的内容：
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
- %s、%d、%f
- {0}、{1}、{name}
- <Alias=...>
- <font ...>
- <color ...>
- $变量
- \n
- \r\n

请完成以下初始化任务。

一、创建目录结构：

AGENTS.md
README.md
.gitignore

config/
  tools.example.json

mod/
  .gitkeep

glossary/
  skyrim_cn_glossary.md
  mod_terms.md
  lex_dictionary_notes.md

source/
  lextranslator_exports/
  xtranslator_exports/
  raw/

work/
  batches/
  normalized/

translated/
  lextranslator_ready/
  xtranslator_ready/

qa/
  validation_errors.md
  review_notes.md
  unresolved_terms.md

out/
  lex_dictionary/
  xtranslator_import/
  dsd_patch/

scripts/
  split-jsonl.ps1
  validate-translation.ps1
  scan-placeholders.ps1
  normalize-export.ps1

docs/
  translation_rules.md
  lextranslator_workflow.md
  xtranslator_workflow.md
  codex_workflow.md
  mod_sandbox_rules.md

samples/
  sample_export.jsonl
  sample_terms.csv

二、创建 AGENTS.md，必须包含以下内容：

1. 项目目标：
   - 本项目用于 Skyrim SE/AE Mod 汉化工程。
   - Codex 是文本工程助手，不是插件编辑器。
   - 项目配合 LexTranslator 和 xTranslator 使用。

2. 工作边界：
   - Codex 只能处理当前项目目录。
   - Codex 只能读取当前项目目录下的 mod/ 作为 Mod 输入。
   - Codex 不能访问真实游戏目录、真实 MO2/Vortex 目录。
   - Codex 不能直接修改 .esp/.esm/.esl/.bsa/.ba2/.pex 等文件。
   - Codex 只能编辑文本类文件。

3. Windows 约束：
   - 使用 PowerShell。
   - 禁止 Bash/WSL/Linux 命令。

4. mod/ 沙盒规则：
   - mod/ 是项目内沙盒 Mod 副本。
   - mod/ 不是游戏实际加载目录。
   - 所有导出、分析、翻译、校验都只能围绕 mod/ 和项目内目录进行。
   - 输出只能进入 source/、work/、translated/、qa/、out/。

5. 翻译规则：
   - 简体中文。
   - 自然游戏本地化。
   - 保留占位符和格式。
   - 不确定术语进入 qa/unresolved_terms.md。

6. QA 要求：
   - 批量翻译后必须运行校验脚本。
   - 必须检查行数、JSON 格式、ID 不变、占位符不丢失、target 不为空。
   - 必须记录错误。

7. Git 建议：
   - 每处理一个 Mod 或一个 batch 提交一次。
   - 不提交真实插件二进制。
   - 不提交压缩包。

三、创建 README.md，内容包括：

1. 项目用途：
   - 用于管理 Skyrim SE/AE Mod 汉化流程。
   - 目标是让翻译流程可维护、可回滚、可批量处理。

2. 推荐工作流：

   第一步：手动把待翻译 Mod 复制到当前项目的 mod/ 目录。
   第二步：使用 LexTranslator 或 xTranslator 打开 mod/ 副本中的插件或导出文本。
   第三步：把导出的文本文件放入 source/lextranslator_exports/ 或 source/xtranslator_exports/。
   第四步：Codex 进行格式整理、分批、翻译辅助、术语统一和校验。
   第五步：输出译文到 translated/ 或 out/。
   第六步：我手动把结果导入 LexTranslator 或 xTranslator。
   第七步：我手动保存 Patch 或翻译输出。
   第八步：我手动复制到 MO2/Vortex 测试。
   第九步：把游戏内发现的问题记录到 qa/review_notes.md。
   第十步：修正后提交 Git。

3. 安全说明：
   - 本项目不直接修改原始 ESP/ESM/ESL。
   - 本项目不访问真实游戏目录。
   - mod/ 是沙盒副本。
   - 最终回写和游戏加载由用户手动完成。

4. 工具分工：
   - LexTranslator 用于主力 AI 批量翻译。
   - xTranslator 用于精修、查漏、对照和回写。
   - Codex 用于文本工程和 QA。

5. 目录结构说明。

6. 如何添加一个新的 Mod 翻译任务。

四、创建 config/tools.example.json，内容为示例路径，不要访问这些路径，不要写死真实路径：

{
  "LexTranslatorPath": "C:\\Modding\\Tools\\LexTranslator\\LexTranslator.exe",
  "XTranslatorPath": "C:\\Modding\\Tools\\xTranslator\\xTranslator.exe",
  "MO2ModsPath": "D:\\MO2\\mods",
  "GameDataPath": "D:\\SteamLibrary\\steamapps\\common\\Skyrim Special Edition\\Data",
  "OutputPatchModPath": "D:\\MO2\\mods\\Bupu CN Translation Patches"
}

并在文件中用注释或 README 说明：
- 这些只是示例路径。
- 脚本不得自动访问这些路径。
- 真实路径由用户手动处理。

五、创建 glossary/skyrim_cn_glossary.md，加入常见 Skyrim 术语：

# Skyrim CN Glossary

## 核心设定

| English | 简体中文 | 说明 |
|---|---|---|
| Dragonborn | 龙裔 | 主角称号 |
| Daedra | 迪德拉 | 魔神/迪德拉相关设定 |
| Aedra | 圣灵 | 圣灵相关设定 |
| Jarl | 领主 | 天际地方统治者 |
| Thane | 男爵 | 领主授予的荣誉头衔 |
| Hold | 领地 | 天际行政区域 |

## 地名

| English | 简体中文 | 说明 |
|---|---|---|
| Whiterun | 雪漫 | 城市 |
| Solitude | 独孤城 | 城市 |
| Windhelm | 风盔城 | 城市 |
| Riften | 裂谷城 | 城市 |
| Markarth | 马卡斯城 | 城市 |
| Winterhold | 冬堡 | 城市/领地 |
| Dawnstar | 晨星 | 城市 |
| Falkreath | 佛克瑞斯 | 城市 |
| Morthal | 莫萨尔 | 城市 |

## 种族

| English | 简体中文 | 说明 |
|---|---|---|
| Nord | 诺德人 | 种族 |
| Imperial | 帝国人 | 种族 |
| Breton | 布莱顿人 | 种族 |
| Redguard | 红卫人 | 种族 |
| Altmer | 高精灵 | 种族 |
| Dunmer | 暗精灵 | 种族 |
| Bosmer | 木精灵 | 种族 |
| Orc | 兽人 | 种族 |
| Khajiit | 虎人 | 种族 |
| Argonian | 亚龙人 | 种族 |

## 派系

| English | 简体中文 | 说明 |
|---|---|---|
| College of Winterhold | 冬堡学院 | 派系 |
| Companions | 战友团 | 派系 |
| Thieves Guild | 盗贼公会 | 派系 |
| Dark Brotherhood | 黑暗兄弟会 | 派系 |
| Imperial Legion | 帝国军团 | 派系 |
| Stormcloaks | 风暴斗篷 | 派系 |

六、创建 glossary/mod_terms.md，内容为当前 Mod 专有术语模板：

# Mod Terms

## 当前 Mod

- Mod 名称：
- 插件名：
- 版本：
- 翻译状态：
- 备注：

## 人名

| English | 简体中文 | 说明 |
|---|---|---|

## 地名

| English | 简体中文 | 说明 |
|---|---|---|

## 组织/派系

| English | 简体中文 | 说明 |
|---|---|---|

## 物品/法术/技能

| English | 简体中文 | 说明 |
|---|---|---|

## 任务/剧情关键词

| English | 简体中文 | 说明 |
|---|---|---|

## 不确定术语

| English | 暂定译名 | 问题 |
|---|---|---|

七、创建 glossary/lex_dictionary_notes.md，内容包括：

# LexTranslator Dictionary Notes

- LexTranslator 可用于 AI 批量翻译和词典辅助。
- 本项目中的 glossary 文件先作为人类可读术语表。
- 不要臆造 LexTranslator 的真实词典格式。
- 如果用户提供 LexTranslator 导出的词典样例，再根据样例生成 out/lex_dictionary/ 下的真实词典文件。
- 词典内容优先来自：
  1. glossary/skyrim_cn_glossary.md
  2. glossary/mod_terms.md
  3. qa/unresolved_terms.md 中经过用户确认的条目

八、创建 samples/sample_export.jsonl，内容如下，每行一个 JSON：

{"id":"00012ABC","plugin":"ExampleMod.esp","type":"BOOK","field":"DESC","source":"The old shrine is silent.","target":"","note":""}
{"id":"00012ABD","plugin":"ExampleMod.esp","type":"NPC_NAME","field":"FULL","source":"Eldrin the Wanderer","target":"","note":""}
{"id":"00012ABE","plugin":"ExampleMod.esp","type":"MCM","field":"TEXT","source":"Enable advanced mode","target":"","note":""}
{"id":"00012ABF","plugin":"ExampleMod.esp","type":"DIALOGUE","field":"NAM1","source":"I saw him near Whiterun. He carried a blade marked with an old sigil.","target":"","note":""}

九、创建 samples/sample_terms.csv，内容如下：

English,Chinese,Type,Note
Dragonborn,龙裔,Lore,Skyrim core term
Whiterun,雪漫,Location,Skyrim city
Jarl,领主,Title,Skyrim political title

十、创建 scripts/validate-translation.ps1。

功能要求：
- 参数：
  - SourcePath
  - TranslatedPath
  - ErrorOutputPath，默认 qa/validation_errors.md
- 检查输入路径和输出路径必须位于当前项目目录内。
- 如果路径在项目外，拒绝执行。
- 检查两个 JSONL 文件行数是否一致。
- 检查每一行是否是合法 JSON。
- 检查 id/plugin/type/field/source 是否未被修改。
- 检查 target 是否为空。
- 检查 source 中出现的常见占位符是否也出现在 target：
  - %s
  - %d
  - %f
  - {0}
  - {1}
  - {name}
  - <...>
  - $变量
  - \n
- 检查 target 是否包含明显未翻译英文长句。
- 输出错误到控制台。
- 同时写入 qa/validation_errors.md。
- 脚本不能因为某一行错误而直接崩溃，要继续检查后续行。
- 使用 PowerShell。
- 不使用 Bash 命令。

实现要求：
- 使用当前脚本所在位置或当前工作目录推导项目根目录。
- 使用 Resolve-Path 检查路径是否在项目根目录下。
- 对不存在的输出文件可以自动创建。
- 对不存在的输入文件明确报错。
- 错误报告使用 UTF-8 编码。

十一、创建 scripts/split-jsonl.ps1。

功能要求：
- 参数：
  - InputPath
  - OutputDir，默认 work/batches
  - BatchSize，默认 100
- 输入路径必须位于当前项目目录内。
- 输出路径必须位于当前项目目录内。
- 如果路径在项目外，拒绝执行。
- 按行切分 JSONL 文件。
- 输出 batch_001.jsonl、batch_002.jsonl 等。
- 保持 UTF-8 编码。
- 不改变每行内容。
- 输出切分总结。
- 使用 PowerShell。
- 不使用 Bash 命令。

十二、创建 scripts/scan-placeholders.ps1。

功能要求：
- 参数：
  - InputPath
  - ReportOutputPath，默认 qa/placeholder_report.md
- 输入和输出路径必须位于当前项目目录内。
- 扫描 JSONL 文件中的 source 和 target 字段。
- 提取常见占位符、标签和变量：
  - %s
  - %d
  - %f
  - {0}
  - {1}
  - {name}
  - <...>
  - $变量
  - \n
- 对比 source 与 target 是否一致。
- 输出报告到 qa/placeholder_report.md。
- 使用 PowerShell。
- 不使用 Bash 命令。

十三、创建 scripts/normalize-export.ps1。

功能要求：
- 参数：
  - InputPath
  - OutputDir，默认 work/normalized
- 输入路径必须位于当前项目目录内。
- 输出路径必须位于当前项目目录内。
- 不假设 LexTranslator 或 xTranslator 的固定导出格式。
- 先实现 JSONL 标准化处理。
- 如果输入已经是 JSONL，则复制到 work/normalized/。
- 如果不是 JSONL，则提示用户需要提供导出格式样例。
- 不破坏原始文件。
- 不覆盖原始文件。
- 不猜测未知格式。
- 使用 PowerShell。
- 不使用 Bash 命令。

十四、创建 docs/translation_rules.md，内容包括：

# Translation Rules

## 语言
- 使用简体中文。
- 保持自然游戏本地化风格。
- 不要机翻腔。
- 不要把奇幻文本翻成现代网络用语。
- 不要擅自改写剧情含义。

## 类型规则
- UI/MCM 文本：短而准确。
- 书籍文本：可以更文学，但不能改变信息。
- 对话文本：符合角色身份。
- 任务目标：清晰直接。
- 物品名：短，像游戏内名称。
- 法术名：保留奇幻感。
- 技能/效果说明：准确优先。

## 禁止翻译
- FormID
- EditorID
- 脚本名
- 变量名
- 路径
- 文件名
- 插件名
- JSON key
- XML/HTML tag

## 必须保留
- 占位符
- 换行符
- 颜色标签
- 字体标签
- HTML/XML 标签
- MCM 控制符
- Papyrus 变量标记

## 不确定术语
- 写入 qa/unresolved_terms.md。
- 不要擅自硬翻。
- 需要用户确认后再进入 glossary/mod_terms.md。

十五、创建 docs/lextranslator_workflow.md，内容包括：

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
- 如果导出格式不确定，把样例放到 source/lextranslator_exports/，再让 Codex 适配脚本。
- Codex 不自动点击 LexTranslator GUI。
- Codex 不直接保存插件。

十六、创建 docs/xtranslator_workflow.md，内容包括：

# xTranslator Workflow

- xTranslator 适合做精修、查漏、对照、回写。
- 推荐用于检查 ESP/ESM、Strings、MCM/Translate、PapyrusPex 文本。
- 对复杂插件，先用 xTranslator 查看文本结构和未翻译项。
- 可以导出文本供 Codex 分析。
- Codex 不直接保存插件。
- 最终保存插件前，必须备份原插件或使用独立 Patch Mod。
- 推荐每次只处理一个插件，避免批量误伤。
- 不要让 Codex 直接操作真实 MO2/Vortex 目录。

十七、创建 docs/codex_workflow.md，内容包括：

# Codex Workflow

## Codex 可以做
- 分析 mod/ 沙盒目录中的文件结构。
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

读取 source/xxx.jsonl，按 docs/translation_rules.md 和 glossary/ 下的术语表翻译 target 字段，输出到 translated/xxx.zh-CN.jsonl，然后运行 scripts/validate-translation.ps1 校验。

十八、创建 docs/mod_sandbox_rules.md，内容包括：

# mod/ Sandbox Rules

- mod/ 是当前项目唯一允许处理的 Mod 输入目录。
- mod/ 必须是从真实 Mod 管理器复制出来的副本。
- mod/ 不是游戏实际加载目录。
- Codex 可以读取 mod/。
- Codex 可以扫描 mod/ 文件结构。
- Codex 可以复制 mod/ 下的文本导出文件到 source/。
- Codex 不能直接修改 mod/ 下的插件二进制文件。
- Codex 不能把输出写回真实游戏目录。
- 所有输出必须写入项目内目录。
- 最终导入 LexTranslator/xTranslator 由用户手动执行。
- 最终复制到 MO2/Vortex 由用户手动执行。

十九、创建 .gitignore，内容包括：

*.esp
*.esm
*.esl
*.bsa
*.ba2
*.pex
*.dll
*.exe
*.7z
*.zip
*.rar
*.bak
logs/
temp/
out/generated/

mod/**/*.esp
mod/**/*.esm
mod/**/*.esl
mod/**/*.bsa
mod/**/*.ba2
mod/**/*.pex
mod/**/*.dll
mod/**/*.exe
mod/**/*.7z
mod/**/*.zip
mod/**/*.rar

二十、初始化 qa 文件：

qa/validation_errors.md：

# Validation Errors

当前暂无校验错误。

qa/review_notes.md：

# Review Notes

用于记录游戏内测试、人工校对和回写后的问题。

qa/unresolved_terms.md：

# Unresolved Terms

| English | Context | Suggested Chinese | Status |
|---|---|---|---|

二十一、初始化 Git：

- 如果当前目录不是 Git 仓库，则执行 git init。
- 添加所有初始化文件。
- 不要提交真实 Mod 二进制文件。
- 不要提交压缩包。
- 如果 Git 可用，创建第一次提交：

git add .
git commit -m "init skyrim mod translation workflow"

注意：
- git 命令可以使用。
- 其他文件操作仍然必须使用 PowerShell 原生命令。
- 不要使用 Bash。

二十二、完成后运行一次基本检查：

1. 确认目录存在：
   - mod/
   - source/
   - work/
   - translated/
   - qa/
   - out/
   - scripts/
   - docs/
   - glossary/
   - samples/

2. 确认 samples/sample_export.jsonl 存在。

3. 用 scripts/validate-translation.ps1 对 samples/sample_export.jsonl 自检一次：
   - SourcePath = samples/sample_export.jsonl
   - TranslatedPath = samples/sample_export.jsonl
   - 因为 target 为空而报错是预期行为。
   - 脚本不能崩溃。
   - 脚本应生成 qa/validation_errors.md。

4. 输出初始化总结：
   - 创建了哪些目录。
   - 创建了哪些脚本。
   - 创建了哪些文档。
   - 路径安全规则是否已经加入。
   - 下一步如何把 LexTranslator 或 xTranslator 导出的文件接入本项目。

验收标准：
- 项目结构完整。
- mod/ 目录存在。
- AGENTS.md 明确限制 Codex 只能围绕 mod/ 和项目内目录工作。
- README.md 明确说明 mod/ 是沙盒副本。
- PowerShell 脚本能运行。
- PowerShell 脚本有项目内路径安全检查。
- 不依赖 Bash。
- 不访问真实 Skyrim 目录。
- 不访问真实 MO2/Vortex 目录。
- 不修改任何 Skyrim 插件文件。
- 不提交任何插件二进制或压缩包。