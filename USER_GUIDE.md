# 普通用户指南

这份指南覆盖一次汉化的日常流程：安装、选择游戏、创建工作区、放入 Mod、开始或继续处理、查看产物，以及人工游戏测试。工具和报告细节见 [高级用户指南](./ADVANCED_USER_GUIDE.md)。

## 准备环境

- Windows。
- Python 3.11 或更高版本。
- Codex、opencode 或 Claude Code，任选其一。只有 Codex 可以处理桌面工具步骤。
- 本仓库源码（使用 opencode 或手动运行初始化脚本时需要）。
- 要汉化的 Skyrim SE/AE 或 Fallout 4 Mod 副本。

不要把真实游戏目录或 MO2/Vortex 目录作为输入。

## 安装与接入 Agent

三种 Agent 使用相同的工作区、翻译规则和检查标准。差别主要在桌面工具：Codex 可以操作 LexTranslator 和 xTranslator；opencode、Claude Code 遇到这类步骤时会暂停，并提示交给 Codex。

### Codex

打开 PowerShell，运行：

```powershell
codex plugin marketplace add iambupu/SkyrimModTranslation --ref master
codex plugin add skyrim-mod-chs-translation --marketplace skyrim-mod-chs
```

安装完成后，用 Codex 打开插件源码目录或准备好的工作区。

### opencode

opencode 使用项目生成的工作区本地插件。先安装 opencode CLI，再在本仓库目录打开 PowerShell。下面的命令会创建 Skyrim SE/AE 工作区、写入本地插件配置并启动 opencode：

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\MyMod --game skyrim-se --tool-setup auto
```

Fallout 4 工作区使用：

```powershell
python scripts\init_opencode.py D:\Fallout4CHS\MyMod --game fallout4 --tool-setup auto
```

已有工作区不需要再次指定游戏：

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\MyMod
```

脚本会在工作区生成 `opencode.json` 和 `.opencode/plugins/skyrim-chs.js`，并读取现有工作状态。它不会复制整套项目源码，也不会增加桌面操作能力。

### Claude Code

在 Claude Code 中运行：

```text
/plugin marketplace add iambupu/SkyrimModTranslation@master
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

安装完成后，用 Claude Code 打开准备好的工作区。这个入口只提供不需要桌面操作的功能；需要桌面工具时会暂停并提示交给 Codex。

## 选择游戏

游戏身份在创建工作区时确定：

- 新工作区应显式使用 `--game skyrim-se` 或 `--game fallout4`。
- 命令行没有传 `--game` 时，交互终端会要求选择游戏并二次确认；非交互调用会退出，不再静默选择 Skyrim。
- 工作区 marker 必须包含 `game_id`；缺失时流程会停止并要求重新初始化或补全游戏身份。
- 通过 Agent 新建工作区时，如果用户没有说明游戏，Agent 会先用自然语言询问“Skyrim SE/AE 还是 Fallout 4”。

初始化后，`.skyrim-chs-workspace.json` 是游戏身份的权威来源。流程不按 Mod 名、目录名或文件名猜游戏，也不会因为后来放入了另一个游戏的文件而自动切换。

## 创建工作区

工作区必须位于插件源码仓库之外，目标路径应当不存在或为空。

推荐直接告诉当前 Agent：

```text
帮我在 D:\SkyrimCHS\MyMod 创建一个 Skyrim SE Mod 汉化工作区，并自动准备工具。
```

或者：

```text
帮我在 D:\Fallout4CHS\MyMod 创建一个 Fallout 4 Mod 汉化工作区，并自动准备工具。
```

需要手动初始化时，可以在仓库目录使用下面的 PowerShell 命令。

Skyrim SE/AE：

```powershell
python scripts\init_workspace.py D:\SkyrimCHS\MyMod --game skyrim-se --tool-setup auto
```

Fallout 4 Experimental：

```powershell
python scripts\init_workspace.py D:\Fallout4CHS\MyMod --game fallout4 --tool-setup auto
```

`--tool-setup auto` 只准备受控的非 GUI 工具。需要桌面程序时，Codex 会继续处理；opencode 和 Claude Code 会说明需要交给 Codex 的步骤。

## 配置词典

新工作区会创建 `glossary/`。词典不是开始汉化或生成交付包的必要条件，但能明显改善专有名词和重复文本的一致性。

工作流只读取当前游戏对应的目录，不会把 Skyrim 词典用于 Fallout 4，也不会反向混用。

| 路径 | 适用游戏 | 用途 |
|---|---|---|
| `glossary/mod_terms.md` | 当前工作区 | 当前 Mod 已确认的专有名词，优先级最高 |
| `glossary/skyrim_cn_glossary.md` | Skyrim SE/AE | Skyrim 基础术语表 |
| `glossary/fallout4_cn_glossary.md` | Fallout 4 | Fallout 4 基础术语表 |
| `glossary/lextranslator_dynamic_dictionaries/skyrim/` | Skyrim SE/AE | LexTranslator 风格的 TXT、CSV、DICT 词典 |
| `glossary/lextranslator_dynamic_dictionaries/fallout4/` | Fallout 4 | LexTranslator 风格的 TXT、CSV、DICT 词典 |
| `glossary/sst/skyrim/` | Skyrim SE/AE | xTranslator `.sst` 词典，只读检索 |
| `glossary/eet/fallout4/` | Fallout 4 | ESP-ESM Translator `.eet` 词典，只读检索 |
| `glossary/sst/fallout4/` | Fallout 4 | xTranslator `.sst` 词典，只读检索 |

把词典文件放入对应目录即可，子目录也会被读取。Markdown 词典使用 `English | 简体中文 | 说明` 表格；文本动态词典支持 `.txt`、`.csv` 和 `.dict`。`.sst`、`.eet` 只用于查找既有译名，工作流不会修改或转换原词典。

`mod_terms.md` 只写当前 Mod 已确认的术语。脚本名、EditorID、FormID、文件路径和内部协议值不要当作普通词语翻译；不确定的译名可以直接让 Agent 暂存为待确认项。

某个推荐目录为空或词典无法读取时，Agent 会记录原因并继续汉化，不会因此把整个任务标记为失败。

## 放入 Mod

进入新工作区，把 Mod 压缩包或文件夹放进：

```text
mod/
```

`mod/` 是工作区内的沙盒副本。原始输入不会被直接改写。

Fallout 4 示例可以使用这个名称：

```text
Classic Holstered Weapons - v1.09-46101-1-09-1779912557
```

这是 Fallout 4 Mod，但工作流仍以 marker 为准。不能靠名称把 Skyrim 工作区变成 Fallout 4 工作区。

## 开始或继续汉化

开始处理：

```text
翻译 mod
```

中途暂停后继续：

```text
继续汉化
```

如果 `mod/` 中有多个 Mod，可以指定名称。Agent 会按 marker 中的游戏身份选择流程；缺工具、证据不完整或需要人工确认时，它会暂停并说明原因。

STRINGS/DLSTRINGS/ILSTRINGS 外部字符串表由专用 adapter 清点、导出、写回和复核。Skyrim SE/AE 与 Fallout 4 的字符串表能力目前都为实验级，需完成真实 Mod、xEdit 和游戏内验收后才能提升为稳定级；它们都是受保护二进制，不能当作普通文本编辑，也不能只凭 xTranslator 输出进入 `final_mod`。

当插件把文字放在外部字符串表中时，还必须把插件身份、引用的 string ID、语言、字符串表和各组件 hash 联合验证。Skyrim SE/AE 和 Fallout 4 的联合交付目前都处于实验阶段，只能在显式启用后生成供人工测试的工作区产物。

Fallout 4 Experimental 当前有几条明确边界：

- Fallout 4 的 STRINGS/DLSTRINGS/ILSTRINGS 可以实验性写回；使用外部字符串表的插件必须额外通过插件与字符串表联合验证，未显式启用或证据不完整时流程会暂停。
- `.esl`、带轻量标记的 ESP/ESM，以及实际翻译目标属于 Light master 的记录可以实验性写回。master-style 证据只针对实际目标 owner：翻译当前插件自己创建的记录时，无关第三方 master 缺失不会阻断；仅引用 `.esl` 也不会让普通 full 插件整体降级。`Skyrim.esm`、`Update.esm`、`Fallout4.esm` 等官方 Full master 由仓库内版本化策略确认，不需要复制游戏文件；只有实际目标 owner 的 Light 状态仍未知时，才需要对应工作区 header/hash 证据。
- 只有流程报告某个实际目标 owner 为未知第三方 `.esp/.esm`、且该文件不在 Mod 内时，才把该 owner 的只读副本放入 `work/master_context/<game_id>/`。插件阶段会在翻译前检查这个目标副本的 TES4 header，并按插件相对路径生成独立的哈希绑定证据；目标证据缺失会先阻断该插件，不会等到写回阶段才失败。
- PEX Export 可用；PEX Apply 目前只能生成供检查的工作区副本，不能作为正式汉化交付。如果这个 Mod 必须翻译 PEX 内容，流程会暂停并说明原因。
- BA2 只允许受控安全解包和同路径 loose override，不重打包。
- SWF、GFX、DLL、EXE 只读审计或原样复制，不修改。

工作流按 Mod 实际用到的资源逐项判断。`Experimental` 是当前游戏支持范围的摘要，不等于所有步骤都会失败；只有 Mod 命中未支持或证据不足的资源能力时才会暂停。

### 大型 Mod

输入准备会生成 `qa/<ModName>.scale_assessment.json` 和 `qa/<ModName>.scale_execution.json`。前者说明规模与风险，后者记录实际采用的限额、超时、解包模式、磁盘预检和手动覆盖参数。

- L0/L1：完整准备，操作方式与普通 Mod 相同。
- L2：默认按资源分类，并用 `work/shards/<ModName>/index.json` 记录 checkpoint；中断后可复用源 hash 未变化的文件。
- L2-L4：默认不复制 Textures、Meshes、Sound、Music、Video 等受保护资源，只物化有翻译价值的内容。
- L3/L4：默认生成汉化覆盖包，安装时必须同时保留原 Mod。
- L5：Agent 会要求按模块拆成多个工作区；当前聚合器只合并通过 QA 的普通文本覆盖层。含插件、PEX 或字符串表写回的子项目会暂停，不能丢失受控工具证据后直接合并。

磁盘不足、文件数或单文件大小超过限制时，流程会在写入前停止并给出拆分或选择性处理建议，不会无限运行。

这些限制的判读方法见 [高级用户指南](./ADVANCED_USER_GUIDE.md)。

## 查看进度

随时可以问：

```text
现在进度到哪了？
```

Agent 会读取工作区进度卡，用 `[SMT 进度]`、`[SMT 阻断]` 或 `[SMT 完成]` 回答。`[SMT 完成]` 只表示项目内检查已满足，不包含人工游戏测试结论。

暂停时可以说：

```text
说明现在卡在哪里
```

或：

```text
继续处理暂停的问题
```

普通用户不需要直接修改 `qa/` 中的 JSON 文件。

## 查看产物

每个 Mod 的交付目录是：

```text
out/<ModName>/汉化产出/
```

| 路径 | 用途 |
|---|---|
| `final_mod/` | 当前游戏 Data 根结构下的待测目录；可能是完整副本，也可能是只含译文的覆盖层 |
| `<ModName>_CHS.zip` | 手动导入 Mod 管理器的汉化包 |
| `intermediate/` | 工具输出、overlay 和审计中间件 |
| `package_report.md` | 打包记录 |

查看 `final_mod/meta/manifest.json` 的 `DeliveryMode`：`direct-replacement-final-mod` 表示完整副本，`translation-overlay-package` 表示必须配合原 Mod 使用的汉化覆盖包。

可以直接问：

```text
说明 <ModName> 能不能进入人工游戏测试
```

## 人工游戏测试

只在对应游戏的隔离测试环境中导入 `_CHS.zip` 或 `final_mod/`。Skyrim SE/AE 使用 Skyrim 测试配置；Fallout 4 使用 Fallout 4 测试配置。不要让两个游戏共用同一个工作区或测试配置。

至少检查：

- 游戏能否正常启动和加载存档。
- 菜单、MCM、提示、任务、对话和物品文本是否正常显示。
- 脚本触发、插件加载顺序和 Mod 冲突是否正常。
- 中文是否截断、乱码、漏译或破坏占位符。
- 实际测试包是否与最新 QA 报告对应。

项目内检查和静态反解析不能替代真实游戏验证。公开发布前，还要确认作者授权和平台规则。
