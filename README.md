# Skyrim SE/AE Mod 汉化工作流

| ![Skyrim Mod CHS Translation logo](./logo.png) |
|:--:|

这是一个给 Skyrim SE/AE Mod 做简体中文汉化的本地工作流。

你把 Mod 放进一个独立工作区，agent 会在这个工作区里提取文本、写译文、调用已配置的工具、检查结果，最后生成可以手动测试的 `final_mod/` 和 `_CHS.zip`。它不会直接碰你的真实游戏目录，也不会自动改 MO2/Vortex 里的文件。

默认用 Codex 插件。opencode 和 Claude Code 也能处理不用桌面工具的部分；遇到 LexTranslator、xTranslator 或窗口操作时，仍然回到 Codex。

## 快速开始

推荐 Codex，安装插件：

```powershell
codex plugin marketplace add iambupu/SkyrimModTranslation --ref master
codex plugin add skyrim-mod-chs-translation --marketplace skyrim-mod-chs
```

然后让 Codex 创建工作区：

```text
帮我在 D:\SkyrimCHS\MyMod 初始化一个新的天际 Mod 汉化工作区，并自动准备非 GUI 工具
```

把要汉化的 Mod 压缩包或文件夹放进新工作区的 `mod/`，再在这个工作区里说：

```text
翻译 mod
```

输出在：

```text
out/<ModName>/汉化产出/
```

里面主要看两个东西：

- `final_mod/`：完整 Skyrim Mod Data 根结构，方便人工检查。
- `<ModName>_CHS.zip`：打包好的汉化包，方便手动导入 MO2/Vortex 测试。

## 它会帮你做什么

很多 Skyrim Mod 不只有一份文本表。一个包里可能同时有插件文本、MCM、Interface 翻译文件、PEX 可见字符串、BSA/BA2 归档和 JSON/XML/CSV/TXT 资源。

这个工作流会尽量把这些内容拆出来处理：

- 找出 `mod/` 里的可处理文件。
- 提取可翻译文本，同时保护 FormID、EditorID、脚本名、变量名、路径、文件名、结构 key 和占位符。
- 用 agent 模型翻译和校对。
- 通过受控工具在工作区里生成插件或 PEX 副本，不直接改原始二进制。
- 组装 `final_mod/` 和 `_CHS.zip`。
- 生成 QA、来源追踪、覆盖率和阻断报告，告诉你能不能进入人工游戏测试。

项目内 QA 通过，只能说明可以开始人工游戏测试，不等于真实游戏里已经验证通过。

## agent工具怎么选

| agent | 适合什么 |
|---|---|
| Codex Plugin | 默认选择。能处理 GUI 后备流程。 |
| opencode | 命令行入口，只处理非 GUI 步骤。 |
| Claude Code | Claude Code 入口，只处理非 GUI 步骤。 |

opencode 可以用插件源码仓库里的初始化脚本启动：

```powershell
uv run scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

没有 uv 时，改用：

```powershell
python scripts\init_opencode.py D:\SkyrimCHS\YourWorkspace
```

Claude Code 使用自己的 `/plugin` 安装入口：

```text
/plugin marketplace add iambupu/SkyrimModTranslation@master
/plugin install skyrim-mod-chs-translation@skyrim-mod-chs
```

opencode 和 Claude Code 不是 GUI 工具入口。需要 LexTranslator、xTranslator、Computer Use 或窗口操作时，用 Codex。

## 你需要知道的边界

- 只读取当前工作区里的 `mod/`。
- 只把产物写入工作区里的 `work/`、`source/`、`translated/`、`out/`、`qa/`、`.workflow/` 和 `traces/`。
- 不访问真实 Skyrim、MO2、Vortex、Steam、AppData 或 `Documents/My Games` 目录。
- 不自动安装或启用 Mod。
- 不直接修改原始 `.esp`、`.esm`、`.esl`、`.pex`、`.bsa`、`.ba2`、`.dll`、`.exe`。
- 需要二进制写回时，只能通过受控工具在工作区里生成副本。

复杂 Mod 可能会卡在工具路径、GUI 保存、人工审查或游戏内测试上。遇到这种情况，agent 应该暂停并说明原因，而不是假装已经完成。

公开发布或长期使用前，还要确认作者授权、平台规则、真实加载环境、MCM 注册、任务/对话/菜单/提示显示，以及 `_CHS.zip` 是否对应最新 QA 报告。没有人工游戏测试，不建议发布。

## 文档怎么读

| 文档 | 适合谁 |
|---|---|
| README | 先了解项目，按最短路径跑起来。 |
| [USER_GUIDE.md](./USER_GUIDE.md) | 普通用户。看安装、建工作区、放 Mod、开始汉化、查看输出。 |
| [ADVANCED_USER_GUIDE.md](./ADVANCED_USER_GUIDE.md) | 高级用户。看工具配置、opencode / Claude Code、报告和测试边界。 |
| [developer_guide.md](./developer_guide.md) | 维护者。看脚本、Skills、适配器、状态机、QA、CI 和发布。 |

普通用户不用读 `docs/`、`scripts/`、`skills/`、`adapters/` 或 `.codex-plugin/`。

## 环境要求

- Windows
- Python 3
- 至少一个 agent 入口，推荐先用 Codex
- 可选：uv

普通文本、压缩包和部分归档可以走自动流程。复杂 ESP/ESM/ESL、PEX、MCM、BSA/BA2 或 GUI 写回，可能还需要 LexTranslator、xTranslator、.NET SDK、SSEEdit/xEdit、BSAFileExtractor、B.A.E. 或 7-Zip。具体缺什么，看当前工作区的检测报告。

## 仓库地址

- Gitee: [https://gitee.com/iambupu/SkyrimModTranslation](https://gitee.com/iambupu/SkyrimModTranslation)
- GitHub: [https://github.com/iambupu/SkyrimModTranslation](https://github.com/iambupu/SkyrimModTranslation)
