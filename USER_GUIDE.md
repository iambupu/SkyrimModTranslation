# 普通用户指南

这份指南覆盖一次汉化的日常流程：安装、选择游戏、创建工作区、放入 Mod、开始或继续处理、查看产物，以及人工游戏测试。工具和报告细节见 [高级用户指南](./ADVANCED_USER_GUIDE.md)。

## 准备环境

- Windows。
- Python 3.11 或更高版本。
- Codex。opencode 和 Claude Code 只能处理非 GUI 步骤。
- 本仓库源码。
- 要汉化的 Skyrim SE/AE 或 Fallout 4 Mod 副本。

不要把真实游戏目录或 MO2/Vortex 目录作为输入。

## 安装 Codex 插件

打开 PowerShell，运行：

```powershell
codex plugin marketplace add iambupu/SkyrimModTranslation --ref master
codex plugin add skyrim-mod-chs-translation --marketplace skyrim-mod-chs
```

安装完成后，用 Codex 打开插件源码目录或准备好的工作区。

## 选择游戏

游戏身份在创建工作区时确定：

- 不传 `--game` 时，默认使用 `skyrim-se`。旧工作区没有 `game_id` 时也按 Skyrim SE/AE 兼容。
- Fallout 4 必须显式使用 `--game fallout4`。

初始化后，`.skyrim-chs-workspace.json` 是游戏身份的权威来源。流程不按 Mod 名、目录名或文件名猜游戏，也不会因为后来放入了另一个游戏的文件而自动切换。

## 创建工作区

工作区必须位于插件源码仓库之外，目标路径应当不存在或为空。

Skyrim SE/AE：

```powershell
python scripts\init_workspace.py D:\SkyrimCHS\MyMod --tool-setup auto
```

Fallout 4 Experimental：

```powershell
python scripts\init_workspace.py D:\Fallout4CHS\MyMod --game fallout4 --tool-setup auto
```

也可以告诉 Codex：

```text
帮我在 D:\Fallout4CHS\MyMod 初始化 Fallout 4 实验性汉化工作区，并自动准备非 GUI 工具
```

`--tool-setup auto` 只准备受控的非 GUI 工具。需要桌面程序时，Codex 会说明还缺什么。

## 放入 Mod

用 Codex 打开新工作区，把 Mod 压缩包或文件夹放进：

```text
mod/
```

`mod/` 是工作区内的沙盒副本。原始输入不会被直接改写。

Fallout 4 示例可以使用这个名称：

```text
Classic Holstered Weapons - v1.09-46101-1-09-1779912557
```

仓库回归测试使用的是同名合成 fixture，只验证 Game Profile、路由和保护规则。它不代表该 Mod 的真实二进制已测试，也不能靠这个名称把 Skyrim 工作区变成 Fallout 4 工作区。

## 开始或继续汉化

开始处理：

```text
翻译 mod
```

中途暂停后继续：

```text
继续汉化
```

如果 `mod/` 中有多个 Mod，可以指定名称。Codex 会按 marker 中的游戏身份选择流程；缺工具、证据不完整或需要人工确认时，它会暂停并说明原因。

Fallout 4 Experimental 当前有几条明确边界：

- localized plugin 和 STRINGS 家族会阻断。
- PEX Export 可用；PEX Apply 需要实验性授权和严格门禁，缺少认证证据时会阻断。
- BA2 只允许受控安全解包和同路径 loose override，不重打包。
- SWF、GFX、DLL、EXE 只读审计或原样复制，不修改。

这些限制的判读方法见 [高级用户指南](./ADVANCED_USER_GUIDE.md)。

## 查看进度

随时可以问：

```text
现在进度到哪了？
```

Codex 会读取工作区进度卡，用 `[SMT 进度]`、`[SMT 阻断]` 或 `[SMT 完成]` 回答。`[SMT 完成]` 只表示项目门禁已满足，不包含人工游戏测试结论。

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
| `final_mod/` | 当前游戏 Data 根结构下的完整待测目录 |
| `<ModName>_CHS.zip` | 手动导入 Mod 管理器的汉化包 |
| `intermediate/` | 工具输出、overlay 和审计中间件 |
| `package_report.md` | 打包记录 |

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

项目内 QA、合成 fixture 和静态反解析都不能替代真实游戏验证。公开发布前，还要确认作者授权和平台规则。
