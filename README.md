# Skyrim SE/AE Mod 汉化工作流

| ![Skyrim Mod CHS Translation logo](./logo.png) |
|:--:|

这是一个在 Windows 本地运行的 Bethesda Mod 简体中文汉化工作流。Skyrim SE/AE 是默认完整入口；项目也提供 **Fallout 4 Experimental Support**，未认证或暂不支持的能力会明确阻断。

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

## 最短使用路径

在插件源码目录运行初始化命令。未指定游戏时，默认创建 Skyrim SE/AE 工作区：

```powershell
python scripts\init_workspace.py D:\SkyrimCHS\MyMod --tool-setup auto
```

Fallout 4 工作区必须显式选择游戏：

```powershell
python scripts\init_workspace.py D:\Fallout4CHS\MyMod --game fallout4 --tool-setup auto
```

也可以直接让 Codex 按对应游戏初始化。工作区建好后，把 Mod 压缩包或文件夹放进 `mod/`，在该工作区中说：

```text
翻译 mod
```

交付结果位于：

```text
out/<ModName>/汉化产出/
```

其中 `final_mod/` 保持当前游戏的 Data 根结构，`<ModName>_CHS.zip` 用于手动导入 Mod 管理器测试。

## 兼容性

| 能力 | Skyrim SE/AE | Fallout 4 Experimental |
|---|---|---|
| loose text、Interface、MCM | 支持 | 支持，按 Game Profile 校验 |
| 非 localized ESP/ESM/ESL 白名单字段 | 支持 | 支持，写回后反解析验证 |
| localized plugin / STRINGS | 按 Skyrim 流程处理 | 检测后阻断 |
| PEX Export | 支持 | 支持 |
| PEX Apply | 支持 | Experimental；可生成并验证工作区副本，但 strict completion 固定阻断 |
| BSA | 审计、受控解包、loose override | 当前 profile 不适用 |
| BA2 | 只读 inventory | 审计、受控安全解包、loose override；不重打包 |
| SWF、GFX、DLL、EXE | 不修改 | 不修改 |
| 游戏内验证 | 人工完成 | 人工完成，不视为已认证 |

项目 QA 通过只表示可以进入人工游戏测试，不代表已经在真实游戏中验证。

## agent 入口

Codex 是完整入口，能够在需要时使用桌面工具。opencode 和 Claude Code 是非 GUI 顶层入口；遇到 LexTranslator、xTranslator、Computer Use 或窗口操作时，必须交回 Codex。

## 文档

| 文档 | 内容 |
|---|---|
| README | 了解项目、支持范围和最短使用路径。 |
| [USER_GUIDE.md](./USER_GUIDE.md) | 安装、选择游戏、日常汉化、查看产物和人工测试。 |
| [ADVANCED_USER_GUIDE.md](./ADVANCED_USER_GUIDE.md) | 工具配置、实验性能力边界、报告判读和恢复。 |
| [developer_guide.md](./developer_guide.md) | 架构、状态机、测试、扩展和发布维护。 |

Fallout 4 的精确能力合同见 [Fallout 4 Experimental Support](./docs/fallout4_experimental_support.md)。普通用户不需要阅读 `scripts/`、`skills/` 或 `adapters/`。

## 仓库地址

- Gitee：[SkyrimModTranslation](https://gitee.com/iambupu/SkyrimModTranslation)
- GitHub：[SkyrimModTranslation](https://github.com/iambupu/SkyrimModTranslation)
