# Skyrim SE/AE Mod 汉化工作流

| ![Skyrim Mod CHS Translation logo](./logo.png) |
|:--:|

这是一个在 Windows 本地运行的 Bethesda Mod 简体中文汉化工作流。Skyrim SE/AE 提供稳定完整支持；项目也提供 **Fallout 4 Experimental Support**，未认证或暂不支持的能力会明确阻断。新工作区不会默认选择游戏。

Mod 只从独立工作区的 `mod/` 读取。译文、检查报告和交付包也只写回工作区，不访问真实游戏目录，不自动修改 MO2/Vortex 中的文件。

## 环境要求

- Windows。
- Python 3.11 或更高版本。
- Codex、opencode 或 Claude Code；推荐使用 Codex。
- 可选：uv。

复杂 Mod 需要哪些额外工具，由当前工作区的检测报告决定。

## 安装

在 PowerShell 中安装 Codex 插件：

```powershell
codex plugin marketplace add iambupu/SkyrimModTranslation --ref master
codex plugin add skyrim-mod-chs-translation --marketplace skyrim-mod-chs
```

## 快速开始

安装插件后，直接告诉 Agent 要创建的工作区位置和游戏。Skyrim SE/AE：

```text
帮我在 D:\SkyrimCHS\MyMod 创建一个天际SE Mod 汉化工作区，并自动准备工具。
```

Fallout 4 Experimental：

```text
帮我在 D:\Fallout4CHS\MyMod 创建一个辐射4 汉化工作区，并自动准备工具。
```

如果没有说明游戏，Agent 会先询问并等待确认，不会根据 Mod 名或目录名猜测。

工作区建好后，把 Mod 压缩包或文件夹放进工作区的 `mod/`，然后在该工作区中说：

```text
翻译 mod
```

词典不是开始翻译的前置条件，但强烈推荐提供当前游戏的词典和 Mod 术语表。缺少词典时流程会继续，术语一致性通常会下降。

交付结果位于：

```text
out/<ModName>/汉化产出/
```

其中 `final_mod/` 保持当前游戏的 Data 根结构，`<ModName>_CHS.zip` 用于手动导入 Mod 管理器测试。

## 兼容性

| 能力 | Skyrim SE/AE | Fallout 4 Experimental |
|---|---|---|
| loose text、Interface、MCM | 支持 | 支持，按 Game Profile 校验 |
| ESP/ESM 中的名称和描述 | 支持 | 支持已验证的常见字段 |
| ESL / light FormID | 支持 | 仅允许只读 inventory；受控写回暂时阻断 |
| STRINGS/DLSTRINGS/ILSTRINGS 外部文本文件 | 支持，需要 Codex 使用 xTranslator 处理 | 暂不支持，检测到后暂停 |
| PEX Export | 支持 | 支持 |
| PEX Apply | 支持 | 暂不能正式交付；只能生成供检查的工作区副本 |
| BSA | 审计、受控解包、loose override | 当前 profile 不适用 |
| BA2 | 只读 inventory | 审计、受控安全解包、loose override；不重打包 |
| 材质、网格、纹理、音频和视频资源 | 原样保留 | 原样保留，不修改 |
| SWF、GFX、DLL、EXE | 不修改 | 不修改 |
| 游戏内验证 | 人工完成 | 人工完成，不视为已认证 |

项目 QA 通过只表示可以进入人工游戏测试，不代表已经在真实游戏中验证。

表中的“稳定/实验性”是整体说明。实际执行会逐项检查当前 Game Profile 对插件文本、PEX、BSA/BA2、外部字符串表和 loose text 的能力；某一项未支持时只阻断依赖该项的 Mod，不会靠游戏名或整体支持级别猜测放行。

## agent 入口

Codex 是完整入口，能够在需要时使用桌面工具。opencode 和 Claude Code 是非 GUI 顶层入口；遇到 LexTranslator、xTranslator、Computer Use 或窗口操作时，必须交回 Codex。

## 文档

| 文档 | 内容 |
|---|---|
| README | 了解项目、支持范围和快速开始。 |
| [USER_GUIDE.md](./USER_GUIDE.md) | 安装、选择游戏、日常汉化、查看产物和人工测试。 |
| [ADVANCED_USER_GUIDE.md](./ADVANCED_USER_GUIDE.md) | 工具配置、实验性能力边界、报告判读和恢复。 |
| [developer_guide.md](./developer_guide.md) | 架构、状态机、测试、扩展和发布维护。 |

Fallout 4 的精确能力合同见 [Fallout 4 Experimental Support](./docs/fallout4_experimental_support.md)。普通用户不需要阅读 `scripts/`、`skills/` 或 `adapters/`。

## 仓库地址

- Gitee：[SkyrimModTranslation](https://gitee.com/iambupu/SkyrimModTranslation)
- GitHub：[SkyrimModTranslation](https://github.com/iambupu/SkyrimModTranslation)
