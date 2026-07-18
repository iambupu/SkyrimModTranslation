# Bethesda Mod 简体中文汉化工作流

| ![Skyrim Mod CHS Translation](./logo.png) |
|:--:|

这是一个在 Windows 本地运行、由 Agent 驱动的 Mod 汉化工作流。目前稳定支持 **Skyrim SE/AE**，并提供 **Fallout 4 Experimental Support（实验性支持）**。

它会检查 Mod 中有哪些可翻译内容，选择对应工具，生成译文和检查报告，最后整理出可供 Mod 管理器测试的汉化包。原始 Mod 不会被直接改写，流程也不会访问真实游戏目录或自动修改 MO2/Vortex。

## 准备环境

- Windows。
- Codex、opencode 或 Claude Code。
- 要汉化的 Skyrim SE/AE 或 Fallout 4 Mod 副本。

复杂 Mod 可能需要 LexTranslator、xTranslator 或其他解码工具。工作流会先检测，再告诉你缺少什么。

## 翻译依赖与引用
- 运行环境：[Python](https://www.python.org/downloads/windows/) 3.11+ 和 [.NET 8 SDK](https://dotnet.microsoft.com/en-us/download/dotnet/8.0)；[uv](https://docs.astral.sh/uv/getting-started/installation/) 可选，不可用时回退到 `venv` 和 `pip`。
- 开源组件：[Mutagen](https://github.com/Mutagen-Modding/Mutagen) `0.53.1` 用于插件解析，[bethesda-structs](https://pypi.org/project/bethesda-structs/) `>=0.1.4` 用于归档清单，[py7zr](https://pypi.org/project/py7zr/) `>=1.1.0` 用于读取 `.7z`；自动准备还会引用固定源码快照和 hash 校验的 [BSAFileExtractor](https://github.com/Sw4T/BSAFileExtractor) 与 [Champollion](https://github.com/Orvid/Champollion)。这些组件保留各自的许可证，第三方工具自身的功能不等于本项目已经认证的写回能力。

## 选择 Agent

| Agent | 适合什么情况 | 接入方式 |
|---|---|---|
| Codex（推荐） | 完整流程；需要时可操作 LexTranslator 或 xTranslator | Codex 插件 |
| opencode | 不需要桌面工具的汉化流程 | 工作区本地插件 |
| Claude Code | 不需要桌面工具的汉化流程 | Claude Code marketplace |

opencode 和 Claude Code 使用同一套工作区、翻译规则和检查标准。遇到必须操作桌面程序的步骤时，它们会暂停并提示交给 Codex，不会把未完成的步骤当作成功。

### Codex

在 PowerShell 中运行：

```powershell
codex plugin marketplace add iambupu/SkyrimModTranslation --ref master
codex plugin add skyrim-mod-chs-translation --marketplace skyrim-mod-chs
```

### opencode

opencode 使用项目生成的工作区本地插件。在仓库目录运行下面的命令，会创建或配置工作区并启动 opencode：

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\MyMod --game skyrim-se --tool-setup auto
```

将 `skyrim-se` 改为 `fallout4` 即可创建 Fallout 4 工作区。

### Claude Code

在 Claude Code 中运行：

```text
/plugin marketplace add iambupu/SkyrimModTranslation@master
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

## 快速开始

### 1. 创建工作区

安装或打开 Agent 后，直接说明工作区位置和游戏：

```text
帮我在 D:\Fallout4CHS\MyMod 创建一个 Fallout 4 Mod 汉化工作区，并自动准备工具。
```

Skyrim SE/AE 只需把游戏名称换成“天际 SE”。如果没有说明游戏，Agent 会先询问并等待确认，不会根据 Mod 名或目录名猜测。

### 2. 放入 Mod

把 Mod 压缩包或文件夹放进新工作区的：

```text
mod/
```

### 3. 开始汉化

在工作区中告诉 Agent：

```text
翻译 mod
```

中途停止后，可以直接说“继续汉化”。工作流会读取已有状态和检查结果，不必从头重新分析。

### 4. 查看结果

交付结果位于：

```text
out/<ModName>/汉化产出/
```

- `final_mod/`：保持游戏 Data 目录结构的待测交付目录。普通 Mod 包含完整副本；大型 Mod 只包含汉化覆盖文件。
- `<ModName>_CHS.zip`：供 MO2、Vortex 等 Mod 管理器手动导入测试。

大型 Mod 会先评估 L0-L5 规模和 R0-R4 风险：L2 起支持 checkpoint 并跳过受保护资源，L3/L4 生成需要原 Mod 的覆盖包，L5 拆分后聚合已验证的文本覆盖层；实际限额不能突破安全上限。词典不是必要条件，但强烈推荐提供当前游戏词典和 Mod 术语表。

## 支持范围
| 内容 | Skyrim SE/AE | Fallout 4 实验性支持 |
|---|---|---|
| 普通文本、界面文本、MCM 配置 | 支持 | 支持 |
| ESP/ESM 中直接保存的名称和描述 | 支持 | 支持已验证的常见字段 |
| ESL 及带轻量 FormID 的插件 | 实验性受控写回；需要工作区内 master style 证据 | 实验性受控写回；需要工作区内 master style 证据 |
| STRINGS/DLSTRINGS/ILSTRINGS 外部字符串表 | 专用 adapter 稳定支持 | 专用 adapter 实验性支持 |
| 文字存放在外部字符串表中的插件 | 插件与字符串表联合交付处于实验阶段 | 插件与字符串表联合交付处于实验阶段 |
| Papyrus PEX（报告中称 PEX Apply） | 支持提取和受控写回 | 可生成供检查的工作区副本，暂不能作为正式交付 |
| 游戏资源归档 | BSA：可审计、受控解包并生成同路径覆盖文件 | BA2：可审计、受控解包并生成同路径覆盖文件；不重打包 |
| 材质、网格、纹理、音频和视频资源 | 原样保留 | 原样保留 |
| SWF、GFX、DLL、EXE | 不修改 | 不修改 |

遇到当前不支持的格式时，流程会明确暂停并说明原因，不会换用其他游戏的处理方式继续运行。

项目内检查全部通过，只表示产物可以进入人工游戏测试，不代表已经在真实游戏中验证。最终仍需用 Mod 管理器安装，并在游戏内检查界面、任务、对话和脚本行为。

## 文档

| 文档 | 适合谁阅读 |
|---|---|
| [普通用户指南](./USER_GUIDE.md) | 安装、创建工作区、开始或继续汉化、查看产物。 |
| [高级用户指南](./ADVANCED_USER_GUIDE.md) | 工具配置、实验性能力、报告判读和失败恢复。 |
| [开发者指南](./developer_guide.md) | 架构、状态机、测试、游戏扩展和发布维护。 |

Fallout 4 的详细支持边界见 [Fallout 4 实验性支持说明](./docs/fallout4_experimental_support.md)。

项目仓库：[Gitee](https://gitee.com/iambupu/SkyrimModTranslation) · [GitHub](https://github.com/iambupu/SkyrimModTranslation)
