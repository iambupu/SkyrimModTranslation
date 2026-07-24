# 普通用户指南

这份指南覆盖一次汉化的日常流程：选择输入和游戏、运行或继续工作流、查看状态与产物，以及人工游戏测试。工具协议和报告细节见 [高级用户指南](./ADVANCED_USER_GUIDE.md)。

## 准备环境

- Windows。
- Python 3.11 或更高版本。
- Codex、opencode 或 Claude Code，任选其一；只有 Codex 可以处理桌面工具步骤。
- 本仓库源码。
- 要汉化的 Skyrim SE/AE 或 Fallout 4 Mod 目录、ZIP 或 7Z 副本。

不要把真实游戏、MO2、Vortex 或其他 Mod 管理器目录作为输入。

## 五个公开命令

普通用户只需要 `python scripts\smt.py` 的五个子命令：

```powershell
python scripts\smt.py run "D:\Mods\ExampleMod.zip" --game skyrim-se
python scripts\smt.py status
python scripts\smt.py resume
python scripts\smt.py doctor
python scripts\smt.py output
```

| 命令 | 用途 | 是否推进工作流 |
|---|---|---|
| `run` | 导入一个输入，创建或复用它的单 Mod 工作区，并推进到稳定结果 | 是 |
| `status` | 读取最近一次已生成的状态快照 | 否 |
| `resume` | 继续当前 session 的获授权低风险动作 | 是 |
| `doctor` | 做系统、映射和工作区只读诊断 | 否 |
| `output` | 显示产物、QA 和 provenance 路径 | 否 |

初始化、输入队列、状态刷新和安全恢复由这个公开入口内部协调。普通用户不需要组合其他脚本，也不要直接修改 `qa/`、`.workflow/` 或 session JSON。

## 第一次运行

Skyrim SE/AE：

```powershell
python scripts\smt.py run "D:\Mods\ExampleMod.zip" --game skyrim-se
```

Fallout 4 Experimental：

```powershell
python scripts\smt.py run "D:\Mods\ExampleMod.7z" --game fallout4
```

目录输入同样受支持：

```powershell
python scripts\smt.py run "D:\Mods\ExampleMod" --game skyrim-se
```

新输入默认在 Windows 文档目录下的 `Documents/SkyrimModTranslationWorkspaces` 创建新工作区。默认规则是“每个新输入一个新工作区、一个 session、一个当前 Mod”。相同内容和身份的同一输入会复用已登记工作区；内容改变后会得到新的输入身份和新工作区。

工作区保存 `mod/` 沙盒副本、`source/`、`work/`、`translated/`、`qa/`、`out/`、`glossary/`、`.workflow/` 和本机工具配置；`.skyrim-chs-workspace.json` marker 是游戏身份来源。流程不按 Mod 名猜游戏。`Classic Holstered Weapons - v1.09-46101-1-09-1779912557` 虽然是 Fallout 4 Mod，也必须由 `--game fallout4` 明确选择。公开 `run` 要求显式 game，不会用普通 CLI 交互或二次确认代替顶层 Agent 对话。原始输入不会被直接修改。

### 显式工作区

需要固定位置时传入 `--workspace`：

```powershell
python scripts\smt.py run "D:\Mods\ExampleMod.zip" --game skyrim-se --workspace "D:\SkyrimCHS\ExampleMod"
```

- 路径不存在：允许创建。
- 路径为空：允许初始化。
- 合法且 marker/session/输入身份匹配：允许复用。
- 非空但不是工作区，或身份不匹配：退出码 `6`。
- 命令绝不清空、覆盖或重新绑定不匹配目录。

在工作区 A 中运行新输入 B，且没有显式 `--workspace` 时，CLI 不会偷偷把 B 绑定到 A；它会继续查询 B 的输入映射或创建新工作区。

## 工具准备模式

`run` 默认使用 `--tool-setup auto`：

| 模式 | 行为 |
|---|---|
| `auto` | 在机器共享不可变缓存中发布或复用受控非 GUI 工具并绑定当前工作区；只有缺失、损坏或版本不匹配时才产生新代 |
| `manual` | 只检测并显示配置建议，不自动安装 |
| `skip` | 完全跳过工具准备；后续预检可以返回工具缺失阻断 |

示例：

```powershell
python scripts\smt.py run "D:\Mods\ExampleMod.zip" --game skyrim-se --tool-setup skip
```

LexTranslator、xTranslator 等 GUI 工具仍需用户自行安装。只有 Codex 可以执行获授权桌面步骤；opencode 和 Claude Code 会返回 `needs_gui` 并要求交给 Codex。

`auto` 不会把共享缓存绝对路径写进 `tools.local.json`。该文件仍只保存用户明确配置的外部工具。旧工作区的 `tools/` 只在精确旧路径、项目 manifest 和完整 hash 清单共同证明身份时作为只复制迁移来源；旧副本始终保留，不会被自动移动或删除。

如果旧路径的项目归属都无法完整证明，`auto` 会要求用户处理，不能覆盖、删除或绕过未知内容。只有旧副本已证明由项目生成、但复制后的内容无法满足当前版本的确定性 key 或完整清单时，流程才会保留旧副本并改走正常共享安装。

旧副本即使能够证明身份，也不会被检测器或运行时直接当作后备工具。只有 `auto` 将其复制到共享仓库、完成验证并提交工作区 binding 后才会执行；已有 binding 损坏、失效或对应缓存被卸载时会明确阻断并要求重新运行 `auto`，不会静默退回旧 venv 或 bootstrap Python。

0.4.0 及更早版本创建的合法 schema v2 工作区 marker 可能没有
`workspace_id`。只读检测不会改写这类工作区；如果已有匹配的 SMT session，
它只在本次读取中采用该 session 的 UUID。运行 `auto` 时会在工作区进程锁下
原子补齐身份：优先沿用匹配 session 的 UUID，没有 session 才分配新 UUID。
非法 UUID 或 marker/session 的游戏、UUID 冲突会原样保留并停止，不会随机覆盖。

共享的插件、PEX、localized delivery 和字符串表 adapter 会始终使用其构建身份对应的共享 .NET SDK，并在整个工具进程期间同时持有租约。只有 adapter 本身也是用户明确配置的外部工具时，外部 `DotNetSdkPath` 才作为该外部组合的运行时；它不会替换共享 adapter 的 SDK。

### 清理或卸载共享工具缓存

普通翻译命令不会自动清理缓存。如果你明确要求“查看工具缓存”“释放旧工具空间”或“卸载共享工具”，Agent 会使用 `managed-tool-cache-maintenance` Skill：

1. 先只读检查缓存；
2. 生成包含条目、引用、大小、影响、有效期和确认 token 的计划；
3. 向你展示完整计划并等待确认；
4. 只应用你确认的同一计划；
5. 再次只读检查结果。

未引用清理按条目尽力执行；活动、改变或重新被引用的条目会保留并报告 `partial`。完整卸载的资格检查和原子 detach 是全有或全无；如果 detach 后的物理删除中断，会保留 plan-scoped trash 并报告 `interrupted`，不能宣称卸载完成。维护流程不会终止正在使用工具的进程，也不会删除工作区、Mod、译文、QA、外部手动工具或控制锁。完整卸载后，现有绑定会暂时不可用；后续 `--tool-setup auto` 可恢复。

## 查看状态快照

```powershell
python scripts\smt.py status
```

`status` 成功读取工作区后始终返回退出码 `0`，即使状态卡显示 `blocked` 或 `qa_failed`。它只展示最近一次生成的状态快照，不刷新 readiness/state/tasks，也不声称重新验证了人工测试证据。输出会标明快照生成时间和 `refreshed_by_this_command=false`。

文本模式会展示权威进度卡中的 `[SMT 进度]`、`[SMT 阻断]` 或 `[SMT 完成]`，不会用命令 stdout 或 trace 猜测阶段。

从无关目录调用时，`status` 会按“显式 `--workspace`、当前目录工作区、最近活动工作区”选择目标。需要指定位置时：

```powershell
python scripts\smt.py status --workspace "D:\SkyrimCHS\ExampleMod"
```

## 继续当前 session

```powershell
python scripts\smt.py resume
```

`resume` 每次只选择当前 Mod 的精确、获授权、低风险、非 GUI 任务。没有低风险任务时是退出码 `0` 的 no-op；存在 Agent、GUI 或用户动作时返回相应公开结果和退出码 `3`。它不会把底层恢复脚本的内部退出码直接暴露为用户失败。

指定工作区：

```powershell
python scripts\smt.py resume --workspace "D:\SkyrimCHS\ExampleMod"
```

## 只读诊断

```powershell
python scripts\smt.py doctor
```

`doctor` 是只读诊断。它可以读取系统、默认根目录直属工作区、session、marker、工具版本和映射，并写 CLI 自有诊断日志；它不会：

- 安装工具或重建 adapter；
- 清理 partial/reservation；
- 删除失效映射；
- 修改工具配置、session 或 workflow 状态；
- 自动认领未登记工作区。

需要诊断指定工作区时：

```powershell
python scripts\smt.py doctor --workspace "D:\SkyrimCHS\ExampleMod"
```

## 查看产物

```powershell
python scripts\smt.py output
```

`output` 使用 session 的精确 Mod 名显示：

```text
out/<ModName>/汉化产出/final_mod/
out/<ModName>/汉化产出/intermediate/
out/<ModName>/汉化产出/<ModName>_CHS.zip
```

它还显示关键 QA/provenance 路径，并分别报告：

- 可以进入人工游戏测试：是/否。
- 人工游戏测试已验证：是/否。

产物尚不存在时，普通查询仍返回退出码 `0`，并把对应路径标记为不存在。只允许打开预定义目标：

```powershell
python scripts\smt.py output --open final-mod
```

可选值为 `root`、`final-mod`、`intermediate` 和 `package-directory`；目标不存在或越出工作区时返回退出码 `1`，不会打开任意路径。

## 公开结果

| outcome | 下一步 |
|---|---|
| `completed` | 当前 session 已达到 `manual_tested`，人工游戏测试证据有效 |
| `ready_for_manual_test` | 可以进入人工游戏测试，但尚未证明人工游戏测试已验证 |
| `needs_agent_translation` | Agent 处理 JSON `next_action.artifacts` 指定的候选或校对包，然后调用 `resume` |
| `needs_gui` | 交给具备桌面能力的 Codex，完成获授权 GUI 步骤后调用 `resume` |
| `needs_user_input` | 提供明确要求的文件、游戏、术语或选择后调用 `resume` |
| `blocked` | 按 diagnostics 和进度卡处理安全、能力或 QA 阻断 |

`completed` 严格等价于有效的 `manual_tested`，不能表示“CLI 运行结束”“没有自动任务”或“静态 QA 通过”。

## 退出码

公开 outcome 与退出码是两个维度：

| 退出码 | 含义 |
|---:|---|
| `0` | 成功、`completed`、`ready_for_manual_test` 或 no-op |
| `1` | 普通读取、打开路径或内部失败 |
| `2` | 参数格式错误 |
| `3` | Agent、GUI、用户输入或普通安全暂停 |
| `4` | 输入格式或资源能力不支持 |
| `5` | 工具或运行环境不可用 |
| `6` | session、marker 或工作区身份冲突 |
| `124` | 超时 |
| `130` | 用户中断 |

不要只根据非零退出码判断底层脚本失败；应同时阅读 outcome、message、next_action 和 diagnostics。

## Agent 使用

用户对顶层 Agent 说“翻译这个 Mod”时，Agent 首次只调用：

```powershell
python scripts\smt.py --format json run "D:\Mods\ExampleMod.zip" --game skyrim-se
```

JSON 模式 stdout 只有一个 schema v1 对象。Agent 根据 `outcome` 和 `next_action` 处理语言或获授权 GUI 工作，后续只使用公开的 `resume`、`status`、`doctor` 和 `output`，不得自行组合初始化、queue、canonical refresh、任务领取或 QA 底层脚本。

## 词典与翻译边界

新工作区会创建 `glossary/`。词典不是开始汉化的必要条件，但能改善专有名词和重复文本的一致性。当前 Mod 已确认术语写入 `glossary/mod_terms.md`；Skyrim 与 Fallout 4 词典按 Game Profile 隔离，不交叉使用。

脚本名、EditorID、FormID、文件路径和协议值不应当作普通文本翻译。不确定译名可以让 Agent 暂存为待确认项。

STRINGS/DLSTRINGS/ILSTRINGS、外部字符串表联合插件、Light 插件/目标 owner、PEX、BSA/BA2 都走各自受控 adapter 和证据门禁。官方 Full master 使用版本化策略确认，不要求复制 `Skyrim.esm`、`Update.esm` 或 `Fallout4.esm`；无关第三方 master 缺失不会阻断当前插件自己创建的翻译目标。

用户可以直接对 Agent 说“翻译 mod”；中途暂停后说“继续汉化”。顶层 Agent 仍只调用本指南列出的公开 CLI。

## 人工游戏测试

只有 `ready_for_manual_test` 后才把 `_CHS.zip` 或 `final_mod/` 导入隔离的目标游戏测试环境。至少检查：

- 游戏能否正常启动并加载存档；
- 菜单、MCM、提示、任务、对话和物品文本；
- 脚本触发、插件加载顺序和 Mod 冲突；
- 中文截断、乱码、漏译和占位符；
- 测试包是否与最新 QA 报告一致。

完成并验证人工测试证据后，公开结果才可以成为 `completed`。项目内检查和静态反解析不能替代真实游戏验证。
