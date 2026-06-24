# Skyrim SE/AE Mod Agent 汉化工作流

| ![Skyrim Mod CHS Translation logo](./logo.png) |
|:--:|

这是一个面向 Windows 环境的 Codex 插件工程，用于《上古卷轴5：天际》Special Edition / Anniversary Edition Mod 简体中文汉化。它不是一个独立 Mod 管理器，而是一套把 Codex、Python 脚本、受控工具适配、工作区状态和 QA 报告组合起来的汉化工作流。

项目目标是让 Skyrim Mod 汉化过程更可维护、可复核、可回滚：每个实际 Mod 汉化任务都在独立工作区中运行，输入、翻译中间件、工具输出、最终交付包和检查报告都保留在项目目录内，避免直接触碰真实游戏目录或 Mod 管理器目录。

## 项目解决什么问题

Skyrim Mod 汉化经常同时涉及插件文本、MCM、Interface 翻译文件、PEX 可见字符串、BSA/BA2 归档、JSON/XML/CSV/TXT 资源以及人工游戏内测试。本项目把这些步骤拆成可追踪的工程流程：

- 识别 Mod 输入和文件类型。
- 提取可翻译文本并保护 FormID、EditorID、脚本名、变量名、路径、文件名、结构 key 和占位符。
- 使用 Codex 进行语义翻译和模型校对。
- 通过受控工具在工作区内生成插件或 PEX 副本，不直接改原始二进制。
- 组装 Skyrim Data 根结构的 `final_mod/` 和可测试的 `_CHS.zip`。
- 生成 QA、来源追踪、覆盖率和阻断报告，判断是否可以进入人工游戏测试。

## 核心设计

项目分为两类目录：

| 类型 | 说明 |
|---|---|
| 插件源仓库 | 保存插件元数据、Skills、Python 脚本、适配器源码、配置模板、文档和 QA 规则 |
| 汉化工作区 | 保存当前 Mod 输入、工具配置、术语表、中间产物、QA 报告和最终输出 |

插件源仓库不应该直接当作某个 Mod 的运行目录。实际汉化任务应初始化到插件仓库外部的独立工作区中。

## 交付物

每个完成到可测试阶段的 Mod 会在工作区内生成：

```text
out/<ModName>/汉化产出/
```

主要内容：

| 输出 | 用途 |
|---|---|
| `final_mod/` | 完整 Skyrim Mod Data 根结构，便于人工检查 |
| `<ModName>_CHS.zip` | 打包好的汉化包，便于手动导入 MO2/Vortex 测试 |
| `intermediate/` | 工具输出、overlay、patch、审计等中间产物 |

工作区还会生成这些状态和排查入口：

| 输出 | 用途 |
|---|---|
| `qa/` | 状态、检查、阻断原因和人工测试辅助报告 |
| `.workflow/` | 用户可见进度卡和结构化进度状态 |
| `traces/` | 本地执行追踪和开发者排查摘要 |

项目内 QA 通过只表示可以进入人工游戏测试，不表示已经在真实游戏中验证通过。

## 适合谁使用

用户指南全部放在根目录：

| 读者 | 目标 | 入口 |
|---|---|---|
| 普通用户 | 把 Mod 放进工作区，拿到可测试的 `_CHS.zip` | [USER_GUIDE.md](./USER_GUIDE.md) |
| 高级用户 | 配置工具、理解暂停原因、查看 QA、判断能否测试 | [ADVANCED_USER_GUIDE.md](./ADVANCED_USER_GUIDE.md) |
| 开发者用户 | 维护插件、脚本、Skills、适配器和 QA 门禁 | [developer_guide.md](./developer_guide.md) |

普通用户不需要阅读 `docs/`、`scripts/`、`skills/`、`adapters/` 或 `.codex-plugin/`。这些目录主要给高级排查和开发维护使用。

## 最短入口

如果你只是想开始一次汉化，先看 [USER_GUIDE.md](./USER_GUIDE.md)。最短流程是：

```text
安装插件 -> 创建工作区 -> 把 Mod 放进 mod/ -> 让 Codex 翻译 mod -> 查看 out/<ModName>/汉化产出/
```

## 安全边界

- 只读取当前工作区内的 `mod/` 输入。
- 只把产物写入工作区内的 `work/`、`source/`、`translated/`、`out/`、`qa/`、`.workflow/` 和 `traces/`。
- 不访问真实 Skyrim、MO2、Vortex、Steam、AppData 或 `Documents/My Games` 目录。
- 不自动安装或启用 Mod。
- 不直接修改原始 `.esp`、`.esm`、`.esl`、`.pex`、`.bsa`、`.ba2`、`.dll`、`.exe`。
- 需要二进制写回时，只能通过受控工具在工作区内生成副本。

## 当前限制

复杂 Mod 可能需要额外工具路径、GUI 工具保存确认、人工审查或游戏内测试。尤其是 ESP/ESM/ESL、PEX、MCM、BSA/BA2 和 GUI 写回场景，是否能自动推进取决于当前工作区的工具配置、输入结构和 QA 结果。

公开发布或长期使用前，还需要确认 Mod 作者授权、平台规则、真实加载环境、MCM 注册、任务/对话/菜单/提示显示，以及 `_CHS.zip` 是否对应最新 QA 报告。没有人工游戏测试，不建议公开发布。

## 仓库地址

- Gitee: [https://gitee.com/iambupu/SkyrimModTranslation](https://gitee.com/iambupu/SkyrimModTranslation)，方便中国用户访问和使用
- GitHub: [https://github.com/iambupu/SkyrimModTranslation](https://github.com/iambupu/SkyrimModTranslation)

## 简单依赖声明

本项目面向 Windows 环境，需要 Python 3 和 Codex。普通文本、压缩包和部分归档可以走项目内自动流程；复杂 ESP/ESM/ESL、PEX、MCM、BSA/BA2 或 GUI 写回场景，可能需要用户自行安装并配置 LexTranslator、xTranslator、.NET SDK、SSEEdit/xEdit、BSAFileExtractor、B.A.E. 或 7-Zip 等外部工具。具体工具是否必需，以当前工作区的检测报告和 Codex 提示为准。
