# Bethesda Mod 简体中文汉化工作流

| ![Skyrim Mod CHS Translation](./logo.png) |
|:--:|

这是一个在 Windows 本地运行、由 Agent 驱动的 Bethesda Mod 汉化工作流。目前稳定支持 **Skyrim SE/AE**，并提供 **Fallout 4 Experimental Support（实验性支持）**。

工作流会识别 Mod 中的可翻译内容，调用受控工具，生成译文和检查报告，最后整理待人工游戏测试的汉化包。它不会直接改写原始 Mod，也不会访问真实游戏、MO2 或 Vortex 目录。

## 准备环境

- Windows。
- Python 3.11 或更高版本。
- Codex、opencode 或 Claude Code；只有 Codex 可以执行桌面工具步骤。
- 本仓库源码和待汉化 Mod 的目录、ZIP 或 7Z 副本。

复杂 Mod 可能需要 .NET 8 SDK、LexTranslator、xTranslator 或其他解码工具。`run` 默认使用 `--tool-setup auto` 检测并准备受控非 GUI 工具；缺失桌面工具时会给出明确提示。

翻译依赖包括 [Mutagen](https://github.com/Mutagen-Modding/Mutagen) `0.53.1`、[bethesda-structs](https://pypi.org/project/bethesda-structs/) `>=0.1.4` 和 [py7zr](https://pypi.org/project/py7zr/) `>=1.1.0`。自动准备还会使用固定源码快照和 hash 校验的 [BSAFileExtractor](https://github.com/Sw4T/BSAFileExtractor) 与 [Champollion](https://github.com/Orvid/Champollion)。这些组件保留各自许可证，第三方工具自身能力不等于本项目已经认证对应写回能力。

## 安装 Agent

### Codex

```powershell
codex plugin marketplace add iambupu/SkyrimModTranslation --ref master
codex plugin add skyrim-mod-chs-translation --marketplace skyrim-mod-chs
```

opencode 和 Claude Code 使用相同的工作区、翻译规则和 QA 门禁。遇到 GUI-only 步骤时，它们会返回 `needs_gui` 并提示交给 Codex，不会把未完成操作伪装成成功。

## 唯一公开入口

普通用户和顶层 Agent 只使用 `python scripts\smt.py`。用户说“翻译 mod”时也由 Agent 调用这个入口。第一次运行默认在 Windows 文档目录下的 `Documents/SkyrimModTranslationWorkspaces` 为每个新输入创建一个新工作区；相同内容和身份的同一输入会复用已登记工作区，不会复用无关旧工作区。没有说明游戏时，Agent 会先询问并等待确认，不按 Mod 名猜测。

五个公开命令如下：

```powershell
python scripts\smt.py run "D:\Mods\ExampleMod.zip" --game skyrim-se
python scripts\smt.py status
python scripts\smt.py resume
python scripts\smt.py doctor
python scripts\smt.py output
```

`run` 接受 Mod 目录、ZIP 或 7Z。Fallout 4 使用 `--game fallout4`。默认工具模式为 `--tool-setup auto`；也可选择 `manual` 或 `skip`。需要创建一个固定的汉化工作区时可以传入 `--workspace "D:\SkyrimCHS\MyMod"`；Fallout 4 可使用 `D:\Fallout4CHS\MyMod`。路径不存在或为空时可初始化，非空但不是匹配工作区时会安全停止且绝不清空目录。

例如，使用目录输入、显式新工作区和手动工具模式：

```powershell
python scripts\smt.py run "D:\Mods\ExampleMod" --game skyrim-se --workspace "D:\SkyrimCHS\MyMod" --tool-setup manual
```

`status` 只读取最近一次生成的状态快照，不刷新状态；`resume` 只推进当前 session 中获授权的低风险动作；`doctor` 是只读诊断，不安装、清理或修复；`output` 显示 `final_mod`、`intermediate` 和 `_CHS.zip` 路径。产物尚不存在时，普通 `output` 仍成功并明确标记不存在。

## 公开结果

| outcome | 含义 |
|---|---|
| `completed` | 当前 session 已达到 `manual_tested`，并且人工游戏测试证据仍有效 |
| `ready_for_manual_test` | 项目内 QA 已通过，可以进入人工游戏测试，但尚未证明实机验证完成 |
| `needs_agent_translation` | 等待 Agent 生成译文、语义校对或翻译决策 |
| `needs_gui` | 等待 Codex 执行获授权桌面工具步骤 |
| `needs_user_input` | 等待用户选择文件、游戏身份、术语或其他明确输入 |
| `blocked` | QA、安全、能力或任务失败导致当前无法继续 |

`output` 会分别显示：

- 可以进入人工游戏测试：是/否。
- 人工游戏测试已验证：是/否。

因此 `ready_for_manual_test` 不能写成 `completed`，项目内静态 QA 也不能代替真实游戏测试。

## 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 成功、`completed`、`ready_for_manual_test` 或无事可做的 no-op |
| `1` | 普通读取、打开路径或内部失败 |
| `2` | argparse 参数错误 |
| `3` | Agent、GUI、用户输入或普通安全暂停 |
| `4` | 输入格式或资源能力不支持 |
| `5` | 工具或运行环境不可用 |
| `6` | session、marker 或工作区身份冲突 |
| `124` | 子进程或整条命令超时 |
| `130` | 用户中断 |

Agent 必须读取 JSON 中的 `outcome`，不能把所有非零退出码理解成底层失败。顶层 Agent 首次执行 `python scripts\smt.py --format json run ...`，完成 JSON `next_action` 指定的语言或获授权 GUI 工作后，只调用对应的 `resume`、`status`、`doctor` 或 `output` 公开命令。

## 产物位置

每个 Mod 的公开路径为：

```text
out/<ModName>/汉化产出/final_mod/
out/<ModName>/汉化产出/intermediate/
out/<ModName>/汉化产出/<ModName>_CHS.zip
```

`final_mod/` 保持当前 Game Profile 的 Data 根结构。它可能是完整副本，也可能是只包含已验证译文、需要原 Mod 的覆盖层；以 manifest 中的 `DeliveryMode` 为准。

## 支持范围

| 内容 | Skyrim SE/AE | Fallout 4 实验性支持 |
|---|---|---|
| 普通文本、界面文本、MCM 配置 | 支持 | 支持 |
| ESP/ESM 中直接保存的名称和描述 | 支持 | 支持已验证的常见字段 |
| ESL 及带轻量 FormID 的插件 | 实验性受控写回；只对实际目标 owner 要求证据 | 实验性受控写回；只对实际目标 owner 要求证据 |
| STRINGS/DLSTRINGS/ILSTRINGS | 专用 adapter 实验性受控写回 | 专用 adapter 实验性受控写回 |
| 文字存放在外部字符串表中的插件 | 插件与字符串表联合交付处于实验阶段 | 插件与字符串表联合交付处于实验阶段 |
| Papyrus PEX（PEX Apply） | 支持提取和受控写回 | 只自动写回已验证直接字面量；其他情况阻断正式交付 |
| 游戏资源归档 | BSA 可审计、受控解包并生成 loose override | BA2 可审计、受控解包并生成 loose override；不重打包 |
| 材质、网格、纹理、音频和视频资源 | 原样保留 | 原样保留 |
| SWF、GFX、DLL、EXE | 不修改 | 不修改 |

官方 Full master 由版本化策略识别，不要求用户复制 `Skyrim.esm`、`Update.esm` 或 `Fallout4.esm`。无关第三方 master 缺失也不会阻断当前插件自己的翻译目标。

## 文档

| 文档 | 适合谁阅读 |
|---|---|
| [普通用户指南](./USER_GUIDE.md) | 五个公开命令、工作区、状态、产物和人工测试 |
| [高级用户指南](./ADVANCED_USER_GUIDE.md) | 工具配置、实验能力、报告判读和内部诊断 |
| [开发者指南](./developer_guide.md) | 架构、状态机、测试、扩展和发布维护 |

Fallout 4 的详细支持边界见 [Fallout 4 实验性支持说明](./docs/fallout4_experimental_support.md)。

项目仓库：[Gitee](https://gitee.com/iambupu/SkyrimModTranslation) · [GitHub](https://github.com/iambupu/SkyrimModTranslation)
