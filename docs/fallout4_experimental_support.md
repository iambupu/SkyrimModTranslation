# Fallout 4 Experimental Support

本文是 Fallout 4 profile 的能力合同和审计参考。它不重复安装、初始化、放入 Mod 或日常操作流程。

## 支持级别

对外支持级别固定为 **Fallout 4 Experimental Support**。`support_level=experimental` 表示已有受控实现和合成回归，但没有覆盖所有真实 Mod、工具版本和游戏运行环境。Experimental 本身不造成永久阻断；命中未支持能力或缺少必需证据时必须 fail closed。

## 游戏身份

工作区 `.skyrim-chs-workspace.json` 是游戏身份的权威来源。Fallout 4 工作区必须记录 `game_id=fallout4`。流程不得根据 Mod 名、目录名、F4SE 文件或 BA2 文件猜测游戏。

显式 game 与 marker 冲突时立即失败。任何下游报告声明了不同的 `game_id`、profile version、adapter 或 PEX category，都视为 stale/mismatch，不能继续用于严格 QA。

当前 profile 合同：

| 字段 | 值 |
|---|---|
| `game_id` | `fallout4` |
| `game_display_name` | `Fallout 4` |
| `support_level` | `experimental` |
| `plugin_adapter` | `fallout4-mutagen` |
| `plugin_adapter_version` | `1` |
| `pex_category` | `Fallout4` |
| `pex_writeback_status` | `experimental` |
| `archive_delivery` | `loose_override` |
| `archive_materialization_enabled` | `true` |
| `archive_allow_repack` | `false` |

## 能力矩阵

| 输入或能力 | 状态 | 放行条件 |
|---|---|---|
| loose text、JSON、XML、CSV、TXT | 可处理 | 结构、占位符和编码检查通过 |
| Interface / MCM 可见文本 | 可处理 | 按 Game Profile 的运行时编码和结构验证 |
| 非 localized ESP/ESM/ESL | Experimental 可处理 | 白名单字段、受控写回、`Fallout4Mod` 反解析不变量通过 |
| localized plugin | blocked | 当前没有受支持的 string-table 写回链 |
| STRINGS / DLSTRINGS / ILSTRINGS | blocked | 不得当作普通 loose text 处理 |
| PEX Export | 可用 | 类别为 `Fallout4`，导出身份和输入 hash 一致 |
| PEX Apply | Experimental | 显式 opt-in 后可生成并验证工作区副本；strict completion 当前固定阻断 |
| BA2 inventory | 可用 | 只读解析，不授权物化 |
| BA2 materialization | Experimental 可用 | 受控 adapter、receipt、manifest、hash 和路径验证全部通过 |
| BA2 repack | 禁止 | `allow_repack=false` |
| SWF / GFX / DLL / EXE | 受保护 | 只读审计或原样复制，不修改 |
| 游戏内效果 | 人工验证 | 不由仓库 QA 或 fixture 认证 |

## 插件 adapter 不变量

非 localized 插件只能处理 Game Profile 白名单中的玩家可见字段。输入和输出都必须由 Mutagen `Fallout4Mod` 解析。写回验证至少覆盖：

- masters 不变。
- FormID 不变。
- record count 不变。
- 非目标字段不变。
- missing 和 unsupported 字段数为 0。
- 输入、译表、输出和验证报告的 game/profile/adapter metadata 一致。

`fallout4-mutagen` 只表示选中了 Fallout 4 插件路径，不代表任意插件结构都已认证。解析失败、字段越出白名单或不变量变化时必须阻断。

## localized 与 STRINGS

Fallout 4 localized plugin 依赖外部 string table。当前 profile 设置 `supports_localized_plugins=false`、`string_tables_enabled=false`。

检测到 localized flag，或发现 `.strings`、`.dlstrings`、`.ilstrings` 时，路由必须生成明确 blocker。不得：

- 用 Skyrim string table 流程兜底。
- 把 STRINGS 当作普通文本直接改写。
- 只翻译插件内残留文本后宣称完整。
- 因缺少工具而把“不支持”降级成 warning。

## PEX Export 与 Apply

PEX Export 使用 `Fallout4` category，可生成工作区内可见字符串中间件。受保护 opcode、比较字符串、脚本名、变量名和结构参数不能进入可写回候选。

PEX Apply 的状态是 experimental。受控工具只有在调用方显式 opt-in 后才能生成工作区副本。输出还需要：

- 输入身份、相对路径和 SHA256 匹配。
- 译表只包含允许写回的可见字符串。
- 输出可按 `Fallout4` category 反读。
- 写回报告、验证报告和 final_mod provenance 一致。
- strict gate 明确记录该输出当前不具备放行资格。

缺少任一验证项时立即 blocked。即使验证项齐全，当前 strict completion 仍固定阻断；现阶段没有可提交的额外证据可以解除门禁。生成了 `.pex` 文件不等于 Apply 已认证。

## BA2 安全协议

BA2 materialization 只在 Fallout 4 profile 启用。Skyrim profile 对 BA2 保持 inventory-only。

物化链必须通过项目受控 wrapper 调用符合 `skyrim-mod-chs.ba2-extractor.v1` 协议的审查过 adapter。有效证据包括：

- 解包前后的源 BA2 SHA256 和大小一致。
- adapter receipt 与调用参数、源文件、staging payload 一致。
- extraction manifest 列出每个 entry 的规范路径、大小和 SHA256。
- 独立验证拒绝绝对路径、`..`、链接、硬链接、路径碰撞和越界输出。
- 发布目录位于当前工作区允许的归档审计路径。
- BA2 来源的译文带 entry hash、manifest 路径和同路径 loose override provenance。

只读 inventory 不授权 materialization。外部进程也不是操作系统沙箱；wrapper 能验证发布结果和已观察到的路径副作用，不能证明恶意可执行文件没有写入任意系统位置。因此只有经过审查、位于工作区或插件目录的 adapter 才能进入该协议。

交付始终使用 same-path loose override。源 BA2 原样保留，不修改、不重打包，`allow_repack=false`。

## 受保护文件

SWF、GFX、DLL、EXE 以及其他不可翻译二进制只允许：

- 只读 inventory 或风险审计。
- 从工作区 `mod/` 原样复制到 final_mod。
- 记录 source SHA256 与 final SHA256 相同的 provenance。

任何内容改写、反编译后回写、重新编译或用旁挂文件冒充直接替换，都不属于当前能力合同。

## 报告与 mismatch

readiness、workflow state、tasks、handoff、progress、strict QA、final manifest 和 binary review metadata 必须传播同一组游戏字段。关键字段包括 `game_id`、`game_profile_version`、`game_display_name`、`support_level`、`plugin_adapter`、`plugin_adapter_version`、`pex_category`、`pex_writeback_status`、`archive_delivery`、`archive_materialization_enabled` 和 `archive_allow_repack`。

以下情况必须视为 stale/mismatch：

- Skyrim 证据出现在 Fallout 4 工作区，或反向出现。
- profile version 或 adapter version 不一致。
- PEX category 不是 `Fallout4`。
- 报告声称 BA2 可重打包，或缺少 loose override 交付策略。
- 工具输出的输入 hash、输出 hash 或相对路径与当前文件不一致。

刷新报告不能修复真实能力缺口。localized/STRINGS、未认证 PEX Apply 或无受控 BA2 adapter 仍应保持 blocked。

## final_mod 与严格 QA

交付目录合同仍是 `out/<ModName>/汉化产出/final_mod/` 和 `<ModName>_CHS.zip`。Data 根按 Fallout 4 profile 判断，允许 F4SE、Materials、MCM、Strings 等 Fallout 4 目录名，不套用 Skyrim 专属提示。

严格 QA 至少验证：

- final manifest 与 provenance 的游戏 metadata 一致。
- 每个 final_mod 文件都有直接来源，source/final hash 可复核。
- 非 localized 插件满足 `Fallout4Mod` 反解析不变量。
- PEX Apply 状态与验证证据一致，并保持当前固定的 strict blocker。
- BA2 原 hash、receipt、manifest、路径安全和 loose override 证据完整。
- localized/STRINGS 等未支持的必需输入保持 blocked。

项目 QA 只决定能否进入人工游戏测试，不写入“真实游戏已认证”结论。

## fixture 与认证差距

仓库的 Fallout 4 回归使用合成 fixture。`Classic Holstered Weapons - v1.09-46101-1-09-1779912557` 只作为真实用例名，合成目录验证 marker 权威、F4SE/Materials/MCM/Strings 路由、DLL 保护和跨游戏 mismatch。

合成 fixture 不包含该 Mod 的真实二进制，也不证明真实插件、PEX、BA2、F4SE DLL、加载顺序或游戏内界面已经通过测试。effect regression、CI 和 Mutagen 反解析同样不能替代真实游戏认证。

从 Experimental 提升认证级别，需要有版本固定的真实工具链、合法可复现的真实样本、插件与 PEX 写回不变量、BA2 安全证据、对应游戏版本的加载测试、人工界面与脚本测试，以及可审计的失败记录。认证必须按能力逐项提升，不能由单个成功样本推导为完整支持。
