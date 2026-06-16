# Skyrim SE/AE Mod 自动化汉化工程

这个项目的目标是：用户只需要在 Codex 中对话，就可以让 Codex 自动完成 Skyrim SE/AE Mod 汉化。

你把待汉化的 Mod 放进项目内的 `mod/` 目录，然后在 Codex 里说“翻译 mod”或说明要处理哪个 Mod。Codex 会在项目目录内自动完成扫描、解包、文件类型判断、文本提取、翻译、工具写回、质量检查、`final_mod` 组装和 `_CHS.zip` 打包。你不需要手动整理译表、逐个运行脚本，也不需要直接编辑插件或脚本二进制。

## 你需要做什么

1. 把待处理的 Mod 压缩包或 Mod 文件夹放到：

```text
mod/
```

2. 打开 Codex，对它说：

```text
翻译 mod
```

也可以更具体一些：

```text
翻译 <ModName> 这个 mod
```

3. 等 Codex 完成后，到输出目录检查结果：

```text
out/<ModName>/汉化产出/
```

4. 用生成的 `final_mod/` 或 `<ModName>_CHS.zip` 在你自己的 MO2/Vortex 配置里测试。

Codex 不会自动安装到真实游戏目录，也不会操作真实 MO2/Vortex 配置。游戏内测试仍然由你自己完成。

## Codex 会自动做什么

Codex 会按项目规则自动完成这些工作：

- 扫描 `mod/` 中的输入。
- 只在项目目录内解包和处理文件。
- 判断 ESP/ESM/ESL、PEX、MCM、Interface、JSON、XML、CSV、TXT 等不同文件类型。
- 优先使用项目内 decoder/CLI 路径处理可自动写回的文件。
- 必要时使用 LexTranslator 或 xTranslator 作为后备工具，但必须把结果保存到项目内输出目录。
- 翻译玩家可见文本，并保护 FormID、EditorID、脚本名、变量名、路径、文件名、占位符和结构 key。
- 对 ESP/PEX 这类二进制文件，只使用受控工具生成项目内副本；Codex 不直接改写二进制。
- 自动组装完整汉化 Mod 目录。
- 自动生成 `_CHS.zip`。
- 自动运行 QA，检查漏译、占位符、结构破坏、二进制反读、包内容一致性和报告状态。
- 如果遇到无法自动完成的问题，会写入 `qa/` 报告并说明阻断原因。

## 输出在哪里

每个 Mod 的结果都在：

```text
out/<ModName>/汉化产出/
```

主要文件和目录：

```text
out/<ModName>/汉化产出/
├─ final_mod/              完整汉化 Mod 目录
├─ intermediate/           中间产物和翻译词典
├─ <ModName>_CHS.zip       打包好的汉化 Mod
└─ package_report.md       打包记录
```

通常你只需要看这两个：

- `final_mod/`：解包后的完整 Mod，可以人工检查。
- `<ModName>_CHS.zip`：打包好的汉化 Mod，可以作为本地 Mod 导入 MO2/Vortex 测试。

## 结果是否完成

Codex 完成后会生成项目状态报告：

```text
qa/translation_readiness.md
qa/workflow_health.md
qa/project_completion_audit.md
qa/translation_goal_compliance.md
```

你通常只需要看：

```text
qa/translation_goal_compliance.md
```

如果里面显示项目内 QA 已完成，说明 Codex 已经完成项目内能自动证明的汉化、组装和校验。

真实游戏/MO2/Vortex 测试不属于 Codex 自动证明范围。即使项目内 QA 全部通过，发布或长期使用前仍建议你在自己的游戏配置中测试。

## 如果 Codex 说 blocked

`blocked` 表示某一步无法可靠自动完成。常见原因包括：

- 缺少某个 decoder 或本地工具。
- 压缩包格式暂不支持自动解包。
- GUI 工具无法自动保存到项目内目录。
- 某些 PEX/插件文本风险过高，需要人工确认。
- QA 发现漏译、占位符损坏、结构错误或包内容不一致。

遇到这种情况，先看：

```text
qa/translation_readiness.md
qa/workflow_health.md
qa/translation_issue_log.md
```

然后继续在 Codex 中对话，例如：

```text
继续处理 blocked 的问题
```

或：

```text
说明现在卡在哪里
```

## 安全边界

本项目只处理项目目录内的文件。

Codex 不会：

- 访问真实 Skyrim 游戏目录。
- 访问真实 MO2/Vortex 目录。
- 自动安装 Mod。
- 直接修改原始 `.esp/.esm/.esl/.pex/.bsa/.ba2`。
- 直接改写 `.psc` 后重新编译。
- 把人工操作伪装成自动化完成。

Codex 可以：

- 从项目内 `mod/` 沙盒读取 Mod 输入。
- 在项目内 `work/`、`translated/`、`out/`、`qa/` 生成中间结果和报告。
- 在项目内协助安装或配置 `tools/` 下需要的工具依赖，或读取用户已经安装并写入配置的工具路径。
- 使用受控工具生成项目内插件/PEX 输出副本。
- 把已验证输出组装进 `final_mod/`。
- 生成项目内 `_CHS.zip`。

## 第一次使用

基础依赖：

```console
python -m pip install -r requirements.txt
```

`requirements.txt` 目前包含 `py7zr`，用于处理 `.7z` 压缩包。

推荐依赖：

- Python：运行 Codex 项目脚本、QA 和自动化流程。
- .NET SDK：运行或构建 Mutagen 相关适配器。可以让 Codex 安装到 `tools/dotnet-sdk/`，也可以自行安装后配置路径。
- Mutagen 适配器：用于 ESP/ESM/ESL 文本导出、写回、验证，以及 PEX 可见字符串导出/写回。项目已有对应入口，通常由 Codex 在项目内调用。

GUI 后备工具：

- LexTranslator：当非 GUI decoder/CLI 路径不可用时，作为插件、PEX 或字符串处理的后备工具。
- xTranslator：用于精修、查漏、复杂导入或 PapyrusPex 后备。

按 Mod 类型可选：

- SSEEdit/xEdit 或 SSEDump 安全包装器：用于插件文本辅助导出和交叉验证。
- Champollion 或其他 PEX 工具：用于 PEX/PSC 只读分析或后备解码。
- BSA 解包器：用于 `.bsa` 归档内容审计。
- BA2 解包器：用于 `.ba2` 归档内容审计。
- 7-Zip CLI：作为 `.7z` 解包后备；首选仍是 Python `py7zr`。

如果需要使用本机 LexTranslator、xTranslator、7z、.NET SDK 或其他工具，把路径写到：

```text
config/tools.local.json
```

`tools/` 目录下需要的依赖可以让 Codex 帮忙在项目内安装或配置；你也可以自行安装这些工具，然后把实际路径写入 `config/tools.local.json`。LexTranslator、xTranslator、SSEEdit/xEdit 等第三方 GUI 工具通常需要你自己从工具作者页面下载并确认许可。

`config/tools.example.json` 只是示例。Codex 不会把示例路径当成真实路径使用。

## 常用对话示例

```text
翻译 mod
```

```text
翻译 mod，如果遇到问题就记下来
```

```text
继续处理 qa 里记录的问题
```

```text
检查现在有哪些 mod 已经 ready
```

```text
重新跑 <ModName> 的 QA
```

```text
说明这个汉化包能不能测试了
```

## 给维护者看的说明

普通用户不需要直接阅读这些内容。维护流程、扩展工具或修改规则时再看：

- `AGENTS.md`：Codex 的项目边界和硬规则。
- `.codex/skills/`：Codex 执行汉化时使用的 Skill。
- `docs/`：工具适配、流程设计和补充说明。
- `scripts/`：Python 主流程、工具适配器和 QA 门禁。

项目不维护 shell 包装脚本。Windows 会话里统一通过 Python 入口运行流程。

## 目录速查

```text
mod/                         待处理 Mod 输入
work/extracted_mods/          项目内解包工作区
work/normalized/              规范化中间译表
translated/                   准备导入或已翻译的文本
out/<ModName>/tool_outputs/   受控工具生成的插件/PEX 输出
out/<ModName>/汉化产出/       最终交付目录
qa/                           QA、状态和问题报告
config/                       本地工具配置模板
```

## 发布前提醒

Codex 可以完成项目内汉化、打包和静态 QA，但不能替你确认：

- Mod 作者授权。
- Nexus/其他平台发布权限。
- 你的真实游戏环境是否有冲突。
- 所有脚本功能在实机里都正常触发。

公开发布或长期使用前，请自行确认授权并完成游戏内测试。
