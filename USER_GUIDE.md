# 普通用户指南

这份指南面向只想完成一次 Mod 汉化并拿到可测试压缩包的用户。你不需要理解状态机、Skills、适配器、QA JSON 或插件内部结构。

本文默认使用 Codex 这个完整入口，因为它同时支持插件调用和 GUI 后备。opencode 和 Claude Code 也可以作为非 GUI 入口使用，安装和边界见 [README.md](./README.md) 的对应章节。

## 你需要准备什么

- Windows。
- Python 3。
- Codex，或 README 中列出的其他受支持 agent 入口。普通完整体验推荐 Codex。
- 本仓库源码。
- 要汉化的 Skyrim SE/AE Mod 压缩包或文件夹。

复杂 Mod 可能需要 LexTranslator、xTranslator、.NET SDK、BSA 工具或 7-Zip。第一次使用时可以先不手动配置，让 Codex 自动检查。

## 第一次安装

推荐用 Codex repo marketplace 直接安装。打开 PowerShell，运行：

```powershell
codex plugin marketplace add iambupu/SkyrimModTranslation --ref master
codex plugin add skyrim-mod-chs-translation --marketplace skyrim-mod-chs
```

当前插件版本为 `0.2.1`，默认从 `master` 分支安装。

如果你不想手动输入命令，也可以用 Codex 打开本仓库，然后说：

```text
帮我安装这个 Skyrim 汉化 Codex 插件
```

如果你之前装过，但想刷新本地入口，可以运行：

```powershell
codex plugin marketplace upgrade skyrim-mod-chs
codex plugin remove skyrim-mod-chs-translation --marketplace skyrim-mod-chs
codex plugin add skyrim-mod-chs-translation --marketplace skyrim-mod-chs
```

也可以直接说：

```text
帮我重新安装这个插件，并刷新本地 marketplace 入口
```

如果你以前安装过旧入口 `skyrim-mod-chs-local`，先清理旧入口，再按上面的 repo marketplace 命令安装：

```powershell
codex plugin remove skyrim-mod-chs-translation --marketplace skyrim-mod-chs-local
codex plugin marketplace remove skyrim-mod-chs-local
```

## 创建工作区

工作区是每次实际汉化 Mod 的地方。不要直接在插件源码仓库里放 Mod。

推荐说法：

```text
帮我在 D:\SkyrimCHS\MyMod 初始化一个新的天际 Mod 汉化工作区，并自动准备非 GUI 工具
```

也可以选择手动配置工具：

```text
初始化一个新的工作区，路径是 D:\SkyrimCHS\ManualTools，工具我手动配置
```

工作区必须是插件仓库外部的空目录，或者一个还不存在的新目录。

## 放入 Mod

用 Codex 打开新工作区，然后把要汉化的 Mod 压缩包或文件夹放进：

```text
mod/
```

`mod/` 是项目内沙盒副本，不是真实游戏目录。不要把真实 MO2/Vortex 目录当作输入。

## 开始汉化

在 Codex 里输入：

```text
翻译 mod
```

如果 `mod/` 里有多个 Mod，可以指定名称：

```text
翻译 <ModName> 这个 mod
```

Codex 会扫描、解包、翻译、组装和检查。过程中如果需要补工具路径、人工确认或游戏测试，它会暂停并说明原因。

## 查看进度

可以随时问：

```text
现在进度到哪了？
```

Codex 应只用 `[SMT 进度]`、`[SMT 阻断]` 或 `[SMT 完成]` 这三类进度卡回答。你不需要阅读脚本输出或 trace 日志来判断是否完成。

## 查看输出

每个 Mod 的交付目录是：

```text
out/<ModName>/汉化产出/
```

常见内容：

| 路径 | 用途 |
|---|---|
| `final_mod/` | 完整汉化 Mod 目录，适合人工检查文件结构 |
| `<ModName>_CHS.zip` | 打包好的汉化包，适合手动导入 MO2/Vortex 测试 |
| `intermediate/` | 中间产物，一般不用看 |
| `package_report.md` | 打包记录 |

## 如果 Codex 暂停

暂停不是失败，通常表示继续自动推进可能不安全。你可以直接说：

```text
说明现在卡在哪里
```

或：

```text
继续处理暂停的问题
```

常见原因包括：

- 缺少本机工具路径。
- 压缩包或归档需要额外解包工具。
- GUI 工具无法自动保存到工作区内目录。
- 插件或 PEX 文本风险较高，需要人工确认。
- 检查发现漏译、占位符损坏、结构错误或来源缺失。
- 已经到达需要人工游戏测试的阶段。

普通用户不需要自己判断内部状态文件。让 Codex 读取报告并解释下一步即可。

## 判断能不能测试

可以直接问：

```text
说明 <ModName> 能不能进游戏测试
```

如果 Codex 说明可以测试，就手动把 `_CHS.zip` 或 `final_mod/` 导入你的 MO2/Vortex 测试环境。

项目内检查通过只表示文件结构、翻译覆盖、来源追踪和静态 QA 允许进入人工测试。Skyrim 的真实加载顺序、脚本触发、MCM 注册、任务/对话显示和 Mod 冲突仍然需要你在自己的游戏环境里确认。

## 常用对话

```text
现在这个项目应该怎么继续？
```

```text
检查工具配置有没有问题
```

```text
翻译 mod，如果遇到问题就记下来
```

```text
检查现在有哪些 mod 已经可以测试
```

```text
重新检查 <ModName>
```

```text
帮我给 <ModName> 生成进游戏测试计划
```

```text
帮我给 <ModName> 生成测试结果记录模板
```

## 你主要会用到的目录

```text
mod/                         待汉化 Mod 输入
out/<ModName>/汉化产出/       final_mod 和 _CHS.zip
qa/                          状态、问题和检查报告
glossary/                    当前工作区的术语表
```

大多数时候只需要看 `mod/` 和 `out/`。遇到问题时让 Codex 解释 `qa/` 里的报告。
