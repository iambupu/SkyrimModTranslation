# Fallout 4 Experimental Support

本文是 Fallout 4 profile 的能力合同和审计参考。它不重复安装、初始化、放入 Mod 或日常操作流程。

## 支持级别

对外支持级别固定为 **Fallout 4 Experimental Support**。`support_level=experimental` 表示已有受控实现和合成回归，但没有覆盖所有真实 Mod、工具版本和游戏运行环境。Experimental 本身不造成永久阻断；命中未支持能力或缺少必需证据时必须 fail closed。

## 游戏身份

工作区 `.skyrim-chs-workspace.json` 是游戏身份的权威来源。Fallout 4 工作区必须记录 `game_id=fallout4`。流程不得根据 Mod 名、目录名、F4SE 文件或 BA2 文件猜测游戏。

显式 game 与 marker 冲突时立即失败。任何下游报告声明了不同的 `game_id`、profile version、adapter 或 PEX category，都视为 stale/mismatch，不能继续用于严格 QA。

当前 profile 的权威 capability 合同：

| 字段 | 值 |
|---|---|
| `game_id` | `fallout4` |
| `support_level` | `experimental` |
| `capabilities.plugin_text.level` | `experimental_write` |
| `capabilities.plugin_text.adapter` | `mutagen-bethesda-plugin` |
| `capabilities.plugin_text.options.adapter_contract_version` | `1` |
| `capabilities.plugin_text.options.mutagen_release` | `Fallout4` |
| `capabilities.plugin_text.options.extract_backend` | `mutagen-adapter` |
| `capabilities.plugin_text.options.localized_plugin_policy` | `block` |
| `capabilities.pex.level` | `experimental_write` |
| `capabilities.pex.adapter` | `mutagen-pex` |
| `capabilities.pex.options.pex_category` | `Fallout4` |
| `capabilities.archive.bsa.level` | `unsupported` |
| `capabilities.archive.ba2.level` | `read_only` |
| `capabilities.loose_text.level` | `stable` |
| `capabilities.string_tables.level` | `experimental_write` |

资源 capability 是执行与严格 QA 的唯一能力来源。Profile 只接受 schema v2，不读取、不派生也不传播旧顶层能力字段；`support_level` 只用于对外说明，不能替代逐项 capability 判断。

## 能力矩阵

| 输入或能力 | 状态 | 放行条件 |
|---|---|---|
| loose text、JSON、XML、CSV、TXT | 可处理 | 结构、占位符和编码检查通过 |
| Interface / MCM 可见文本 | 可处理 | MCM 按实际文件格式路由；Interface 按 Game Profile 的运行时编码和结构验证 |
| 非 localized ESP/ESM | Experimental 可处理 | 白名单字段、受控写回、`Fallout4Mod` 反解析不变量通过 |
| ESL / light FormID | Experimental | 当前插件或实际写回目标所需的 master-style 证据、canonical FormKey、精确 occurrence 和显式实验写回授权；无关依赖不参与门禁 |
| localized plugin | Experimental | 只通过 `localized_delivery` 联合插件锚点、引用覆盖和字符串表组件；generic plugin path 仍阻断 |
| STRINGS / DLSTRINGS / ILSTRINGS | Experimental | 专用 adapter 负责清点、导出、写回和复核；不得当作普通 loose text 处理 |
| PEX Export | 可用 | 类别为 `Fallout4`，导出身份和输入 hash 一致 |
| PEX Apply | Experimental | 显式 opt-in 后可生成并验证工作区副本；strict completion 当前固定阻断 |
| BA2 inventory | 可用 | 只读解析，不授权物化 |
| BA2 materialization | Experimental 可用 | 受控 adapter、receipt、manifest、hash 和路径验证全部通过 |
| BA2 repack | 禁止 | `capabilities.archive.ba2.level=read_only` 不提供 write/repack |
| SWF / GFX / DLL / EXE | 受保护 | 只读审计或原样复制，不修改 |
| 游戏内效果 | 人工验证 | 不由仓库 QA 或 fixture 认证 |

## Data container 与安全路由

Fallout 4 Mod 是 Data 根下的多资源集合，不能只按 ESP 和 BA2 理解。目录是 container，扩展名决定资源类型。两者共同决定路由。

| 路径或 container | 当前合同 |
|---|---|
| `Materials/*.bgsm`、`Materials/*.bgem` | 受保护，只能从 `mod/` 原样复制 |
| `Meshes/`、`Textures/` | 受保护，只能从 `mod/` 原样复制 |
| `Sound/`、`Music/`、`Video/` | 受保护，只能从 `mod/` 原样复制 |
| `Vis/`、`Seq/` | 受保护，只能从 `mod/` 原样复制 |
| `MCM/` | MCM 是 container；按 JSON、INI、TOML、TXT 等实际格式继续路由 |
| `F4SE/` | DLL 不修改；INI/TOML 整行注释可进入只读候选包，key/value 只处理结构化确认的玩家可见内容 |
| `Interface/*.swf`、`Interface/*.gfx` | 只做 inventory 和人工检查；优先使用外部 `Interface/translations/*.txt` |

Materials、Meshes、Textures、Sound、Music、Video、Vis、Seq 下的文件默认属于 original-copy 交付。source SHA256 与 final SHA256 必须相同。宽泛的 Tool Adapter 二进制条款不能放开这些资源；`tool_outputs` 只接收当前 Profile 明确允许写回的插件、PEX 或字符串表。

MCM 中的 key、路径、协议值和内部标识必须保留。F4SE 配置同样不能按“所有字符串都可翻译”处理。字段用途不明时保持原文，并进入人工复核。

SWF/GFX 当前没有写回能力。外部 translations TXT 存在时优先翻译该文本；不存在时只记录 inventory/manual 结果，不能反编译界面文件后回写。

## 插件 adapter 不变量

非 localized 插件只能处理 Game Profile 白名单中的玩家可见字段。输入和输出都必须由 Mutagen `Fallout4Mod` 解析。写回验证至少覆盖：

- masters 不变。
- FormID 不变。
- record count 不变。
- 解析结构与逻辑 payload 不变量通过：record、FormID、record flags、解析后的 subrecord 类型/顺序/索引和非目标逻辑 payload 保持一致，目标 source/target 精确匹配；允许变化的目标 record data-size 与祖先 GRUP size 会在报告中列出。压缩记录会先解压，`XXXX` 扩展长度包装会被解析，因此当前校验不承诺压缩流、`XXXX` 包装形式或文件中除目标范围外的每个原始字节完全不变。
- missing 和 unsupported 字段数为 0。
- 输入、译表、输出和验证报告的 game/profile/adapter metadata 一致。

`mutagen-bethesda-plugin` 表示选中了共享的受控插件 adapter；具体格式由 Profile 的 `mutagen_release=Fallout4` 传入。它不代表任意插件结构都已认证。解析失败、字段越出白名单或不变量变化时必须阻断。

## localized 与 STRINGS

Fallout 4 localized plugin 依赖外部 string table。字符串表 adapter 可按 `experimental_write` 独立导出、写回和验证；`capabilities.plugin_text.options.localized_plugin_policy=block` 继续阻止 generic plugin path 单独放行 localized 插件。联合交付改由同为 `experimental_write` 的 `localized_delivery` 复合能力完成。

检测到 `.strings`、`.dlstrings`、`.ilstrings` 时，路由必须进入专用 string-table adapter；检测到 localized flag 时，必须进入 composite 路径。能力未显式授权、组件不完整或证据不一致时生成明确 blocker。不得：

- 用 Skyrim string table 流程兜底。
- 把 STRINGS 当作普通文本直接改写。
- 只翻译插件内残留文本后宣称完整。
- 因缺少工具而把“不支持”降级成 warning。

## PEX Export 与 Apply

PEX Export 使用 `Fallout4` category，并按 `config/pex_visible_apis/fallout4.json` 解析调用目标、opcode 和语义参数位置。当前自动可写范围只有 fixture 证明过的 `Debug.Notification` 和 `Debug.MessageBox` 直接字面量；其他 API、未知调用和动态参数进入 `manual_review`，诊断、配置和协议值标记为 `protected`。文本是否像自然语言不能改变分类，新增 API 必须先补 fixture 和输出验证。

PEX Apply 的状态是 experimental。受控工具只有在调用方显式 opt-in 后才能生成工作区副本。输出还需要：

- 输入身份、相对路径和 SHA256 匹配。
- 译表中的每个写回行都保留精确 object/state/function/instruction/argument、callee、semantic role、visibility basis 和 source identity，且分类为 `visible`。
- 输出可按 `Fallout4` category 反读。
- 写回报告、验证报告和 final_mod provenance 一致。
- strict gate 明确记录该输出当前不具备放行资格。

缺少任一验证项时立即 blocked。即使验证项齐全，当前 strict completion 仍固定阻断；现阶段没有可提交的额外证据可以解除门禁。生成了 `.pex` 文件不等于 Apply 已认证。

## BA2 安全协议

BA2 materialization 只在 Fallout 4 profile 启用。Skyrim profile 对 BA2 保持 inventory-only。

物化链必须通过项目受控 wrapper 调用符合 `skyrim-mod-chs.ba2-extractor.v1` 协议的审查过 adapter。有效证据包括：

- 解包前后的源 BA2 SHA256 和大小一致。
- adapter 完成后、发布前生成规范 entry 清单与 payload root；receipt binding 覆盖源文件快照、adapter identity/protocol、limits 和 staging payload snapshot。
- manifest 生成和独立 verify 都逐项比较当前 extracted payload 与 receipt，任何增删改或路径变化失败。
- extraction manifest 列出每个 entry 的规范路径、大小和 SHA256。
- 独立验证拒绝绝对路径、`..`、链接、硬链接、路径碰撞和越界输出。
- 发布目录位于当前工作区允许的归档审计路径。
- BA2 来源的译文带 entry hash、manifest 路径和同路径 loose override provenance。

只读 inventory 不授权 materialization。外部进程也不是操作系统沙箱；wrapper 能验证发布结果和已观察到的路径副作用，不能证明恶意可执行文件没有写入任意系统位置。因此只有经过审查、位于工作区或插件目录的 adapter 才能进入该协议。

交付始终使用 same-path loose override。源 BA2 原样保留，不修改、不重打包；当前归档 capability 不提供 write/repack。

## 受保护文件

SWF、GFX、DLL、EXE 以及其他不可翻译二进制只允许：

- 只读 inventory 或风险审计。
- 从工作区 `mod/` 原样复制到 final_mod。
- 记录 source SHA256 与 final SHA256 相同的 provenance。

任何内容改写、反编译后回写、重新编译或用旁挂文件冒充直接替换，都不属于当前能力合同。

Materials、Meshes、Textures、Sound、Music、Video、Vis、Seq 也适用同一保护规则。它们不得因为来自 Tool Adapter 输出而改变来源资格。

## 报告与 mismatch

readiness、workflow state、tasks、handoff、progress、strict QA、final manifest 和 binary review metadata 必须传播同一组公共游戏字段：`game_id`、`game_profile_version`、`game_display_name`、`support_level` 和 `interface_translation_encoding`。插件、PEX 与归档工具报告另行记录本次调用的 adapter、operation、category/options 和 hash 证据，并与当前 capability 对照。

以下情况必须视为 stale/mismatch：

- Skyrim 证据出现在 Fallout 4 工作区，或反向出现。
- profile version 不一致。
- 工具证据的 adapter contract version 或 PEX category 与当前 capability options 不一致。
- 报告声称 BA2 可重打包，或缺少 loose override 交付策略。
- 工具输出的输入 hash、输出 hash 或相对路径与当前文件不一致。

刷新报告不能修复真实能力缺口。localized/STRINGS 缺少复合或组件证据、未认证 PEX Apply，或请求的 BA2 模式没有可用受控路径时仍应保持 blocked。内置路径只 materialize GNRL；DX10 保持 inventory-only，完整提取仍可能需要受控外部 adapter。

## final_mod 与严格 QA

交付目录合同仍是 `out/<ModName>/汉化产出/final_mod/` 和 `<ModName>_CHS.zip`。普通规模可以构建完整副本，大型 Mod 可以构建声明依赖原 Mod 的翻译覆盖层；两者都保持 Fallout 4 Data 根相对路径。允许 F4SE、Materials、MCM、Strings 等 Fallout 4 目录名，不套用 Skyrim 专属提示。

插件必须保持原相对路径和原文件名。普通或 light 插件只有在 Profile 允许写回且对应验证证据完整时，才能从 `tool_outputs` 覆盖原路径。STRINGS 家族需要专用 AdapterResult；localized 插件和字符串表还必须作为一个事务化复合组件集发布。当前这些实验能力只能形成工作区人工测试产物，不能表示为稳定完成。

严格 QA 至少验证：

- final manifest 与 provenance 的游戏 metadata 一致。
- 每个 final_mod 文件都有直接来源，source/final hash 可复核。
- 非 localized 插件满足 `Fallout4Mod` 反解析不变量。
- PEX Apply 状态与验证证据一致，并保持当前固定的 strict blocker。
- BA2 原 hash、receipt、manifest、路径安全和 loose override 证据完整。
- localized/STRINGS 的组件、引用覆盖、语言、hash 或 composite receipt 不完整时保持 blocked。

项目 QA 只决定能否进入人工游戏测试，不写入“真实游戏已认证”结论。

## fixture 与认证差距

仓库的 Fallout 4 回归使用合成 fixture。`Classic Holstered Weapons - v1.09-46101-1-09-1779912557` 只作为真实用例名，合成目录验证 marker 权威、F4SE/Materials/MCM/Strings 路由、DLL 保护和跨游戏 mismatch。

合成 fixture 不包含该 Mod 的真实二进制，也不证明真实插件、PEX、BA2、F4SE DLL、加载顺序或游戏内界面已经通过测试。effect regression、CI 和 Mutagen 反解析同样不能替代真实游戏认证。

从 Experimental 提升认证级别，需要有版本固定的真实工具链、合法可复现的真实样本、插件与 PEX 写回不变量、BA2 安全证据、对应游戏版本的加载测试、人工界面与脚本测试，以及可审计的失败记录。认证必须按能力逐项提升，不能由单个成功样本推导为完整支持。
